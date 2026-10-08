"""Keep the camera's clock on the Pi's, so its alarm timestamps are trusted.

The controller refuses a webhook whose alarm time sits too far from its own
clock. On 2026-09-16 every webhook was refused for four days because the
NVR that records the camera kept pushing it a clock two hours out, and the
camera's own NTP was overwritten with it. The Pi is NTP-synced and already
talks to the camera, so it is the natural owner of the camera's time: once
an hour it reads ``GetTime`` and, when the display is not UTC to within a
few seconds, writes ``SetTime`` with UTC.

The camera's alarm timestamps carry a fixed ``+0000`` suffix whatever the
camera displays, so the only configuration the controller can trust is a
camera that *displays* UTC: ``timeZone 0`` with DST disabled. Anything else
is not a choice this system can honour, whoever made it (the NVR's channel
time push, a phone app's "sync with phone time", a save of the camera's Date
and Time page), and it is corrected the same way.

Three things made the fault keep coming back after the first reconciler
shipped, and each has its own guard here:

* A zone other than UTC was reported as ``skipped_config`` and never touched,
  so one sync from a phone in another time zone silenced recognition for good.
  Every non-UTC configuration is now corrected.
* ``SetTime`` takes the *displayed* time and the firmware shifts it by the DST
  hour when that flag changes in the same write, so a correction that also
  changed the configuration could land an hour out. The correction is read
  back and, when it did not land, written a second time with the configuration
  now unchanged, so the firmware displays exactly what it was sent.
* The Pi has no RTC and the service starts before ``systemd-timesyncd`` has
  stepped the clock after a power cut. The first pass used to run at once and
  could write the Pi's stale boot time into a camera that was right. Nothing is
  written until the Pi's own clock is known to be synchronised.

The same writer that moves the clock turns the camera's NTP off, so each pass
also reads ``GetNtp`` and turns it back on when it finds it disabled.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import threading
import time
from datetime import datetime, timezone

from .reolink import CameraError


DEFAULT_TOLERANCE_SECONDS = 5.0
DEFAULT_INTERVAL_SECONDS = 3600.0
DEFAULT_RETRY_SECONDS = 300.0
OUTCOMES = (
    "not_checked", "ok", "corrected", "correction_failed", "skipped_config",
    "pi_unsynced", "camera_busy", "camera_unreachable", "camera_error", "disabled",
)
MAX_REPORTED_SKEW_SECONDS = 10_000_000
# What the camera's NTP block is put back to when a writer has switched it
# off and left no server: the values the commissioning script writes.
NTP_DEFAULTS = {"server": "pool.ntp.org", "port": 123, "interval": 60}
# ``systemd-timesyncd`` touches this file on the first successful sync after
# boot; it lives on tmpfs and so is absent until then.
TIMESYNC_STAMP = "/run/systemd/timesync/synchronized"
# ``adjtimex(2)`` returns this clock state while the kernel clock is undisciplined.
_TIME_ERROR = 5


class ClockReconciler:
    """Hourly read-back-and-correct of the camera clock against the Pi's."""

    def __init__(self, client, *, enabled: bool = True,
                 tolerance_seconds: float = DEFAULT_TOLERANCE_SECONDS,
                 interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
                 retry_seconds: float = DEFAULT_RETRY_SECONDS,
                 clock=time.time, utc_now=None, pi_clock_synced=None, journal=None):
        self._client = client
        self._enabled = bool(enabled)
        self._tolerance = float(tolerance_seconds)
        self._interval = float(interval_seconds)
        self._retry = float(retry_seconds)
        self._clock = clock
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self._pi_clock_synced = pi_clock_synced or pi_clock_is_synced
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
        outcome, skew, detail = "camera_error", None, {}
        try:
            outcome, skew, detail = self._reconcile()
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
            # Only a clock found right waits the full hour. A correction is
            # looked at again soon, so a writer that keeps moving the clock is
            # caught within minutes rather than leaving the camera wrong for
            # most of an hour; a Pi not yet synced, or a camera that would not
            # answer, is asked again sooner too -- but never in a tight loop:
            # the client's own login throttle and breaker sit underneath this.
            self._next_at = now + (
                self._interval if outcome in ("ok", "skipped_config") else self._retry
            )
        self._journal(
            "clock_reconcile", outcome=outcome,
            skew_seconds="unknown" if skew is None else f"{skew:+.0f}",
            **detail,
        )
        return self.snapshot()

    def _reconcile(self) -> tuple[str, float | None, dict]:
        state = self._client.clock_state()
        fields, dst = state.get("Time"), state.get("Dst")
        if not isinstance(fields, dict) or not isinstance(dst, dict):
            raise CameraError("camera reported an unusable clock")
        displayed = _displayed(fields)
        now = self._utc_now()
        skew = (displayed - now.replace(tzinfo=None)).total_seconds()
        zone, dst_enabled = _config(fields, dst)
        # What the camera was found displaying goes on every line, so the
        # journal shows the signature of whatever keeps writing it: the NVR
        # pushes UTC with DST on, a phone app pushes its own zone.
        detail = {"time_zone": zone, "dst": dst_enabled}
        displays_utc = zone == 0 and dst_enabled == 0
        if displays_utc and abs(skew) <= self._tolerance:
            detail["ntp"] = self._restore_ntp()
            return "ok", skew, detail
        if not self._pi_clock_synced():
            # The Pi's own time is not yet trusted. Writing it into the camera
            # would move a right clock to a wrong one; wait and look again.
            return "pi_unsynced", skew, detail
        residual = self._correct(fields, dst)
        detail["ntp"] = self._restore_ntp()
        if abs(residual) <= self._tolerance:
            return "corrected", residual, detail
        return "correction_failed", residual, detail

    def _correct(self, fields: dict, dst: dict) -> float:
        """Write UTC, displayed as UTC, and return the skew that remains."""
        residual = self._write_utc(fields, dst)
        if abs(residual) <= self._tolerance:
            return residual
        # The first write changed the configuration as well as the time, and
        # the firmware shifts the display by the DST hour when the flag moves
        # in the same call. The configuration is now UTC with DST off, so a
        # second write of the time alone displays exactly what it is sent.
        state = self._client.clock_state()
        after_fields = state.get("Time") if isinstance(state, dict) else None
        after_dst = state.get("Dst") if isinstance(state, dict) else None
        if not isinstance(after_fields, dict) or not isinstance(after_dst, dict):
            raise CameraError("camera reported an unusable clock")
        return self._write_utc(after_fields, after_dst)

    def _write_utc(self, fields: dict, dst: dict) -> float:
        now = self._utc_now()
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
        if not isinstance(after_fields, dict):
            raise CameraError("camera reported an unusable clock")
        return (_displayed(after_fields) - self._utc_now().replace(tzinfo=None)).total_seconds()

    def _restore_ntp(self) -> str:
        """Turn the camera's own NTP back on when a writer has switched it off.

        Never raises: NTP is the camera's own drift control between passes,
        and a camera that will not answer ``GetNtp`` still had its clock set.
        """
        ntp_state = getattr(self._client, "ntp_state", None)
        if ntp_state is None:
            return "unsupported"
        try:
            ntp = ntp_state()
            if not isinstance(ntp, dict):
                return "unknown"
            if _int(ntp.get("enable")) == 1:
                return "on"
            wanted = dict(ntp)
            server = wanted.get("server")
            if not isinstance(server, str) or not server.strip():
                wanted.update(NTP_DEFAULTS)
            wanted["enable"] = 1
            self._client.set_ntp(wanted)
            return "restored"
        except Exception:
            return "unknown"


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


def pi_clock_is_synced(*, stamp_path: str = TIMESYNC_STAMP, adjtimex=None) -> bool:
    """Whether the Pi's own clock has been set by NTP since boot.

    ``systemd-timesyncd`` leaves a stamp on tmpfs after its first successful
    sync, which is the definite answer on the Pi. Failing that, ``adjtimex``
    is asked for the kernel clock state, which is what ``timedatectl`` reports
    as "System clock synchronized". When neither can answer (the service's
    sandbox refuses the syscall and no stamp exists) the clock is treated as
    not synced: a camera left alone is recoverable on the next pass, a camera
    written with a boot-time clock is wrong until then.
    """
    try:
        if os.path.exists(stamp_path):
            return True
    except OSError:
        pass
    state = _adjtimex_state() if adjtimex is None else adjtimex()
    return state is not None and 0 <= state < _TIME_ERROR


def _adjtimex_state() -> int | None:
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        call = libc.adjtimex
    except (OSError, AttributeError):
        return None
    call.argtypes = [ctypes.c_void_p]
    call.restype = ctypes.c_int
    # A zeroed struct has modes == 0, which only reads the clock state.
    buffer = ctypes.create_string_buffer(1024)
    try:
        state = call(buffer)
    except Exception:
        return None
    return None if state < 0 else state


def _config(fields: dict, dst: dict) -> tuple[int, int]:
    return _int(fields.get("timeZone"), -1), _int(dst.get("enable"), 0)


def _int(value, default: int = -1) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
