"""Keep what the OCR saw and said, for training a local recogniser later.

Every frame sent to Plate Recognizer and the answer that came back are
written under one private directory as a payload file plus a JSON sidecar
sharing its stem. The answer is a pseudo-label, not truth: the sidecar keeps
the raw candidates, scores and box so a review step can confirm or correct
it. Writing never raises into the recognition path.

The pair is an *artefact*, not specifically a frame. The sidecar names its
``kind`` and ``media_type``, so the gate's own audio -- the clips
``gate_controller.audio_capture`` records in the ``audio`` directory beside
this one -- is another artefact travelling this same pipeline rather than
needing a second one. ``pending`` walks those subdirectories; the size bound
below does not, because each store prunes only what it wrote.

This directory is a **buffer, not the archive**. ``gate_controller.corpus_upload``
ships each artefact to R2 and calls :meth:`TrainingCorpus.discard` once the
cloud has confirmed it, so what remains on the SD card is only what has not
shipped yet. The size bound stays as a backstop for a long outage: past the
cap the oldest pairs are removed first, and that is a permanent loss, which is
exactly why the upload exists.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from math import isfinite
import os
from pathlib import Path
import tempfile
from threading import Lock

LOGGER = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 1024 * 1024 * 1024
MIN_MAX_BYTES = 16 * 1024 * 1024
MAX_SIDECAR_BYTES = 64 * 1024
KEEP_RESULT_KEYS = ("plate", "score", "dscore", "box", "candidates", "region", "vehicle")
#: Bumped when the sidecar gained its ``artefact`` block. Version 1 sidecars
#: are still on the card and still readable: they are frames, and the reader
#: below says so rather than making the uploader guess.
SIDECAR_SCHEMA_VERSION = 2
FRAME_KIND = "frame"
FRAME_MEDIA_TYPE = "image/jpeg"
FRAME_SUFFIX = ".jpg"
SIDECAR_SUFFIX = ".json"
#: How far under the corpus root :meth:`TrainingCorpus.pending` looks. The root
#: is depth 0 and the gate's audio clips are at depth 1, in the ``audio``
#: directory ``load_audio_capture_config`` puts beside the corpus. It is a
#: bound rather than an unlimited walk because this directory is on an SD card
#: in a warm cabinet, the poll runs every five minutes, and a symlink or a
#: mount somebody leaves under the corpus must not turn that poll into a
#: filesystem crawl.
MAX_SCAN_DEPTH = 2


@dataclass(frozen=True)
class CorpusArtefact:
    """One payload file and its sidecar, sharing a stem.

    A frame today. An audio clip needs nothing here to change: the sidecar
    names the kind and the media type, and the payload is whatever the file
    holds.
    """

    stem: str
    payload_path: Path
    sidecar_path: Path

    @property
    def captured_at(self) -> str:
        """The UTC timestamp the stem was named for, or an empty string."""
        return self.stem.split("-", 1)[0]


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
        self._discarded = 0
        self._total_bytes: int | None = None

    def record(self, image: bytes, *, payload, source: str, geometry=None,
               extra: dict | None = None, local: dict | None = None) -> Path | None:
        """Write one image/sidecar pair. Returns the JPEG path, or None on failure."""
        try:
            return self._record(
                image, payload=payload, source=source, geometry=geometry,
                extra=extra, local=local,
            )
        except Exception:
            self._failures += 1
            LOGGER.warning("gate_corpus outcome=failed", exc_info=False)
            return None

    def _record(self, image, *, payload, source, geometry, extra, local=None):
        if not isinstance(image, (bytes, bytearray)) or not image[:3] == b"\xff\xd8\xff":
            raise ValueError("corpus images must be JPEG bytes")
        directory = self._ensure_directory()
        now = self._clock()
        digest = hashlib.sha256(image).hexdigest()
        stem = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{digest[:12]}"
        sidecar = {
            "schema_version": SIDECAR_SCHEMA_VERSION,
            "captured_at": now.isoformat(),
            "source": source,
            "artefact": {
                "kind": FRAME_KIND,
                "media_type": FRAME_MEDIA_TYPE,
                "sha256": digest,
                "bytes": len(image),
            },
            # Kept beside `artefact` for the sidecars already written and for
            # anything still reading the old name. New readers want `artefact`.
            "image": {"sha256": digest, "bytes": len(image)},
            "geometry": _geometry_fields(geometry),
            "ocr": _ocr_fields(payload),
        }
        local_fields = _local_fields(local)
        if local_fields is not None:
            sidecar["local"] = local_fields
        if isinstance(extra, dict):
            sidecar["extra"] = {k: v for k, v in extra.items() if _json_safe(v)}
        encoded = json.dumps(sidecar, sort_keys=True).encode("utf-8")
        if len(encoded) > MAX_SIDECAR_BYTES:
            sidecar["ocr"] = {"truncated": True, "plate": sidecar["ocr"].get("plate")}
            if "local" in sidecar:
                sidecar["local"] = {
                    "truncated": True, "plate": sidecar["local"].get("plate"),
                }
            encoded = json.dumps(sidecar, sort_keys=True).encode("utf-8")
        with self._lock:
            image_path = _write_private(directory, stem + FRAME_SUFFIX, bytes(image))
            try:
                _write_private(directory, stem + SIDECAR_SUFFIX, encoded)
            except Exception:
                image_path.unlink(missing_ok=True)
                raise
            self._records += 1
            self._account(len(image) + len(encoded))
            self._prune_locked(directory)
        return image_path

    def pending(self, limit: int | None = None) -> list["CorpusArtefact"]:
        """The artefacts still on the card, oldest first.

        The stem starts with a UTC timestamp, so sorting the names sorts by
        capture time -- across directories as well as within one, which is
        what makes a single ordering out of the frames at the root and the
        audio clips beneath it. A pair missing either half is not offered:
        half an artefact is not something to upload, and the size backstop
        will eventually reclaim it.

        The subdirectories are walked because the corpus root is not the only
        place artefacts land: the gate's audio clips are written to ``audio``
        beside it, deliberately sharing this pair-and-stem convention so that
        one uploader carries both. A pass that read only the root would leave
        every clip on the card for ever, which is exactly what happened
        between the first capture and this fix.
        """
        artefacts: list[CorpusArtefact] = []
        for directory in self._scan_directories():
            artefacts.extend(self._pairs_in(directory))
        # Sorting on the stem alone, not the path, so a clip recorded before a
        # frame is uploaded before it whichever directory each sits in.
        artefacts.sort(key=lambda artefact: artefact.stem)
        return artefacts if limit is None else artefacts[:max(0, limit)]

    def _scan_directories(self) -> list[Path]:
        """The corpus root and the artefact directories beneath it.

        Breadth first and depth bounded. Hidden names are skipped -- they are
        the half-written temporaries ``_write_private`` leaves -- and so are
        symlinks, which are the one way a bounded walk could still escape the
        corpus.

        ``scandir`` rather than ``iterdir`` because the root holds a frame for
        every OCR request the controller has not shipped yet: the directory
        entry already says whether it is a directory, and asking the SD card
        again for each of those thousands of files, every poll, is a cost with
        nothing to show for it.
        """
        found = [self.directory]
        frontier = [(self.directory, 0)]
        while frontier:
            directory, depth = frontier.pop(0)
            if depth >= MAX_SCAN_DEPTH:
                continue
            try:
                with os.scandir(directory) as entries:
                    children = sorted(
                        entry.name for entry in entries
                        if not entry.name.startswith(".")
                        and entry.is_dir(follow_symlinks=False)
                    )
            except OSError:
                continue
            for name in children:
                child = directory / name
                found.append(child)
                frontier.append((child, depth + 1))
        return found

    def _pairs_in(self, directory: Path) -> list["CorpusArtefact"]:
        """The complete payload/sidecar pairs in one directory.

        Pairing never crosses a directory: a frame at the root and a sidecar
        under ``audio`` that happened to share a stem are two half artefacts,
        not one whole one.
        """
        try:
            entries = list(directory.iterdir())
        except OSError:
            return []
        payloads: dict[str, Path] = {}
        sidecars: set[str] = set()
        for entry in entries:
            name = entry.name
            if name.startswith(".") or not entry.is_file():
                continue
            if name.endswith(SIDECAR_SUFFIX):
                sidecars.add(name[: -len(SIDECAR_SUFFIX)])
            else:
                payloads.setdefault(entry.stem, entry)
        return [
            CorpusArtefact(
                stem=stem,
                payload_path=payloads[stem],
                sidecar_path=directory / (stem + SIDECAR_SUFFIX),
            )
            for stem in sorted(sidecars & payloads.keys())
        ]

    def discard(self, artefact) -> bool:
        """Drop one artefact that is safely in the cloud.

        Takes the :class:`CorpusArtefact` ``pending`` handed out, or a bare
        stem for one written at the root. The artefact is what says *which
        directory*: a clip lives under ``audio`` and deleting the root path of
        its stem would delete nothing, leaving the uploader to ship the same
        clip on every pass for ever.

        Called only after the cloud has confirmed the artefact is stored, so
        this is the step that turns the card from an archive into a buffer.
        Accounting happens under the same lock ``record`` uses: a stale byte
        total would make the backstop prune live pairs it does not need to.
        """
        directory, stem = self._locate(artefact)
        freed = 0
        removed = False
        with self._lock:
            for suffix in (SIDECAR_SUFFIX, *self._payload_suffixes(directory, stem)):
                path = directory / (stem + suffix)
                try:
                    size = path.stat().st_size
                    path.unlink()
                except OSError:
                    continue
                freed += size
                removed = True
            if removed:
                self._discarded += 1
                # Only the root is accounted -- see `_account` -- so only bytes
                # freed at the root come off the total. Subtracting an audio
                # clip that was never added would walk the total down towards
                # zero and quietly disable the size backstop.
                if self._total_bytes is not None and directory == self.directory:
                    self._total_bytes = max(0, self._total_bytes - freed)
        return removed

    def _locate(self, artefact) -> tuple[Path, str]:
        """The directory and stem of an artefact, however it was named."""
        stem = getattr(artefact, "stem", artefact)
        payload_path = getattr(artefact, "payload_path", None)
        directory = (
            Path(payload_path).parent if payload_path is not None else self.directory
        )
        return directory, str(stem)

    def _payload_suffixes(self, directory: Path, stem: str) -> tuple[str, ...]:
        """Every non-sidecar suffix written under ``stem``.

        Read from the directory rather than assumed, so an audio artefact is
        discarded by the same call that discards a frame.
        """
        try:
            return tuple(
                path.suffix for path in directory.glob(stem + ".*")
                if path.is_file() and path.suffix != SIDECAR_SUFFIX
            )
        except OSError:
            return (FRAME_SUFFIX,)

    def status(self) -> dict:
        return {
            "directory": str(self.directory),
            "max_bytes": self._max_bytes,
            "bytes": self._total_bytes,
            "records": self._records,
            "failures": self._failures,
            "pruned": self._pruned,
            "discarded": self._discarded,
        }

    # -- internals --------------------------------------------------------
    def _ensure_directory(self) -> Path:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        return self.directory

    def _account(self, added: int) -> None:
        """The bytes this corpus wrote, which is the root and only the root.

        ``pending`` reads the subdirectories too, but reading is not owning.
        The audio clips under ``audio`` are written and bounded by
        ``AudioClipStore``, which has its own byte cap and its own retention
        window; counting them here would give one directory two pruners with
        different ideas about what to delete, and the one that does not know
        about the upload would be free to delete a clip on its way to R2.
        """
        if self._total_bytes is None:
            self._total_bytes = sum(
                entry.stat().st_size for entry in self.directory.iterdir() if entry.is_file()
            )
        else:
            self._total_bytes += added

    def _prune_locked(self, directory: Path) -> None:
        """The backstop, over the frames at the root and nothing else.

        Scoped to what ``_account`` counts, and to the two suffixes this class
        writes, so the subdirectories ``pending`` now walks are read from and
        never deleted from here.
        """
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


KEEP_LOCAL_KEYS = (
    "status", "plate", "score", "mean_score", "box", "latency_ms", "candidates",
)


def _local_fields(local) -> dict | None:
    """The on-device read, kept beside ``ocr`` and bounded the same way.

    The local answer is a second pseudo-label, not truth, so it is stored
    next to the cloud one rather than merged into it: a review pass can then
    see where the two disagreed on the very same frame.
    """
    if not isinstance(local, dict):
        return None
    return {key: local[key] for key in KEEP_LOCAL_KEYS
            if key in local and _json_safe(local[key])} or None


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
    if isinstance(value, float) and not isfinite(value):
        # json.dumps would emit NaN/Infinity, which no strict JSON reader will
        # parse. A sidecar nothing can read is worse than a missing field, and
        # a recogniser confidence is exactly the kind of float that can arrive
        # non-finite.
        return False
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, (list, tuple)):
        return len(value) <= 64 and all(_json_safe(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return len(value) <= 64 and all(isinstance(k, str) and _json_safe(v, depth + 1) for k, v in value.items())
    return False
