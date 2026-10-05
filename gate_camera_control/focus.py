"""Re-run the camera's autofocus by nudging the zoom one step and back.

Overnight from 3 to 4 October 2026 the RLC-811A lost focus with autofocus
enabled: the focus motor ended at 86 (it had been 80) on zoom position 2, every
daylight frame after it was soft, and plate reads collapsed. Stepping the zoom
from 2 to 3 and back to 2 in daylight made the camera's own autofocus run again;
it settled at 78 and the picture was sharp. This module does exactly that and
nothing more:

* it reads ``GetZoomFocus``;
* it moves the zoom one position (up, or down at the long end) and waits;
* it moves the zoom back to **exactly** the position it read, waits for the
  autofocus, and reads back to prove the zoom is where it started.

It never writes a focus position -- ``ReolinkClient.zoom_to`` can only send
``ZoomPos`` -- and it never leaves the zoom changed: the position it read is
written to durable storage *before* the lens moves, a failed return is retried
in the request and then in the background for the life of the process, and a
service that restarts with that record still on disk puts the zoom back before
it answers anything. That is the same shape as the IR lease in ``ir.py``.

This is the narrow exception to "nothing on the Pi touches the lens" that
``scripts/reolink/configure-rlc811a.py`` keeps: that script still sends no
``ZoomFocus`` command of any kind, and nothing here can change where the lens
ends up.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from collections import deque
from datetime import datetime, timezone

from .atomic import atomic_write
from .reolink import ZOOM_MAX, ZOOM_MIN, CameraError


#: Seconds between the step and the return, and between the return and the
#: read-back: the manual fix on 2026-10-05 waited about 7 s each way and the
#: autofocus had settled.
SETTLE_SECONDS = 7.0
#: Service-wide floor between two nudges, whoever asks. The controller's own
#: schedule is far slower (one a day, plus at most one every 6 h when frames go
#: soft); this only stops a caller that is not the controller from wearing the
#: zoom motor.
MIN_INTERVAL_SECONDS = 600.0
#: Service-wide cap in any rolling 24 hours, for the same reason.
MAX_PER_DAY = 6
DAY_SECONDS = 86_400.0
#: In-request retries of the return. The last one outlasts the client's 60 s
#: circuit breaker, so a camera that answered 502 once still gets a real try.
RETURN_RETRY_DELAYS = (5.0, 20.0, 65.0)
#: After the request has given up, the background worker keeps trying.
BACKGROUND_RETURN_DELAYS = (5.0, 10.0, 20.0, 40.0, 60.0)
REASONS = ("daily", "detection", "manual")
MAX_RECORD_FILE_BYTES = 1024
_RECORD_KEYS = frozenset({"zoom", "focus", "started_at"})


class RefocusRefused(Exception):
    """Refused before the lens was touched; ``status`` is the HTTP answer."""

    def __init__(self, status: int, error: str, *, reason=None, retry_after=None):
        super().__init__(error)
        self.status = int(status)
        self.error = error
        self.reason = reason
        self.retry_after = None if retry_after is None else max(1, int(retry_after))


class ZoomReturnFailed(CameraError):
    """The zoom could not be proven back at its original position."""

    code = "zoom_return_failed"

    def __init__(self, expected: int, observed):
        super().__init__("zoom_return_failed")
        self.expected = expected
        self.observed = observed


class RefocusController:
    """One zoom nudge at a time, always returned to where it started."""

    def __init__(self, client, *, record_path=None, clock=time.time, sleep=time.sleep,
                 journal=None, settle_seconds: float = SETTLE_SECONDS,
                 min_interval_seconds: float = MIN_INTERVAL_SECONDS,
                 max_per_day: int = MAX_PER_DAY,
                 return_retry_delays=RETURN_RETRY_DELAYS,
                 background_return_delays=BACKGROUND_RETURN_DELAYS):
        self._client = client
        self._record_path = None if record_path is None else os.fspath(record_path)
        self._clock = clock
        self._sleep = sleep
        self._journal = journal or (lambda *_arguments, **_keywords: None)
        self._settle = float(settle_seconds)
        self._min_interval = float(min_interval_seconds)
        self._max_per_day = int(max_per_day)
        self._return_retry_delays = tuple(float(delay) for delay in return_retry_delays)
        self._background_delays = tuple(float(delay) for delay in background_return_delays)
        # Held for the whole nudge, and by the background return, so the lens
        # only ever has one owner.
        self._run_lock = threading.Lock()
        self._lock = threading.Lock()
        self._attempts: deque[float] = deque()
        self._pending_return: int | None = None
        self._return_failures = 0
        self._next_return_at: float | None = None

    # -- startup ---------------------------------------------------------
    def restore_on_start(self) -> None:
        """Put the zoom back if the previous process died part-way through a nudge.

        Only a record this service wrote can move the lens here: no record
        means nothing was touched, and the lens is left exactly where it is.
        """
        record, corrupt = self._load_record()
        if corrupt:
            # The position it named cannot be trusted, so nothing is written on
            # its strength. Said loudly, because a person may need to look.
            self._journal("refocus_record_corrupt", level="warning")
            self._remove_record()
            return
        if record is None:
            return
        with self._lock:
            self._pending_return = record["zoom"]
            self._return_failures = 0
            self._next_return_at = self._clock()
        self._journal("startup_zoom_return", zoom=record["zoom"], level="warning")
        self.run_due_return()

    # -- the nudge ---------------------------------------------------------
    def refocus(self, reason: str = "manual") -> dict:
        if reason not in REASONS:
            raise ValueError("invalid_request")
        if not self._run_lock.acquire(blocking=False):
            raise RefocusRefused(409, "refocus_busy", reason="in_progress")
        try:
            self._admit(reason)
            before = self._client.zoom_focus()
            original = before["zoom"]
            target = original + 1 if original < ZOOM_MAX else original - 1
            if not ZOOM_MIN <= target <= ZOOM_MAX:
                raise CameraError("no neighbouring zoom position")
            # Written before the lens moves: if this process dies between the
            # step and the return, the next start still knows where to go.
            # A record that cannot be written means the step is not taken.
            try:
                self._write_record({
                    "zoom": original, "focus": before["focus"], "started_at": self._clock(),
                })
            except OSError as error:
                self._journal("refocus", outcome="record_unwritable", reason=reason,
                              level="warning")
                raise CameraError("refocus record could not be written") from error
            step_error = None
            try:
                self._client.zoom_to(target)
            except CameraError as error:
                # The camera may have moved anyway, so the return below runs
                # regardless and decides from what it reads.
                step_error = error
            self._sleep(self._settle)
            after, attempts = self._return_to(original, reason=reason)
            self._remove_record()
            status = "completed" if step_error is None else "step_failed"
            self._journal(
                "refocus", outcome=status, reason=reason,
                zoom_before=original, zoom_step=target, zoom_after=after["zoom"],
                focus_before=before["focus"], focus_after=after["focus"],
                return_attempts=attempts,
            )
            return {
                "observed_at": _isoformat(self._clock()),
                "status": status,
                "reason": reason,
                "zoom": {"before": original, "stepped_to": target, "after": after["zoom"]},
                "focus": {"before": before["focus"], "after": after["focus"]},
                "return_attempts": attempts,
            }
        finally:
            self._run_lock.release()

    def _admit(self, reason: str) -> None:
        now = self._clock()
        with self._lock:
            if self._pending_return is not None:
                raise RefocusRefused(409, "refocus_busy", reason="zoom_return_pending")
            while self._attempts and now - self._attempts[0] >= DAY_SECONDS:
                self._attempts.popleft()
            if len(self._attempts) >= self._max_per_day:
                retry_after = self._attempts[0] + DAY_SECONDS - now
                self._journal("refocus_rate_limited", reason=reason,
                              retry_after=max(1, int(retry_after)))
                raise RefocusRefused(429, "rate_limited", retry_after=retry_after)
            if self._attempts and now - self._attempts[-1] < self._min_interval:
                retry_after = self._min_interval - (now - self._attempts[-1])
                self._journal("refocus_rate_limited", reason=reason,
                              retry_after=max(1, int(retry_after)))
                raise RefocusRefused(429, "rate_limited", retry_after=retry_after)
            # Counted from here, whether or not the camera then answers: any
            # call that reaches the camera spends the budget.
            self._attempts.append(now)

    def _return_to(self, original: int, *, reason: str):
        """Drive the zoom back to ``original`` and prove it. Raises if it cannot."""
        observed = None
        delays = (0.0,) + self._return_retry_delays
        for attempt, delay in enumerate(delays, start=1):
            if delay:
                self._sleep(delay)
            try:
                current = self._return_once(original)
            except CameraError as error:
                self._journal("refocus_return", attempt=attempt, outcome=error.code,
                              expected=original, reason=reason, level="warning")
                continue
            observed = current["zoom"]
            if observed == original:
                return current, attempt
            self._journal("refocus_return", attempt=attempt, outcome="mismatch",
                          expected=original, observed=observed, reason=reason,
                          level="warning")
        # Not proven back. The record stays on disk, the background worker
        # keeps trying, and no further nudge is admitted until it succeeds.
        with self._lock:
            self._pending_return = original
            self._return_failures = 0
            self._next_return_at = self._clock() + self._background_delays[0]
        self._journal("refocus_return_failed", expected=original,
                      observed="unknown" if observed is None else observed,
                      reason=reason, level="error")
        raise ZoomReturnFailed(original, observed)

    def _return_once(self, original: int) -> dict:
        current = self._client.zoom_focus()
        if current["zoom"] == original:
            return current
        self._client.zoom_to(original)
        # Long enough for the autofocus the return triggers to settle, so the
        # focus read back is the one the camera will keep.
        self._sleep(self._settle)
        return self._client.zoom_focus()

    # -- background return -----------------------------------------------
    def run_due_return(self) -> bool:
        """Retry an outstanding return when it is due. True when one was tried."""
        with self._lock:
            original = self._pending_return
            due = (original is not None and self._next_return_at is not None
                   and self._clock() >= self._next_return_at)
        if not due:
            return False
        if not self._run_lock.acquire(blocking=False):
            return False
        try:
            try:
                current = self._return_once(original)
                outcome = "completed" if current["zoom"] == original else "mismatch"
                observed = current["zoom"]
            except CameraError as error:
                outcome, observed = error.code, None
            with self._lock:
                if outcome == "completed":
                    self._pending_return = None
                    self._next_return_at = None
                    self._return_failures = 0
                else:
                    self._return_failures += 1
                    index = min(self._return_failures, len(self._background_delays) - 1)
                    self._next_return_at = self._clock() + self._background_delays[index]
                attempt = self._return_failures
            if outcome == "completed":
                self._remove_record()
                self._journal("refocus_return", outcome="completed", background=True,
                              expected=original, observed=observed)
            else:
                self._journal("refocus_return", outcome=outcome, background=True,
                              attempt=attempt, expected=original,
                              observed="unknown" if observed is None else observed,
                              level="error")
            return True
        finally:
            self._run_lock.release()

    def return_pending(self) -> bool:
        with self._lock:
            return self._pending_return is not None

    # -- the durable record ----------------------------------------------
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
            self._journal("refocus_record_unremovable", level="warning")

    def _load_record(self):
        """``(record, corrupt)``: no record, a usable one, or an unusable one."""
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
        zoom = decoded["zoom"]
        if (isinstance(zoom, bool) or not isinstance(zoom, int)
                or not ZOOM_MIN <= zoom <= ZOOM_MAX):
            return None, True
        return {"zoom": zoom}, False


class ZoomReturnWorker:
    """Background thread that retries a zoom return the request could not finish."""

    def __init__(self, controller: RefocusController, *, interval_seconds: float = 1.0):
        self._controller = controller
        self._interval_seconds = float(interval_seconds)
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="camera-zoom-return", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self._interval_seconds + 1)

    def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                self._controller.run_due_return()
            except Exception:
                # Recorded on the controller and retried; never fatal.
                pass
            self._stopped.wait(self._interval_seconds)


def _isoformat(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(
        float(epoch_seconds), tz=timezone.utc
    ).replace(microsecond=0).isoformat()
