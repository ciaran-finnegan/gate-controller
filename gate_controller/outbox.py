import base64
import hashlib
import os
import tempfile
import warnings
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from threading import Event

import requests
from PIL import Image, ImageOps


MAX_IMAGE_PIXELS = 16_000_000
Image.MAX_IMAGE_PIXELS = min(Image.MAX_IMAGE_PIXELS or MAX_IMAGE_PIXELS, MAX_IMAGE_PIXELS)

LOCAL_IMAGE_PATH_KEY = "_local_image_path"
MAX_OUTBOX_IMAGE_BYTES = 512 * 1024
MAX_OUTBOX_IMAGE_DIMENSION = 1280


class OutboxSyncError(RuntimeError):
    pass


class EvidenceSpoolError(RuntimeError):
    pass


class EvidenceSpool:
    """Own private, content-addressed JPEG evidence until delivery succeeds."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def stage(self, source_path: Path) -> str:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                encoded = _normalise_jpeg(Path(source_path))
        except Exception as error:
            raise EvidenceSpoolError(
                f"could not prepare JPEG evidence: {source_path}"
            ) from error
        if encoded is None:
            raise EvidenceSpoolError(f"could not prepare JPEG evidence: {source_path}")
        digest = hashlib.sha256(encoded).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        destination = self.root / f"{digest}.jpg"
        descriptor, temporary_name = tempfile.mkstemp(prefix=".tmp-", dir=self.root)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                descriptor = -1
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            destination.chmod(0o600)
            _fsync_directory(self.root)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
        return digest

    def load(self, digest: str) -> bytes:
        path = self._path(digest)
        try:
            with path.open("rb") as source:
                encoded = source.read(MAX_OUTBOX_IMAGE_BYTES + 1)
        except OSError as error:
            raise EvidenceSpoolError(f"evidence is unavailable: {digest}") from error
        if len(encoded) > MAX_OUTBOX_IMAGE_BYTES:
            raise EvidenceSpoolError(f"evidence exceeds the upload limit: {digest}")
        if hashlib.sha256(encoded).hexdigest() != digest:
            raise EvidenceSpoolError(f"evidence digest mismatch: {digest}")
        try:
            with Image.open(BytesIO(encoded)) as image:
                if image.format != "JPEG":
                    raise EvidenceSpoolError(f"evidence is not JPEG: {digest}")
                image.verify()
        except EvidenceSpoolError:
            raise
        except Exception as error:
            raise EvidenceSpoolError(f"evidence is corrupt: {digest}") from error
        return encoded

    def delete(self, digest: str) -> None:
        try:
            self._path(digest).unlink(missing_ok=True)
        except OSError as error:
            raise EvidenceSpoolError(f"could not delete evidence: {digest}") from error

    def cleanup(self, pending_digests: set[str]) -> None:
        if not self.root.is_dir():
            return
        for path in self.root.iterdir():
            if path.name.startswith(".tmp-") or (
                path.suffix == ".jpg" and path.stem not in pending_digests
            ):
                try:
                    path.unlink(missing_ok=True)
                except OSError as error:
                    raise EvidenceSpoolError(f"could not clean evidence: {path.name}") from error

    def _path(self, digest: str) -> Path:
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise EvidenceSpoolError("invalid evidence digest")
        return self.root / f"{digest}.jpg"


class HttpOutboxSender:
    """Deliver optional event notifications through an explicitly configured endpoint."""

    def __init__(self, url: str, *, session=None, timeout: tuple[float, float] = (2, 4),
                 bearer_token: str | None = None, controller_id: str = "primary"):
        self._url = url
        self._controller_id = controller_id
        self._session = session or requests.Session()
        self._timeout = timeout
        self._headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}

    def __call__(self, payload: dict, evidence_bytes: bytes | None = None) -> None:
        transmitted = _prepare_outbox_payload(
            payload, self._controller_id, evidence_bytes
        )
        request = {"json": transmitted, "timeout": self._timeout}
        headers = dict(self._headers)
        headers["Idempotency-Key"] = _outbox_idempotency_key(transmitted)
        request["headers"] = headers
        response = self._session.post(self._url, **request)
        if not 200 <= response.status_code < 300:
            raise OutboxSyncError(f"outbox endpoint returned HTTP {response.status_code}")


class CloudflareOutboxSender:
    def __init__(self, client, controller_id):
        self.client = client
        self._controller_id = controller_id

    def __call__(self, payload: dict, evidence_bytes: bytes | None = None) -> None:
        transmitted = _prepare_outbox_payload(payload, self._controller_id, evidence_bytes)
        try:
            acknowledgement = self.client.post_json(
                "/api/controller/events",
                transmitted,
                headers={"Idempotency-Key": _outbox_idempotency_key(transmitted)},
                expect_json=True,
                max_response_bytes=4096,
            )
        except requests.HTTPError as error:
            status_code = getattr(getattr(error, "response", None), "status_code", None)
            detail = status_code if isinstance(status_code, int) else "failure"
            raise OutboxSyncError(f"outbox endpoint returned HTTP {detail}") from error
        if not _is_ingest_acknowledgement(acknowledgement):
            raise OutboxSyncError("outbox endpoint did not confirm ingest")


def _prepare_outbox_payload(payload: dict, controller_id: str,
                            evidence_bytes: bytes | None) -> dict:
    transmitted = dict(payload)
    legacy_path = transmitted.pop(LOCAL_IMAGE_PATH_KEY, None)
    if legacy_path is not None and "image_sha256" not in transmitted:
        transmitted.setdefault("image_status", "legacy_evidence_unavailable")
    transmitted.setdefault("controller_id", controller_id)
    image_digest = transmitted.get("image_sha256")
    if evidence_bytes is not None:
        actual_digest = hashlib.sha256(evidence_bytes).hexdigest()
        if image_digest != actual_digest:
            raise OutboxSyncError("evidence bytes do not match the queued digest")
        if len(evidence_bytes) > MAX_OUTBOX_IMAGE_BYTES:
            raise OutboxSyncError("evidence exceeds the upload limit")
        transmitted["image"] = {
            "filename": f"{image_digest}.jpg",
            "content_type": "image/jpeg",
            "data_base64": base64.b64encode(evidence_bytes).decode("ascii"),
            "sha256": image_digest,
        }
    elif image_digest is not None:
        raise OutboxSyncError("queued evidence bytes are unavailable")
    return transmitted


def _outbox_idempotency_key(payload: dict) -> str:
    return hashlib.sha256(
        f"{payload['controller_id']}:{payload['event_id']}".encode("utf-8")
    ).hexdigest()


def _is_ingest_acknowledgement(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("eventId"), int)
        and not isinstance(value.get("eventId"), bool)
        and isinstance(value.get("inserted"), bool)
    )


def _normalise_jpeg(path: Path) -> bytes | None:
    if not path.is_file() or not _has_jpeg_signature(path):
        return None
    with Image.open(path) as source:
        if source.format != "JPEG":
            return None
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail(
            (MAX_OUTBOX_IMAGE_DIMENSION, MAX_OUTBOX_IMAGE_DIMENSION),
            Image.Resampling.LANCZOS,
        )
        return _bounded_jpeg(image)


def _bounded_jpeg(image: Image.Image) -> bytes | None:
    working = image
    for _ in range(4):
        for quality in (82, 70, 58, 46):
            output = BytesIO()
            working.save(output, format="JPEG", quality=quality, optimize=True)
            encoded = output.getvalue()
            if len(encoded) <= MAX_OUTBOX_IMAGE_BYTES:
                return encoded
        width = max(1, int(working.width * 0.8))
        height = max(1, int(working.height * 0.8))
        if (width, height) == working.size:
            break
        working = working.resize((width, height), Image.Resampling.LANCZOS)
    return None


def _has_jpeg_signature(path: Path) -> bool:
    try:
        with Path(path).open("rb") as source:
            return source.read(3) == b"\xff\xd8\xff"
    except OSError:
        return False


class OutboxWorker:
    """Persist remote work first, then retry it without affecting gate decisions."""

    def __init__(self, store, send: Callable[..., None], poll_interval: float = 5.0, *,
                 evidence_spool: EvidenceSpool | None = None,
                 controller_id: str = "primary",
                 clock: Callable[[], datetime] | None = None):
        self._store = store
        self._send = send
        self._poll_interval = poll_interval
        self._evidence_spool = evidence_spool or EvidenceSpool(
            self._store.path.parent / "event-evidence"
        )
        self._controller_id = controller_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._last_retention_at: datetime | None = None
        try:
            self._store.bind_pending_outbox_controller(self._controller_id)
            self._evidence_spool.cleanup(self._store.pending_evidence_digests())
        except Exception:
            pass

    def prepare_payload(self, image_path: Path | None = None) -> dict:
        payload = {"event_id": None, "controller_id": self._controller_id}
        if image_path is not None:
            try:
                payload["image_sha256"] = self._evidence_spool.stage(image_path)
            except Exception:
                payload["image_status"] = "unavailable_before_queue"
        return payload

    def enqueue(self, event_id: int) -> int:
        return self._store.queue_outbox(event_id, self.prepare_payload())

    def run_once(self) -> int:
        now = self._clock()
        self._run_retention(now)
        completed = 0
        for item_id, _queued_payload in self._store.pending_outbox_items():
            try:
                payload = self._store.prepare_outbox_attempt(item_id, self._clock())
                if payload is None:
                    continue
                image_digest = payload.get("image_sha256")
                if image_digest is None:
                    self._send(payload)
                else:
                    self._send(payload, self._evidence_spool.load(image_digest))
            except Exception:
                try:
                    self._store.mark_outbox_retry(item_id)
                except Exception:
                    pass
                continue
            try:
                self._store.complete_outbox_item(item_id, self._clock())
            except Exception:
                continue
            if (
                image_digest is not None
                and image_digest not in self._store.pending_evidence_digests()
            ):
                try:
                    self._evidence_spool.delete(image_digest)
                except Exception:
                    pass
            completed += 1
        return completed

    def _run_retention(self, now: datetime) -> None:
        if (
            self._last_retention_at is not None
            and now - self._last_retention_at < timedelta(hours=1)
        ):
            return
        self._last_retention_at = now
        try:
            self._store.purge_delivered_telemetry(now - timedelta(days=30))
        except Exception:
            pass

    def run_forever(self, stop_event: Event) -> None:
        while not stop_event.is_set():
            self.run_once()
            stop_event.wait(self._poll_interval)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
