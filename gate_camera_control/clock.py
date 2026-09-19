"""Keep the camera's clock on the Pi's, so its alarm timestamps are trusted.

The controller refuses a webhook whose alarm time sits too far from its own
clock. On 2026-09-16 every webhook was refused for four days because the
NVR that records the camera kept pushing it a clock two hours out, and the
camera's own NTP was overwritten with it. The Pi is NTP-synced and already
talks to the camera, so it is the natural owner of the camera's time: once
an hour it reads ``GetTime`` and, when the display is more than a few seconds
from UTC, writes ``SetTime`` with UTC.

The reconciler only corrects a camera configured to *display* UTC
(``timeZone 0`` with DST disabled). ``SetTime`` takes the displayed time and
the firmware shifts it by the DST hour when that flag changes in the same
write, so any other configuration is reported as skew but left alone: it is
the operator's setting, and a wrong correction is worse than none.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from .reolink import CameraError


DEFAULT_TOLERANCE_SECONDS = 5.0
DEFAULT_INTERVAL_SECONDS = 3600.0
DEFAULT_RETRY_SECONDS = 300.0
OUTCOMES = (
    "not_checked", "ok", "corrected", "skipped_config", "camera_busy",
    "camera_unreachable", "camera_error", "disabled",
)
MAX_REPORTED_SKEW_SECONDS = 10_000_000


class ClockReconciler:
    """Hourly read-back-and-correct of the camera clock against the Pi's."""

    def __init__(self, client, *, enabled: bool = True,
                 tolerance_seconds: float = DEFAULT_TOLERANCE_SECONDS,
                 interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
                 retry_seconds: float = DEFAULT_RETRY_SECONDS,
                 clock=time.time, utc_now=None, journal=None):
        self._client = client
        self._enabled = bool(enabled)
        self._tolerance = float(tolerance_seconds)
        self._interval = float(interval_seconds)
        self._retry = float(retry_seconds)
        self._clock = clock
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self._journal = journal or (lambda *_a, **_k: None)
        self._lock = threading.Lock()
        self._next_at = None if self._enabled else float("inf")
        self._outcome = "disabled" if not self._enabled else "not_checked"
        self._skew_seconds: float | None = None
        self._checked_at: float | None = None
        self._corrections = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    def snapshot(self) -> dict:
        """Bounded, nonsecret clock block for the published state."""
        with self._lock:
            skew = self._skew_seconds
            checked_at = self._checked_at
            outcome = self._outcome
            corrections = self._corrections
        if skew is not None:
            skew = max(-MAX_REPORTED_SKEW_SECONDS, min(MAX_REPORTED_SKEW_SECONDS, int(round(skew))))
        return {
            "synced": outcome in ("ok", "corrected"),
            "outcome": outcome if outcome in OUTCOMES else "camera_error",
            "skew_seconds": skew,
            "checked_at": None if checked_at is None else _isoformat(checked_at),
            "corrections": corrections,
        }

    def run_due(self) -> bool:
        """Reconcile when the schedule says so. Returns True when it ran."""
        with self._lock:
            due = self._next_at is None or self._clock() >= self._next_at
        if not due:
            return False
        self.reconcile()
        return True

    def reconcile(self) -> dict:
        if not self._enabled:
            return self.snapshot()
        outcome, skew = "camera_error", None
        try:
            outcome, skew = self._reconcile()
        except CameraError as error:
            outcome = _error_outcome(error)
        except Exception:
            outcome = "camera_error"
        now = self._clock()
        with self._lock:
            self._outcome = outcome
            self._skew_seconds = skew
            self._checked_at = now
            if outcome == "corrected":
                self._corrections += 1
            # A camera that would not answer is asked again sooner than the
            # hourly pass, but never in a tight loop: the client's own login
            # throttle and breaker sit underneath this.
            self._next_at = now + (
                self._interval if outcome in ("ok", "corrected", "skipped_config")
                else self._retry
            )
        self._journal(
            "clock_reconcile", outcome=outcome,
            skew_seconds="unknown" if skew is None else f"{skew:+.0f}",
        )
        return self.snapshot()

    def _reconcile(self) -> tuple[str, float | None]:
        state = self._client.clock_state()
        fields, dst = state.get("Time"), state.get("Dst")
        if not isinstance(fields, dict) or not isinstance(dst, dict):
            raise CameraError("camera reported an unusable clock")
        displayed = _displayed(fields)
        now = self._utc_now()
        skew = (displayed - now.replace(tzinfo=None)).total_seconds()
        if int(fields.get("timeZone", -1)) != 0:
            # A zone somebody chose. The skew above includes their offset, so
            # it is not an error and this must not "correct" it away.
            return "skipped_config", skew
        # timeZone is already UTC and only the DST flag has moved, which is not
        # a choice anybody makes: it is the state this camera keeps being put
        # back into, and the firmware then counts the offset twice and lands
        # two hours out. Seen on 2026-09-16, fixed, and back by 12:43 on the
        # 17th -- after which the reconciler logged `skipped_config +7200`
        # every hour for sixteen hours and corrected nothing, which is the
        # worst of both: it could see the fault and had decided not to act.
        #
        # The correction below already writes `enable=0`, so this needs no new
        # behaviour, only permission to run.
        if abs(skew) <= self._tolerance:
            return "ok", skew
        corrected = {
            key: value for key, value in fields.items() if key != "isDst"
        }
        corrected.update(
            year=now.year, mon=now.month, day=now.day,
            hour=now.hour, min=now.minute, sec=now.second, timeZone=0,
        )
        self._client.set_clock(corrected, dict(dst, enable=0))
        after = self._client.clock_state()
        after_fields = after.get("Time") if isinstance(after, dict) else None
        if isinstance(after_fields, dict):
            residual = (_displayed(after_fields) - self._utc_now().replace(tzinfo=None)).total_seconds()
        else:
            residual = skew
        return "corrected", residual


class ClockWorker:
    """Background thread that drives the reconciler on its own schedule."""

    def __init__(self, reconciler: ClockReconciler, *, poll_seconds: float = 30.0):
        self._reconciler = reconciler
        self._poll_seconds = float(poll_seconds)
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="camera-clock-reconcile", daemon=True
        )

    def start(self) -> None:
        if self._reconciler.enabled:
            self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self._poll_seconds + 1)

    def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                self._reconciler.run_due()
            except Exception:
                # Recorded on the reconciler; it must never take the service down.
                pass
            self._stopped.wait(self._poll_seconds)


def _displayed(fields: dict) -> datetime:
    try:
        return datetime(
            int(fields["year"]), int(fields["mon"]), int(fields["day"]),
            int(fields["hour"]), int(fields["min"]), int(fields["sec"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise CameraError("camera reported an unusable clock") from error


def _error_outcome(error: CameraError) -> str:
    name = type(error).__name__
    if name == "CameraBusy":
        return "camera_busy"
    if name == "CameraUnreachable":
        return "camera_unreachable"
    return "camera_error"


def _isoformat(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(
        float(epoch_seconds), tz=timezone.utc
    ).replace(microsecond=0).isoformat()
