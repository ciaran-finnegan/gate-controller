"""Keep the gate camera in focus: a daily zoom nudge, and one when frames go soft.

Overnight from 3 to 4 October 2026 the RLC-811A's autofocus drifted (focus
motor 80 -> 86 on zoom 2) and every daylight frame after it was soft. The
per-frame ``sharpness`` this controller already records in event telemetry
went from a daylight median of about 0.21-0.26 (frames 0.19-0.29, 15-30
September) to at most 0.158 on 4-5 October, on 1920- and 3840-wide frames
alike; plate reads collapsed and the gate opened for 1 of 34 passages.
Stepping the zoom 2 -> 3 -> 2 in daylight made the camera refocus (focus 78)
and the picture was sharp again.

This module asks the camera-control service -- the only process holding the
camera credentials -- to do that nudge (``POST /camera/refocus``), and decides
*when*:

* **daily**: once per local day inside a midday window (12:00-15:00
  Europe/Dublin by default), because a defocus found by a person days later is
  days of lost reads;
* **on detection**: when the recent daylight event frames are soft. A frame
  counts only when it is a whole frame (16:9, at least 1280 wide -- a cropped
  plate band would score differently), lit (brightness >= 0.25), and taken with
  IR off. The trigger is the **median of at least 3 such frames, from at least
  2 different events, below 0.18**: under the lowest healthy frame ever
  measured (0.19) and above the highest soft one (0.158). The median of several
  frames from more than one passage keeps a single motion-blurred car from
  looking like a lens fault.

Every attempt is guarded: daylight only (the still taken first must be lit,
and IR must be Off); never while a vehicle is being handled or within the
quiet window after one; at most one detection-triggered run per
``min_interval``; and a hard cap per rolling day. While it runs it holds the
controller's activity gate, so the early-trigger watcher and the corpus stand
down rather than reading a scene that is zooming.

It is verified by measuring a 4K still before and after with the very
function event frames are scored with (``images.measure_jpeg_quality``), and
recorded as one ``gate_camera_refocus stage=attempt`` journal line. A nudge
that does not bring the sharpness back is logged as a warning and is **not**
repeated: the next try waits for the interval and the cap like any other.

Nothing here can reach the relay. It reads telemetry the pipeline has already
finished with, and talks only to the camera-control service on loopback.
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import os
import stat
import statistics
import threading
import time
from collections import deque
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .images import measure_jpeg_quality


LOGGER = logging.getLogger(__name__)

MODES = ("off", "shadow", "on")
DEFAULT_MODE = "on"
DEFAULT_TIMEZONE = "Europe/Dublin"
DEFAULT_DAILY_WINDOW = "12:00-15:00"
#: Below every healthy daylight frame measured 15-30 September (0.19) and above
#: every soft one on 4-5 October (at most 0.158).
DEFAULT_SHARPNESS_THRESHOLD = 0.18
DEFAULT_MIN_FRAMES = 3
DEFAULT_MIN_EVENTS = 2
DEFAULT_MIN_BRIGHTNESS = 0.25
DEFAULT_MIN_INTERVAL_HOURS = 6.0
DEFAULT_MAX_PER_DAY = 3
DEFAULT_QUIET_SECONDS = 120.0
#: Two attempts never closer than this, whatever triggered them, so a daily
#: run does not follow straight on from a detection run.
MIN_GAP_SECONDS = 3600.0
#: The detection window: the most recent qualifying frames, no older than this.
WINDOW_FRAMES = 12
WINDOW_MAX_AGE_SECONDS = 36 * 3600.0
#: What counts as a whole frame rather than a crop.
MIN_FRAME_WIDTH = 1280
FULL_FRAME_ASPECT = 16 / 9
ASPECT_TOLERANCE = 0.05
POLL_SECONDS = 30.0
#: After a skip that cost a camera still -- too dark, or no still -- the next
#: look is at least this far off, so a soft window that outlives the daylight
#: costs four stills an hour overnight rather than one every poll.
DEFER_SECONDS = 900.0
#: Skips are journaled when the reason changes, or at most this often.
SKIP_JOURNAL_SECONDS = 1800.0
DAY_SECONDS = 86_400.0

CAMERA_CONTROL_HOST = "127.0.0.1"
CAMERA_CONTROL_PORT = 8767
#: The service settles 7 s each way and may retry the return for about 90 s.
REFOCUS_TIMEOUT_SECONDS = 240.0
SNAPSHOT_TIMEOUT_SECONDS = 20.0
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_JSON_BYTES = 4096
MAX_STATE_FILE_BYTES = 8192
_STATE_KEYS = frozenset({"daily_date", "attempts", "last_attempt_at", "last"})


@dataclass(frozen=True)
class RefocusConfig:
    mode: str = DEFAULT_MODE
    timezone_name: str = DEFAULT_TIMEZONE
    daily_start_minute: int = 12 * 60
    daily_end_minute: int = 15 * 60
    sharpness_threshold: float = DEFAULT_SHARPNESS_THRESHOLD
    min_frames: int = DEFAULT_MIN_FRAMES
    min_events: int = DEFAULT_MIN_EVENTS
    min_brightness: float = DEFAULT_MIN_BRIGHTNESS
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_HOURS * 3600.0
    max_per_day: int = DEFAULT_MAX_PER_DAY
    quiet_seconds: float = DEFAULT_QUIET_SECONDS

    @property
    def enabled(self) -> bool:
        return self.mode != "off"


def load_refocus_config(environment=None) -> RefocusConfig:
    """Read ``GATE_CAMERA_REFOCUS*``. A bad value is journaled and defaulted.

    Never a reason to refuse to start: this is camera upkeep, and a controller
    that is down opens for nobody. An unreadable *mode* falls back to ``off``,
    the one value that cannot move the lens on a typo.
    """
    environment = os.environ if environment is None else environment
    mode = str(environment.get("GATE_CAMERA_REFOCUS", DEFAULT_MODE) or DEFAULT_MODE).strip().lower()
    if mode not in MODES:
        _rejected("GATE_CAMERA_REFOCUS", "off")
        mode = "off"
    zone = str(environment.get("GATE_CAMERA_REFOCUS_TIMEZONE", "") or "").strip() or DEFAULT_TIMEZONE
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        _rejected("GATE_CAMERA_REFOCUS_TIMEZONE", DEFAULT_TIMEZONE)
        zone = DEFAULT_TIMEZONE
    window = str(environment.get("GATE_CAMERA_REFOCUS_DAILY_WINDOW", "") or "").strip()
    try:
        start, end = _parse_window(window or DEFAULT_DAILY_WINDOW)
    except ValueError:
        _rejected("GATE_CAMERA_REFOCUS_DAILY_WINDOW", DEFAULT_DAILY_WINDOW)
        start, end = _parse_window(DEFAULT_DAILY_WINDOW)
    return RefocusConfig(
        mode=mode,
        timezone_name=zone,
        daily_start_minute=start,
        daily_end_minute=end,
        sharpness_threshold=_number(environment, "GATE_CAMERA_REFOCUS_SHARPNESS_THRESHOLD",
                                    DEFAULT_SHARPNESS_THRESHOLD, 0.05, 0.5),
        min_frames=int(_number(environment, "GATE_CAMERA_REFOCUS_MIN_FRAMES",
                               DEFAULT_MIN_FRAMES, 3, WINDOW_FRAMES, integer=True)),
        min_events=DEFAULT_MIN_EVENTS,
        min_brightness=_number(environment, "GATE_CAMERA_REFOCUS_MIN_BRIGHTNESS",
                               DEFAULT_MIN_BRIGHTNESS, 0.1, 0.9),
        min_interval_seconds=3600.0 * _number(
            environment, "GATE_CAMERA_REFOCUS_MIN_INTERVAL_HOURS",
            DEFAULT_MIN_INTERVAL_HOURS, 1.0, 48.0),
        max_per_day=int(_number(environment, "GATE_CAMERA_REFOCUS_MAX_PER_DAY",
                                DEFAULT_MAX_PER_DAY, 1, 6, integer=True)),
        quiet_seconds=_number(environment, "GATE_CAMERA_REFOCUS_QUIET_SECONDS",
                              DEFAULT_QUIET_SECONDS, 30.0, 3600.0),
    )


def _rejected(key: str, using) -> None:
    LOGGER.error("gate_camera_refocus stage=config key=%s status=rejected using=%s", key, using)


def _number(environment, key, default, minimum, maximum, *, integer=False) -> float:
    raw = str(environment.get(key, "") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if (not math.isfinite(value) or not minimum <= value <= maximum
            or (integer and value != int(value))):
        _rejected(key, default)
        return default
    return value


def _parse_window(text: str) -> tuple[int, int]:
    """``HH:MM-HH:MM`` local, start before end, within one day."""
    try:
        start_text, end_text = text.split("-")
        start, end = _minute(start_text), _minute(end_text)
    except (ValueError, AttributeError) as error:
        raise ValueError("invalid window") from error
    if not start < end:
        raise ValueError("invalid window")
    return start, end


def _minute(text: str) -> int:
    hours, minutes = text.strip().split(":")
    if len(hours) != 2 or len(minutes) != 2:
        raise ValueError("invalid time")
    hours, minutes = int(hours), int(minutes)
    if not (0 <= hours <= 24 and 0 <= minutes < 60) or hours * 60 + minutes > 1440:
        raise ValueError("invalid time")
    return hours * 60 + minutes


# -- detection ---------------------------------------------------------------

class FocusWindow:
    """The most recent daylight whole frames, and whether they read as soft."""

    def __init__(self, config: RefocusConfig, *, clock=time.time):
        self._config = config
        self._clock = clock
        self._lock = threading.Lock()
        self._frames: deque = deque(maxlen=WINDOW_FRAMES)

    def observe(self, telemetry, *, ir_state: str) -> int:
        """Add the qualifying frames of one finished event. Returns how many."""
        if ir_state != "Off":
            return 0
        frames = getattr(telemetry, "frames", None) or ()
        trace = getattr(telemetry, "trace_id", None)
        now = self._clock()
        added = 0
        with self._lock:
            for frame in frames:
                if not self._qualifies(frame):
                    continue
                self._frames.append((now, trace, float(frame.sharpness)))
                added += 1
        return added

    def _qualifies(self, frame) -> bool:
        try:
            if getattr(frame, "status", "ok") != "ok":
                return False
            width, height = int(frame.width), int(frame.height)
            sharpness, brightness = float(frame.sharpness), float(frame.brightness)
        except (AttributeError, TypeError, ValueError):
            return False
        if width < MIN_FRAME_WIDTH or height <= 0:
            return False
        if abs(width / height - FULL_FRAME_ASPECT) > ASPECT_TOLERANCE * FULL_FRAME_ASPECT:
            return False
        if not (math.isfinite(sharpness) and math.isfinite(brightness)):
            return False
        return brightness >= self._config.min_brightness

    def verdict(self) -> dict:
        """``{"soft", "frames", "events", "median"}`` over the live window."""
        cutoff = self._clock() - WINDOW_MAX_AGE_SECONDS
        with self._lock:
            while self._frames and self._frames[0][0] < cutoff:
                self._frames.popleft()
            values = [sharpness for _at, _trace, sharpness in self._frames]
            events = len({trace for _at, trace, _sharpness in self._frames})
        median = statistics.median(values) if values else None
        soft = (
            median is not None
            and len(values) >= self._config.min_frames
            and events >= self._config.min_events
            and median < self._config.sharpness_threshold
        )
        return {"soft": soft, "frames": len(values), "events": events, "median": median}

    def clear(self) -> None:
        """Forget frames taken before a nudge: they describe the old focus."""
        with self._lock:
            self._frames.clear()


# -- the camera-control service ----------------------------------------------

class CameraControlError(Exception):
    """The service did not do what was asked; ``code`` says why, boundedly."""

    def __init__(self, code: str, *, status=None, body=None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.body = body or {}


class CameraControlClient:
    """Loopback calls to gate-camera-control. No credential, no camera address."""

    def __init__(self, host: str = CAMERA_CONTROL_HOST, port: int = CAMERA_CONTROL_PORT, *,
                 connection_factory=None):
        self._host = host
        self._port = int(port)
        self._connection_factory = connection_factory or (
            lambda timeout: http.client.HTTPConnection(self._host, self._port, timeout=timeout)
        )

    def snapshot(self) -> bytes:
        status, content_type, body = self._request(
            "GET", "/camera/snap", None, SNAPSHOT_TIMEOUT_SECONDS, MAX_SNAPSHOT_BYTES,
        )
        if status != 200 or not content_type.startswith("image/jpeg"):
            raise CameraControlError(_error_code(status, body), status=status)
        return body

    def refocus(self, reason: str) -> dict:
        payload = json.dumps({"reason": reason}).encode("utf-8")
        status, _content_type, body = self._request(
            "POST", "/camera/refocus", payload, REFOCUS_TIMEOUT_SECONDS, MAX_JSON_BYTES,
        )
        decoded = _json_object(body)
        if status != 200:
            raise CameraControlError(_error_code(status, body), status=status, body=decoded)
        return decoded

    def _request(self, method, path, body, timeout, maximum):
        connection = self._connection_factory(timeout)
        headers = {"Accept": "*/*", "Connection": "close"}
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read(maximum + 1)
            status = response.status
            content_type = response.getheader("Content-Type") or ""
        except (OSError, http.client.HTTPException) as error:
            raise CameraControlError("service_unreachable") from error
        finally:
            try:
                connection.close()
            except (OSError, http.client.HTTPException):
                pass
        if len(payload) > maximum:
            raise CameraControlError("response_too_large", status=status)
        return status, content_type, payload


def _json_object(body: bytes) -> dict:
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


_ERROR_CODES = frozenset({
    "rate_limited", "refocus_busy", "camera_busy", "camera_unreachable", "camera_error",
    "zoom_return_failed", "not_found", "invalid_request", "internal_error",
})


def _error_code(status, body: bytes) -> str:
    error = _json_object(body).get("error") if body else None
    if error in _ERROR_CODES:
        return error
    return f"http_{int(status)}" if isinstance(status, int) else "camera_error"


# -- the worker --------------------------------------------------------------

class CameraRefocusWorker:
    """Decides when to nudge, guards it, verifies it and records it."""

    def __init__(self, config: RefocusConfig, client, *, state_path=None, activity=None,
                 session_active=None, ir_state=None, clock=time.time, sleep=time.sleep,
                 measure=measure_jpeg_quality):
        self.config = config
        self._client = client
        self._state_path = None if state_path is None else Path(state_path)
        self._activity = activity
        self._session_active = session_active or (lambda: False)
        self._ir_state = ir_state or (lambda: "unknown")
        self._clock = clock
        self._sleep = sleep
        self._measure = measure
        self._zone = ZoneInfo(config.timezone_name)
        self.window = FocusWindow(config, clock=clock)
        self._lock = threading.Lock()
        # Only one attempt at a time, whoever asks.
        self._run_lock = threading.Lock()
        self._daily_date: str | None = None
        self._attempts: list[float] = []
        self._last_attempt_at: float | None = None
        self._last: dict | None = None
        self._last_skip: tuple | None = None
        self._last_skip_at: float | None = None
        # Set after a skip that cost a camera still (too dark, no still).
        self._not_before: float | None = None
        self._load_state()

    # -- inputs ----------------------------------------------------------
    def observe_result(self, result) -> None:
        """Feed one finished burst's frames. Never raises into the pipeline."""
        if not self.config.enabled:
            return
        try:
            telemetry = getattr(result, "telemetry", None)
            if telemetry is None:
                return
            self.window.observe(telemetry, ir_state=self._safe_ir_state())
        except Exception:
            LOGGER.warning("gate_camera_refocus stage=observe_failed")

    # -- the loop ----------------------------------------------------------
    def run_forever(self, stop_event) -> None:
        if not self.config.enabled:
            stop_event.wait()
            return
        while not stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                LOGGER.exception("gate_camera_refocus stage=failed")
            stop_event.wait(POLL_SECONDS)

    def run_once(self) -> str | None:
        """Attempt a refocus if one is due and every guard allows it.

        Returns the attempt's outcome, a ``skipped_<reason>`` token, or
        ``None`` when nothing was due.
        """
        if not self.config.enabled:
            return None
        if not self._run_lock.acquire(blocking=False):
            return None
        try:
            now = self._clock()
            trigger, verdict = self._due(now)
            if trigger is None:
                return None
            reason = self._blocked(now)
            if reason is not None:
                return self._skip(trigger, reason, now)
            return self._attempt(trigger, verdict, now)
        finally:
            self._run_lock.release()

    def _due(self, now: float):
        verdict = self.window.verdict()
        with self._lock:
            last = self._last_attempt_at
            daily_done = self._daily_date == self._local_date(now)
            not_before = self._not_before
        if not_before is not None and now < not_before:
            return None, verdict
        if last is not None and now - last < MIN_GAP_SECONDS:
            return None, verdict
        if verdict["soft"] and (last is None or now - last >= self.config.min_interval_seconds):
            return "detection", verdict
        if not daily_done and self._in_daily_window(now):
            return "daily", verdict
        return None, verdict

    def _blocked(self, now: float) -> str | None:
        with self._lock:
            self._attempts = [at for at in self._attempts if now - at < DAY_SECONDS]
            if len(self._attempts) >= self.config.max_per_day:
                return "daily_cap"
        activity = self._activity
        try:
            if activity is not None:
                if activity.busy_reason() is not None:
                    return "vehicle_activity"
                if activity.quiet_seconds() < self.config.quiet_seconds:
                    return "vehicle_activity"
            if self._session_active():
                return "vehicle_activity"
        except Exception:
            # Not being able to tell is not permission to move the lens.
            return "vehicle_activity"
        if self._safe_ir_state() != "Off":
            return "ir_not_off"
        return None

    def _attempt(self, trigger: str, verdict: dict, now: float) -> str:
        record = {
            "trigger": trigger,
            "window_frames": verdict["frames"],
            "window_median": verdict["median"],
        }
        # The still comes first and outside the activity hold: it moves
        # nothing, and a hold that ended on a "too dark" answer would reset
        # the quiet window the corpus and this guard both wait on.
        try:
            before = self._measure(self._client.snapshot())
        except CameraControlError as error:
            return self._defer(trigger, f"snapshot_{error.code}", now)
        if before.status != "ok":
            return self._defer(trigger, "snapshot_unreadable", now)
        record["sharpness_before"] = before.sharpness
        record["brightness_before"] = before.brightness
        if before.brightness < self.config.min_brightness:
            # The camera's autofocus hunts in the dark; that is how the lens
            # got lost overnight. Daylight only.
            return self._defer(trigger, "dark", now)
        # Asked again: the still took time, and a car may have arrived in it.
        reason = self._blocked(now)
        if reason is not None:
            return self._skip(trigger, reason, now)
        # Counted before the service is asked, so a call that hangs or dies
        # still spends the budget.
        self._count_attempt(now)
        if self.config.mode == "shadow":
            record["outcome"] = "shadow"
            return self._finish(record, now, None)
        hold = (self._activity.activity("camera_refocus")
                if self._activity is not None else nullcontext())
        with hold:
            # Bumped by every gate decision that begins from here on, so a
            # vehicle arriving mid-nudge is recorded rather than missed.
            epoch = self._activity.epoch() if self._activity is not None else None
            try:
                answer = self._client.refocus(trigger)
            except CameraControlError as error:
                record["outcome"] = error.code
                zoom = error.body.get("zoom") if isinstance(error.body, dict) else None
                if isinstance(zoom, dict):
                    record["zoom_before"] = zoom.get("expected")
                    record["zoom_after"] = zoom.get("observed")
                # The lens may have moved; the old frames no longer describe it.
                self.window.clear()
                return self._finish(record, now, epoch)
            zoom = answer.get("zoom") if isinstance(answer.get("zoom"), dict) else {}
            focus = answer.get("focus") if isinstance(answer.get("focus"), dict) else {}
            record.update(
                zoom_before=zoom.get("before"), zoom_step=zoom.get("stepped_to"),
                zoom_after=zoom.get("after"),
                focus_before=focus.get("before"), focus_after=focus.get("after"),
            )
            self.window.clear()
            after = self._after_snapshot()
            if after is None:
                record["outcome"] = "unverified"
            else:
                record["sharpness_after"] = after.sharpness
                record["brightness_after"] = after.brightness
                if answer.get("status") == "step_failed":
                    record["outcome"] = "step_failed"
                elif after.sharpness >= self.config.sharpness_threshold:
                    record["outcome"] = "completed"
                else:
                    record["outcome"] = "not_recovered"
            return self._finish(record, now, epoch)

    def _after_snapshot(self):
        """One still after the service has settled; one retry past its 2 s budget."""
        for attempt in range(2):
            if attempt:
                self._sleep(2.5)
            try:
                measured = self._measure(self._client.snapshot())
            except CameraControlError:
                continue
            if measured.status == "ok":
                return measured
        return None

    def _disturbed(self, epoch) -> bool:
        """Did a gate decision begin while the lens was being moved?"""
        try:
            if epoch is not None and self._activity is not None:
                if self._activity.epoch() != epoch:
                    return True
            return bool(self._session_active())
        except Exception:
            return True

    def _count_attempt(self, now: float) -> None:
        with self._lock:
            self._attempts.append(now)
            self._last_attempt_at = now
            # Any attempt is that day's nudge: a detection run in the morning
            # is not followed by a second, routine one at midday.
            self._daily_date = self._local_date(now)
        self._save_state()

    def _finish(self, record: dict, now: float, epoch) -> str:
        record["disturbed"] = self._disturbed(epoch)
        record["at"] = _isoformat(now)
        outcome = record["outcome"]
        with self._lock:
            self._last = _bounded_record(record)
        self._save_state()
        fields = " ".join(
            f"{key}={_journal_value(record.get(key))}" for key in (
                "trigger", "outcome", "zoom_before", "zoom_step", "zoom_after",
                "focus_before", "focus_after", "sharpness_before", "sharpness_after",
                "brightness_before", "window_median", "window_frames", "disturbed",
            )
        )
        line = f"gate_camera_refocus stage=attempt {fields}"
        if outcome in ("completed", "shadow"):
            LOGGER.info(line)
        elif outcome == "zoom_return_failed":
            LOGGER.error(line)
        else:
            # Includes `not_recovered`: the nudge ran and the picture is still
            # soft. Said once, and not retried until the interval allows.
            LOGGER.warning(line)
        return outcome

    def _defer(self, trigger: str, reason: str, now: float) -> str:
        """A skip that cost a camera still: do not ask again for a while."""
        with self._lock:
            self._not_before = now + DEFER_SECONDS
        return self._skip(trigger, reason, now)

    def _skip(self, trigger: str, reason: str, now: float) -> str:
        key = (trigger, reason)
        if (self._last_skip != key or self._last_skip_at is None
                or now - self._last_skip_at >= SKIP_JOURNAL_SECONDS):
            self._last_skip, self._last_skip_at = key, now
            LOGGER.info("gate_camera_refocus stage=skipped trigger=%s reason=%s",
                        trigger, _journal_value(reason))
        return f"skipped_{reason}"

    # -- time --------------------------------------------------------------
    def _local(self, now: float) -> datetime:
        return datetime.fromtimestamp(now, tz=timezone.utc).astimezone(self._zone)

    def _local_date(self, now: float) -> str:
        return self._local(now).date().isoformat()

    def _in_daily_window(self, now: float) -> bool:
        local = self._local(now)
        minute = local.hour * 60 + local.minute
        return self.config.daily_start_minute <= minute < self.config.daily_end_minute

    def _safe_ir_state(self) -> str:
        try:
            state = self._ir_state()
        except Exception:
            return "unknown"
        return state if isinstance(state, str) else "unknown"

    # -- heartbeat ---------------------------------------------------------
    def status(self) -> dict:
        """Additive heartbeat block. The app drops keys it does not know yet."""
        now = self._clock()
        verdict = self.window.verdict()
        with self._lock:
            attempts = sum(1 for at in self._attempts if now - at < DAY_SECONDS)
            last = None if self._last is None else dict(self._last)
            daily_date = self._daily_date
        return {
            "mode": self.config.mode,
            "window": {
                "frames": verdict["frames"],
                "events": verdict["events"],
                "median_sharpness": _rounded(verdict["median"]),
                "threshold": self.config.sharpness_threshold,
                "soft": verdict["soft"],
            },
            "attempts_24h": attempts,
            "max_per_day": self.config.max_per_day,
            "daily_done_for": daily_date,
            "last_attempt": last,
        }

    # -- persistence -------------------------------------------------------
    def _save_state(self) -> None:
        if self._state_path is None:
            return
        with self._lock:
            document = {
                "daily_date": self._daily_date,
                "attempts": [round(at, 3) for at in self._attempts][-16:],
                "last_attempt_at": self._last_attempt_at,
                "last": self._last,
            }
        body = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        temporary = self._state_path.with_name(f".{self._state_path.name}.new")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._state_path)
        except OSError:
            LOGGER.warning("gate_camera_refocus stage=state_unwritable")

    def _load_state(self) -> None:
        """Restore the schedule across restarts, which a release causes several times a day.

        A record that exists but cannot be read is treated as an attempt made
        just now: losing it must hold the lens still, not free it to move.
        """
        if self._state_path is None:
            return
        try:
            descriptor = os.open(self._state_path,
                                 os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return
        except OSError:
            self._conservative_restart()
            return
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_STATE_FILE_BYTES:
                raise ValueError("invalid refocus state")
            body = os.read(descriptor, metadata.st_size)
            decoded = json.loads(body.decode("utf-8"))
            if not isinstance(decoded, dict) or set(decoded) != _STATE_KEYS:
                raise ValueError("invalid refocus state")
            daily = decoded["daily_date"]
            if daily is not None:
                date.fromisoformat(daily)
            attempts = decoded["attempts"]
            if not isinstance(attempts, list) or not all(
                _finite_number(at) for at in attempts
            ):
                raise ValueError("invalid refocus state")
            last_at = decoded["last_attempt_at"]
            if last_at is not None and not _finite_number(last_at):
                raise ValueError("invalid refocus state")
            last = decoded["last"]
            if last is not None and not isinstance(last, dict):
                raise ValueError("invalid refocus state")
        except (OSError, ValueError, TypeError, UnicodeDecodeError):
            self._conservative_restart()
            return
        finally:
            os.close(descriptor)
        now = self._clock()
        self._daily_date = daily
        # A time in the future is not a time this process wrote; clamp it.
        self._attempts = [min(float(at), now) for at in attempts]
        self._last_attempt_at = None if last_at is None else min(float(last_at), now)
        self._last = _bounded_record(last) if last is not None else None

    def _conservative_restart(self) -> None:
        LOGGER.warning("gate_camera_refocus stage=state_corrupt treated_as=attempt_now")
        now = self._clock()
        self._attempts = [now]
        self._last_attempt_at = now
        self._daily_date = self._local_date(now)


def _finite_number(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


_RECORD_FIELDS = (
    "at", "trigger", "outcome", "zoom_before", "zoom_step", "zoom_after",
    "focus_before", "focus_after", "sharpness_before", "sharpness_after",
    "brightness_before", "brightness_after", "window_median", "window_frames", "disturbed",
)


def _bounded_record(record: dict) -> dict:
    """The last attempt, reduced to bounded scalars for the file and the heartbeat."""
    bounded = {}
    for key in _RECORD_FIELDS:
        value = record.get(key)
        if isinstance(value, bool) or value is None:
            bounded[key] = value
        elif isinstance(value, int):
            bounded[key] = value if -10_000 <= value <= 10_000 else None
        elif isinstance(value, float):
            bounded[key] = _rounded(value)
        elif isinstance(value, str):
            bounded[key] = _journal_value(value)[:40]
        else:
            bounded[key] = None
    return bounded


def _rounded(value):
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return round(float(value), 4)


def _journal_value(value) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.3f}"
    text = str(value)
    return "".join(c for c in text if c.isalnum() or c in "-_.:+")[:64] or "unknown"


def _isoformat(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(
        float(epoch_seconds), tz=timezone.utc
    ).replace(microsecond=0).isoformat()


def build_refocus_worker(environment, *, state_directory, activity=None, trigger_capture=None,
                         ir_state=None, client=None, clock=time.time, sleep=time.sleep):
    """The refocus worker the environment configures, or None when it is off."""
    config = load_refocus_config(environment)
    if not config.enabled:
        LOGGER.info("gate_camera_refocus stage=config mode=off")
        return None
    session_active = getattr(trigger_capture, "session_active", None)
    worker = CameraRefocusWorker(
        config, client or CameraControlClient(),
        state_path=Path(state_directory) / "camera-refocus.json",
        activity=activity,
        session_active=session_active if callable(session_active) else None,
        ir_state=ir_state, clock=clock, sleep=sleep,
    )
    LOGGER.info(
        "gate_camera_refocus stage=config mode=%s window=%02d:%02d-%02d:%02d timezone=%s "
        "threshold=%.3f min_frames=%d min_brightness=%.2f min_interval_hours=%g max_per_day=%d",
        config.mode, config.daily_start_minute // 60, config.daily_start_minute % 60,
        config.daily_end_minute // 60, config.daily_end_minute % 60, config.timezone_name,
        config.sharpness_threshold, config.min_frames, config.min_brightness,
        config.min_interval_seconds / 3600.0, config.max_per_day,
    )
    return worker
