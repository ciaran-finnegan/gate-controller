"""Keep what the OCR saw and said, for training a local recogniser later.

Every frame sent to Plate Recognizer and the answer that came back are
written under one private directory as a JPEG plus a JSON sidecar. The
answer is a pseudo-label, not truth: the sidecar keeps the raw candidates,
scores and box so a review step can confirm or correct it. The directory is
bounded by size; the oldest pairs are removed first. Writing never raises
into the recognition path.
"""
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
from threading import Lock

LOGGER = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 1024 * 1024 * 1024
MIN_MAX_BYTES = 16 * 1024 * 1024
MAX_SIDECAR_BYTES = 64 * 1024
KEEP_RESULT_KEYS = ("plate", "score", "dscore", "box", "candidates", "region", "vehicle")


class TrainingCorpus:
    def __init__(self, directory: Path, *, max_bytes: int = DEFAULT_MAX_BYTES, clock=None):
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < MIN_MAX_BYTES:
            raise ValueError("training corpus max_bytes must be at least 16 MiB")
        self.directory = Path(directory)
        self._max_bytes = max_bytes
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = Lock()
        self._records = 0
        self._failures = 0
        self._pruned = 0
        self._total_bytes: int | None = None

    def record(self, image: bytes, *, payload, source: str, geometry=None,
               extra: dict | None = None) -> Path | None:
        """Write one image/sidecar pair. Returns the JPEG path, or None on failure."""
        try:
            return self._record(image, payload=payload, source=source, geometry=geometry, extra=extra)
        except Exception:
            self._failures += 1
            LOGGER.warning("gate_corpus outcome=failed", exc_info=False)
            return None

    def _record(self, image, *, payload, source, geometry, extra):
        if not isinstance(image, (bytes, bytearray)) or not image[:3] == b"\xff\xd8\xff":
            raise ValueError("corpus images must be JPEG bytes")
        directory = self._ensure_directory()
        now = self._clock()
        digest = hashlib.sha256(image).hexdigest()
        stem = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{digest[:12]}"
        sidecar = {
            "schema_version": 1,
            "captured_at": now.isoformat(),
            "source": source,
            "image": {"sha256": digest, "bytes": len(image)},
            "geometry": _geometry_fields(geometry),
            "ocr": _ocr_fields(payload),
        }
        if isinstance(extra, dict):
            sidecar["extra"] = {k: v for k, v in extra.items() if _json_safe(v)}
        encoded = json.dumps(sidecar, sort_keys=True).encode("utf-8")
        if len(encoded) > MAX_SIDECAR_BYTES:
            sidecar["ocr"] = {"truncated": True, "plate": sidecar["ocr"].get("plate")}
            encoded = json.dumps(sidecar, sort_keys=True).encode("utf-8")
        with self._lock:
            image_path = _write_private(directory, stem + ".jpg", bytes(image))
            try:
                _write_private(directory, stem + ".json", encoded)
            except Exception:
                image_path.unlink(missing_ok=True)
                raise
            self._records += 1
            self._account(len(image) + len(encoded))
            self._prune_locked(directory)
        return image_path

    def status(self) -> dict:
        return {
            "directory": str(self.directory),
            "max_bytes": self._max_bytes,
            "bytes": self._total_bytes,
            "records": self._records,
            "failures": self._failures,
            "pruned": self._pruned,
        }

    # -- internals --------------------------------------------------------
    def _ensure_directory(self) -> Path:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        return self.directory

    def _account(self, added: int) -> None:
        if self._total_bytes is None:
            self._total_bytes = sum(
                entry.stat().st_size for entry in self.directory.iterdir() if entry.is_file()
            )
        else:
            self._total_bytes += added

    def _prune_locked(self, directory: Path) -> None:
        if self._total_bytes is None or self._total_bytes <= self._max_bytes:
            return
        pairs = sorted(
            {entry.stem for entry in directory.iterdir() if entry.is_file() and entry.suffix in (".jpg", ".json")}
        )
        for stem in pairs:
            if self._total_bytes <= self._max_bytes:
                break
            for suffix in (".jpg", ".json"):
                path = directory / (stem + suffix)
                try:
                    size = path.stat().st_size
                    path.unlink()
                    self._total_bytes -= size
                except FileNotFoundError:
                    continue
            self._pruned += 1


def _write_private(directory: Path, name: str, data: bytes) -> Path:
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=f".{name}.")
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, 0o600)
        final = directory / name
        os.replace(temporary, final)
        return final
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _geometry_fields(geometry) -> dict | None:
    if geometry is None:
        return None
    fields = {}
    for key in ("frame_width", "frame_height", "crop_left", "crop_top", "crop_width", "crop_height",
                "upload_width", "upload_height", "precropped", "cropped"):
        value = getattr(geometry, key, None)
        if _json_safe(value):
            fields[key] = value
    return fields or None


def _ocr_fields(payload) -> dict:
    if not isinstance(payload, dict):
        return {"results": []}
    results = payload.get("results")
    kept = []
    if isinstance(results, list):
        for result in results[:8]:
            if not isinstance(result, dict):
                continue
            kept.append({key: result[key] for key in KEEP_RESULT_KEYS if key in result and _json_safe(result[key])})
    fields = {"results": kept}
    if kept:
        fields["plate"] = kept[0].get("plate")
        fields["score"] = kept[0].get("score")
    for key in ("processing_time", "timestamp"):
        if key in payload and _json_safe(payload[key]):
            fields[key] = payload[key]
    return fields


def _json_safe(value, depth: int = 0) -> bool:
    if depth > 6:
        return False
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, (list, tuple)):
        return len(value) <= 64 and all(_json_safe(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return len(value) <= 64 and all(isinstance(k, str) and _json_safe(v, depth + 1) for k, v in value.items())
    return False
