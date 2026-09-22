import csv
import logging
import os
import tempfile
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock

from .cloud_health import TransitionLogger
from .matching import normalise_plate

LOGGER = logging.getLogger(__name__)


MAX_PLATE_SNAPSHOT_BYTES = 256 * 1024
MAX_PLATE_ROWS = 1000
MAX_NORMALISED_PLATE_LENGTH = 16

#: The longest the CSV's mtime may lag behind the last successful refresh.
#:
#: The snapshot is refreshed every 30 s and the plate list changes a few times
#: a year, so almost every refresh has nothing to write. Skipping those writes
#: leaves one loose end: ``reload_local`` dates the snapshot by the file's
#: mtime, which is what the staleness rule is measured from *after a restart*.
#: An mtime frozen at the last real change would make a perfectly fresh
#: snapshot look fourteen days old. So the inode is touched -- no data blocks,
#: no fsync, no rename -- once it has fallen this far behind, which is 2,880
#: rewrites a day turned into 24 metadata updates.
MTIME_TOUCH_INTERVAL = timedelta(hours=1)
#: ...and never more than this share of the staleness limit, so a cache
#: configured to expire in minutes is not left an hour of drift to fall into.
MTIME_TOUCH_FRACTION = 0.1


class AuthorisationError(RuntimeError):
    pass


class AuthorisedPlateCache:
    """Reload an atomically replaced CSV without discarding a known-good snapshot."""

    def __init__(self, path: Path, *, max_staleness: timedelta | None = None, clock=None):
        self._path = Path(path)
        self._plates: tuple[str, ...] | None = None
        self._version = None
        self._refreshed_at: datetime | None = None
        self._last_error: str | None = None
        self._max_staleness = max_staleness
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = Lock()
        self.reload_local()

    def get(self) -> tuple[str, ...]:
        with self._lock:
            if self._plates is None:
                raise AuthorisationError("no valid authorised-plates snapshot")
            if (self._max_staleness is not None and self._refreshed_at is not None
                    and _snapshot_is_stale(
                        self._clock(), self._refreshed_at, self._max_staleness
                    )):
                raise AuthorisationError("authorised-plates snapshot is stale")
            return self._plates

    def reload_local(self) -> bool:
        with self._lock:
            try:
                version = self._file_version()
                refreshed = self._read_complete_file()
                if self._file_version() != version:
                    raise ValueError("authorised plates CSV changed during refresh")
            except (OSError, csv.Error, ValueError) as error:
                self._last_error = str(error)
                return False
            self._plates = refreshed
            self._version = version
            modified_at = datetime.fromtimestamp(self._path.stat().st_mtime, timezone.utc)
            if self._refreshed_at is None or modified_at > self._refreshed_at:
                self._refreshed_at = modified_at
            self._last_error = None
            return True

    def replace(self, plates) -> None:
        """Record a refreshed snapshot, writing the CSV only if it changed.

        The refresh runs every 30 s and the list itself changes a few times a
        year, so the write below -- temporary file, fsync, rename, fsync of the
        directory -- used to run about 2,880 times a day to produce a file
        identical to the one already there. An unchanged snapshot now skips it
        and keeps the bookkeeping only: the refresh still happened, and
        ``_refreshed_at`` still moves, so staleness is measured from the last
        successful *refresh* and not from the last write.
        """
        normalized = tuple(sorted(filter(None, (normalise_plate(value) for value in plates))))
        if self._record_unchanged(normalized):
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=self._path.parent, prefix=f".{self._path.name}.", text=True
        )
        try:
            with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as target:
                writer = csv.writer(target)
                writer.writerow(("plate",))
                writer.writerows((plate,) for plate in normalized)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self._path)
            _fsync_directory(self._path.parent)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        with self._lock:
            self._plates = normalized
            self._version = self._file_version()
            self._refreshed_at = self._clock()
            self._last_error = None

    def _record_unchanged(self, normalized: tuple[str, ...]) -> bool:
        """Book a refresh that matched the snapshot already on disk.

        Content, never mtime: the file is left exactly as it is unless somebody
        outside this process has rewritten it since we last read it, in which
        case ours is written back over theirs as it always was.
        """
        with self._lock:
            if self._plates is None or self._plates != normalized:
                return False
            try:
                if self._version is None or self._file_version() != self._version:
                    return False
            except OSError:
                return False
            self._refreshed_at = self._clock()
            self._last_error = None
            self._touch()
            return True

    def _touch(self) -> None:
        """Keep the CSV's mtime within ``MTIME_TOUCH_INTERVAL`` of this refresh.

        Only the inode is written, and only when the mtime has actually fallen
        behind. A failure here is not a failed refresh -- the snapshot in
        memory is good and the file is unchanged -- so it is logged and
        swallowed. Caller holds the lock.
        """
        now = self._clock()
        try:
            modified_at = datetime.fromtimestamp(self._path.stat().st_mtime, timezone.utc)
            if now - modified_at <= self._touch_interval():
                return
            os.utime(self._path, (now.timestamp(), now.timestamp()))
            self._version = self._file_version()
        except (OSError, OverflowError, ValueError, TypeError) as error:
            LOGGER.debug("stage=plates_mtime_touch_failed error=%s", error)

    def _touch_interval(self) -> timedelta:
        if self._max_staleness is None:
            return MTIME_TOUCH_INTERVAL
        return min(MTIME_TOUCH_INTERVAL, self._max_staleness * MTIME_TOUCH_FRACTION)

    def mark_refresh_error(self, error: Exception) -> None:
        with self._lock:
            self._last_error = str(error)

    def status(self) -> dict:
        with self._lock:
            stale = (
                self._max_staleness is not None and self._refreshed_at is not None
                and _snapshot_is_stale(
                    self._clock(), self._refreshed_at, self._max_staleness
                )
            )
            return {
                "available": self._plates is not None,
                "stale": stale,
                "refreshed_at": self._refreshed_at.isoformat() if self._refreshed_at else None,
                "last_error": self._last_error,
            }

    def _file_version(self):
        stat = self._path.stat()
        return stat.st_ino, stat.st_size, stat.st_mtime_ns

    def _read_complete_file(self) -> tuple[str, ...]:
        with self._path.open(newline="", encoding="utf-8") as source:
            rows = list(csv.reader(source))
        if not rows or not rows[0] or rows[0][0].strip().lower() != "plate":
            raise ValueError("authorised plates CSV has no plate header")
        plates = []
        width = len(rows[0])
        for row in rows[1:]:
            if not row or not row[0].strip():
                continue
            if len(row) != width:
                raise ValueError("authorised plates CSV has malformed row")
            plates.append(row[0].strip())
        return tuple(plates)


def _snapshot_is_stale(now: datetime, refreshed_at: datetime,
                       max_staleness: timedelta) -> bool:
    try:
        age = now - refreshed_at
    except (TypeError, ValueError):
        return True
    return age < timedelta(0) or age > max_staleness


class CloudflarePlateFetcher:
    def __init__(self, client, controller_id):
        self.client = client
        self._controller_id = controller_id

    def __call__(self) -> list[dict]:
        payload = self.client.get_json(
            "/api/controller/plates?" + urlencode({"controller_id": self._controller_id}),
            max_response_bytes=MAX_PLATE_SNAPSHOT_BYTES,
        )
        if isinstance(payload, dict) and payload.get("controller_id") == self._controller_id:
            rows = payload.get("plates")
        else:
            raise AuthorisationError("Cloudflare plates returned a snapshot for another controller")
        if not isinstance(rows, list) or len(rows) > MAX_PLATE_ROWS or any(
            not isinstance(row, dict) or not isinstance(row.get("plate"), str)
            for row in rows
        ):
            raise AuthorisationError("Cloudflare plates returned invalid JSON")
        if any(
            len(normalise_plate(row["plate"])) > MAX_NORMALISED_PLATE_LENGTH
            for row in rows
        ):
            raise AuthorisationError("Cloudflare plates exceeded the normalized plate length limit")
        return rows


class AuthorisationRefreshWorker:
    def __init__(self, cache: AuthorisedPlateCache, fetch, poll_interval: float = 30.0, *,
                 health: TransitionLogger | None = None):
        self._cache = cache
        self._fetch = fetch
        self._poll_interval = poll_interval
        self._health = health or TransitionLogger(LOGGER, "plates_refresh")

    @property
    def consecutive_failures(self) -> int:
        """Failed plate refreshes since the last success, for the heartbeat."""
        return self._health.consecutive_failures

    def run_once(self) -> bool:
        try:
            rows = self._fetch()
            if not isinstance(rows, list) or any(
                not isinstance(row, dict) or not isinstance(row.get("plate"), str)
                for row in rows
            ):
                raise AuthorisationError("authorised plate refresh was malformed")
            self._cache.replace(row["plate"] for row in rows)
        except Exception as error:
            self._cache.mark_refresh_error(error)
            self._health.failure(error)
            return False
        self._health.success()
        return True

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
