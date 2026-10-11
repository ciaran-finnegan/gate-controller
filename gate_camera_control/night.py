"""Night mode for the IR lease: switch the ISP so the IR LEDs actually light, and put it back.

The app's Night vision tile leases the IR illuminator (`IrLights` `Auto`). On
the fitted RLC-811A that did nothing at night, because the ISP has been pinned to
`dayNight` `Color` since 2026-09-07 (the PIR floodlight lights plates in colour;
docs/reviews/2026-09-06-camera-night-configuration.md). In `Color` the camera
never swings its IR cut filter out and never lights its IR LEDs, so the leased
IR was invisible and the picture stayed black.

This module makes the lease mean what the tile says. While an IR lease is on,
`Isp.dayNight` is `Black&White`; when it ends -- expiry, a requested Off, or the
next start after a crash -- the whole `Isp` block is written back **exactly** as
it was read before the lease, and only then is IR switched off. The discipline
is the one invariant 7 (docs/invariants.md) holds the lens to:

* the camera's own block is read first and written to durable storage before
  anything changes, so a restart part-way through still knows what to put back;
* the only value ever written that was not in that snapshot is `dayNight`
  itself, and only for the lease; the restore is the snapshot, untouched;
* a restore that fails is retried with backoff for the life of the process,
  journaled at error priority, and reported in the state the heartbeat carries,
  so the camera is never left in night mode silently;
* nothing is written on the strength of a block that could not be read -- a
  camera whose ISP cannot be read, or a record that cannot be trusted, gets no
  mode change and the caller gets an error it can show.

Why only `dayNight`, and not the exposure: the 2026-09-06 review measured the
deployed `Manual` 1/250 s at gain 16 *with IR on* (its row 13): brightness
0.1159, "the veiling haze across the driveway is gone and the fence and trees
are clean". That is a dim but usable IR picture, and it is the exposure the
plate reader is tuned to, so it stays. Arrivals during a lease are read from
that IR picture, which has not been tested against a car.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time

from .atomic import atomic_write
from .reolink import NIGHT_DAY_NIGHT, CameraError, usable_isp_block

RESTORE_RETRY_SECONDS = (5.0, 10.0, 20.0, 40.0, 60.0)
MAX_RECORD_FILE_BYTES = 16 * 1024
_RECORD_KEYS = frozenset({"isp", "set_at"})


class NightModeController:
    """The ISP half of the IR lease: snapshot, switch to night, restore exactly.

    Every camera call here is made from inside the IR controller's writer
    lock, so a snapshot, a switch and a restore never interleave with each
    other or with an IR write. The controller itself holds only its own
    bookkeeping lock.
    """

    def __init__(self, client, *, record_path=None, clock=time.time, journal=None,
                 night_mode: str = NIGHT_DAY_NIGHT, retry_seconds=RESTORE_RETRY_SECONDS):
        self._client = client
        self._record_path = None if record_path is None else os.fspath(record_path)
        self._clock = clock
        self._journal = journal or (lambda *_arguments, **_keywords: None)
        self._night_mode = night_mode
        self._retry_seconds = tuple(float(delay) for delay in retry_seconds)
        self._lock = threading.Lock()
        self._restore_failed = False
        self._restore_attempts = 0
        self._next_restore_at = None
        self._last_error = None
        self._record, self._record_corrupt = self._load_record()
        if self._record_corrupt:
            # The camera may be in night mode and the only copy of what it
            # was before cannot be read. Nothing is written on its strength;
            # a person has to look (docs/camera-control.md, Night vision).
            self._journal("isp_record_corrupt", level="error")

    # -- state -------------------------------------------------------------
    @property
    def active(self) -> bool:
        """True while a snapshot is outstanding, i.e. the camera may be in night mode."""
        with self._lock:
            return self._record is not None or self._record_corrupt

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "enabled": True,
                "active": self._record is not None or self._record_corrupt,
                "restore_failed": self._restore_failed or self._record_corrupt,
                "last_error": "record_corrupt" if self._record_corrupt else self._last_error,
            }

    # -- entering ------------------------------------------------------------
    def prepare(self, *, key=None) -> None:
        """Read the ISP and persist it, before anything on the camera changes.

        A second call while a snapshot is outstanding -- a lease extended from
        the app -- is a no-op: the snapshot is the picture *before* the lease,
        and reading the camera now would capture the night values and make
        them what the lease "restores" to.
        """
        with self._lock:
            if self._record is not None:
                return
            corrupt = self._record_corrupt
        if corrupt:
            raise CameraError("the night-mode record is unreadable; restore the ISP by hand")
        block, modes = self._client.isp()
        if modes is not None and self._night_mode not in modes:
            self._journal("isp_night", outcome="unsupported", key=key, level="error",
                          day_night=block.get("dayNight"))
            raise CameraError("the camera does not offer the night dayNight mode")
        record = {"isp": block, "set_at": float(self._clock())}
        try:
            self._write_record(record)
        except OSError as error:
            self._journal("isp_night", outcome="record_unwritable", key=key, level="error")
            raise CameraError("the night-mode record could not be written") from error
        with self._lock:
            self._record = record
        self._journal("isp_snapshot", day_night=block.get("dayNight"), key=key)

    def apply(self, *, key=None) -> None:
        """Write the snapshot back with `dayNight` switched to night. Raises on failure.

        The snapshot stays on disk either way: a write the camera did not
        confirm may still have landed, and the record is what puts it back.
        """
        with self._lock:
            record = self._record
        if record is None:
            raise CameraError("no ISP snapshot to switch from")
        night = json.loads(json.dumps(record["isp"]))
        night["dayNight"] = self._night_mode
        try:
            self._client.set_isp(night)
        except CameraError as error:
            with self._lock:
                self._last_error = error.code
            self._journal("isp_night", outcome=error.code, key=key)
            raise
        with self._lock:
            self._last_error = None
        self._journal("isp_night", outcome="completed", day_night=self._night_mode, key=key)

    # -- leaving -------------------------------------------------------------
    def restore(self, *, reason: str, key=None) -> bool:
        """Write the snapshot back exactly. True when a restore was written.

        No snapshot means nothing to put back -- an IR-only lease, or one
        already restored -- and the camera is not touched. A failed restore
        keeps the record, schedules a retry and raises, so the caller's own
        revert is recorded as failed too and IR is not switched off ahead of
        the picture.
        """
        with self._lock:
            record = self._record
        if record is None:
            return False
        try:
            self._client.set_isp(record["isp"])
        except CameraError as error:
            with self._lock:
                self._last_error = error.code
                self._restore_failed = True
                self._restore_attempts += 1
                backoff = self._retry_seconds[
                    min(self._restore_attempts, len(self._retry_seconds)) - 1
                ]
                self._next_restore_at = self._clock() + backoff
                attempt = self._restore_attempts
            self._journal("isp_restore", outcome=error.code, reason=reason, key=key,
                          attempt=attempt, retry_after=int(backoff), level="error")
            raise
        with self._lock:
            self._record = None
            self._restore_failed = False
            self._restore_attempts = 0
            self._next_restore_at = None
            self._last_error = None
        self._remove_record()
        self._journal("isp_restore", outcome="completed", reason=reason, key=key,
                      day_night=record["isp"].get("dayNight"))
        return True

    def restore_on_start(self) -> None:
        """Put the ISP back if the previous process died with a lease outstanding.

        Called before the IR startup revert, so the picture is restored before
        the illuminator goes off, in the same order a normal revert takes. A
        failure here is retried through `restore_due` / `restore`.
        """
        with self._lock:
            outstanding = self._record is not None
        if not outstanding:
            return
        self._journal("startup_isp_restore", level="warning")
        try:
            self.restore(reason="startup")
        except CameraError:
            pass

    def restore_due(self) -> bool:
        """True when a failed restore's backoff has elapsed and it should be retried."""
        with self._lock:
            return (self._record is not None and self._restore_failed
                    and self._next_restore_at is not None
                    and self._clock() >= self._next_restore_at)

    # -- the durable record ---------------------------------------------------
    def _write_record(self, record: dict) -> None:
        if self._record_path is None:
            return
        body = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        atomic_write(self._record_path, body, 0o600)

    def _remove_record(self) -> None:
        if self._record_path is None:
            return
        try:
            os.unlink(self._record_path)
        except FileNotFoundError:
            pass
        except OSError:
            self._journal("isp_record_unremovable", level="warning")

    def _load_record(self):
        """``(record, corrupt)``: no record, a usable one, or an unusable one.

        An unusable record is kept on disk as evidence and is never written
        back: the block it holds cannot be trusted to be the camera's.
        """
        if self._record_path is None:
            return None, False
        flags = os.O_RDONLY | os.O_NONBLOCK
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._record_path, flags)
        except FileNotFoundError:
            return None, False
        except OSError:
            return None, True
        try:
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode)
                    or not 0 < metadata.st_size <= MAX_RECORD_FILE_BYTES):
                return None, True
            body = os.read(descriptor, metadata.st_size)
        except OSError:
            return None, True
        finally:
            os.close(descriptor)
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None, True
        if not isinstance(decoded, dict) or set(decoded) != _RECORD_KEYS:
            return None, True
        set_at = decoded["set_at"]
        if isinstance(set_at, bool) or not isinstance(set_at, (int, float)):
            return None, True
        # The same shape check the client applies before it will write a block.
        if not usable_isp_block(decoded["isp"]):
            return None, True
        return {"isp": decoded["isp"], "set_at": float(set_at)}, False
