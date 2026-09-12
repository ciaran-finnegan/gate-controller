"""Nonsecret, atomic camera-control state publication for controller heartbeats.

The controller has no camera credentials and no camera host, so the only thing
it learns about the camera is what this file says.  Nothing written here names
the camera, its address, or any credential.
"""

import json
import threading
import time

from .atomic import atomic_write


STATE_PATH = "/run/gate-camera/state.json"
PUBLISH_INTERVAL_SECONDS = 5.0
# One bounded GetIrLights on the cached token, roughly twice a minute. Without
# it a healthy camera reads as `camera_unreachable` at rest, because nothing but
# an operator's own request ever observed it.
REFRESH_INTERVAL_SECONDS = 30.0
IR_STATES = ("Auto", "Off")
SPOTLIGHT_STATES = ("On", "Off")
REASONS = (
    "ready", "not_observed", "camera_busy", "camera_unreachable", "camera_error",
)


def default_state() -> dict:
    return {
        "observed_at": int(time.time()),
        "camera_control": {
            "available": False,
            "reason": "not_observed",
            "ir": _unknown_light("Off"),
            "spotlight": _unknown_light("Off"),
        },
    }


def state_document(ir_snapshot, spotlight_snapshot=None, *, now=None) -> dict:
    """Convert the light snapshots into the exact nonsecret heartbeat document.

    `available` and `reason` are the IR light's, exactly as they have always
    been. The spotlight is reported beside it and never speaks for the service:
    a spotlight the camera would not answer about leaves the block `unknown`
    without taking the whole control away from the app.

    With no spotlight snapshot the pre-spotlight document is rendered, which is
    what an older build of this service published and what the controller must
    still be able to read.
    """
    state = ir_snapshot.get("state")
    if state not in IR_STATES:
        state = "unknown"
    last_error = ir_snapshot.get("last_error")
    if state == "unknown":
        # A camera nobody has looked at yet is not a camera that failed. Saying
        # `camera_unreachable` here made a healthy camera look broken after
        # every restart, and the app hid the control until someone opened the
        # page and forced a read.
        reason = last_error if last_error in REASONS else "not_observed"
    else:
        reason = "ready"
    camera_control = {
        "available": state != "unknown",
        "reason": reason,
        "ir": _light_block(ir_snapshot, IR_STATES),
    }
    if spotlight_snapshot is not None:
        camera_control["spotlight"] = _light_block(
            spotlight_snapshot, SPOTLIGHT_STATES
        )
    return {
        "observed_at": int(time.time() if now is None else now),
        "camera_control": camera_control,
    }


def _light_block(snapshot, states) -> dict:
    state = snapshot.get("state")
    if state not in states:
        state = "unknown"
    default = snapshot.get("default")
    if default not in states:
        default = "Off"
    effective_until = snapshot.get("effective_until")
    if not isinstance(effective_until, str):
        effective_until = None
    return {
        "state": state,
        "default": default,
        "effective_until": effective_until,
        "revert_failed": bool(snapshot.get("revert_failed")),
    }


def _unknown_light(default: str) -> dict:
    return {
        "state": "unknown",
        "default": default,
        "effective_until": None,
        "revert_failed": False,
    }


def write_state(path, document: dict) -> None:
    """Atomically replace the world-readable, nonsecret state file."""
    body = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    atomic_write(path, body, 0o644)


class StatePublisher:
    """Publishes both lights' state, and keeps one slow observation behind them.

    The snapshot itself never calls the camera.  The optional `refresher` does,
    once every `refresh_interval_seconds`, on the cached login token and behind
    the client's breaker -- enough that a camera nobody has touched still reads
    as `ready` rather than sitting at `not_observed` until a request arrives.
    """

    def __init__(self, path, snapshot_provider, *, spotlight_provider=None,
                 interval_seconds=PUBLISH_INTERVAL_SECONDS,
                 refresher=None, refresh_interval_seconds=REFRESH_INTERVAL_SECONDS,
                 clock=time.time):
        self._path = path
        self._snapshot_provider = snapshot_provider
        self._spotlight_provider = spotlight_provider
        self._interval_seconds = float(interval_seconds)
        self._refresher = refresher
        self._refresh_interval_seconds = float(refresh_interval_seconds)
        self._clock = clock
        self._last_refresh_at = None
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="camera-control-state", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._thread.join(timeout=self._interval_seconds + 1)

    def publish_once(self) -> None:
        spotlight = (
            None if self._spotlight_provider is None else self._spotlight_provider()
        )
        write_state(
            self._path, state_document(self._snapshot_provider(), spotlight)
        )

    def refresh_if_due(self) -> bool:
        """Observe the camera at most once per refresh interval. True if it ran."""
        if self._refresher is None:
            return False
        now = self._clock()
        if (self._last_refresh_at is not None
                and now - self._last_refresh_at < self._refresh_interval_seconds):
            return False
        self._last_refresh_at = now
        self._refresher()
        return True

    def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                self.refresh_if_due()
            except Exception:
                # A camera that will not answer is already recorded as the
                # controller's last error; it must never stop publication.
                pass
            try:
                self.publish_once()
            except (OSError, TypeError, ValueError):
                # State publication is best effort and must never affect control.
                pass
            self._stopped.wait(self._interval_seconds)
