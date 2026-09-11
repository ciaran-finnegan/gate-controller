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
REASONS = (
    "ready", "not_observed", "camera_busy", "camera_unreachable", "camera_error",
)
TALK_REASONS = (
    "ready", "not_enabled", "not_probed", "ffmpeg_missing", "unsupported",
    "camera_auth", "camera_busy", "camera_unreachable", "camera_error",
)


def default_state() -> dict:
    return {
        "observed_at": int(time.time()),
        "camera_control": {
            "available": False,
            "reason": "not_observed",
            "ir": {
                "state": "unknown",
                "default": "Off",
                "effective_until": None,
                "revert_failed": False,
            },
            "talkback": default_talkback(),
        },
    }


def default_talkback() -> dict:
    return {"available": False, "reason": "not_enabled", "active": False}


def talkback_document(talk_block) -> dict:
    """Bound the talk controller's block to the exact nonsecret shape published."""
    if not isinstance(talk_block, dict):
        return default_talkback()
    reason = talk_block.get("reason")
    if reason not in TALK_REASONS:
        reason = "camera_error"
    available = bool(talk_block.get("available")) and reason == "ready"
    return {
        "available": available,
        "reason": reason if available or reason != "ready" else "camera_error",
        "active": bool(talk_block.get("active")),
    }


def state_document(ir_snapshot, *, now=None, talk_block=None) -> dict:
    """Convert an IR snapshot into the exact nonsecret heartbeat document."""
    state = ir_snapshot.get("state")
    if state not in IR_STATES:
        state = "unknown"
    default = ir_snapshot.get("default")
    if default not in IR_STATES:
        default = "Off"
    last_error = ir_snapshot.get("last_error")
    if state == "unknown":
        # A camera nobody has looked at yet is not a camera that failed. Saying
        # `camera_unreachable` here made a healthy camera look broken after
        # every restart, and the app hid the control until someone opened the
        # page and forced a read.
        reason = last_error if last_error in REASONS else "not_observed"
    else:
        reason = "ready"
    effective_until = ir_snapshot.get("effective_until")
    if not isinstance(effective_until, str):
        effective_until = None
    return {
        "observed_at": int(time.time() if now is None else now),
        "camera_control": {
            "available": state != "unknown",
            "reason": reason,
            "ir": {
                "state": state,
                "default": default,
                "effective_until": effective_until,
                "revert_failed": bool(ir_snapshot.get("revert_failed")),
            },
            "talkback": talkback_document(talk_block),
        },
    }


def write_state(path, document: dict) -> None:
    """Atomically replace the world-readable, nonsecret state file."""
    body = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    atomic_write(path, body, 0o644)


class StatePublisher:
    """Publishes the IR state, and keeps one slow observation behind it.

    The snapshot itself never calls the camera.  The optional `refresher` does,
    once every `refresh_interval_seconds`, on the cached login token and behind
    the client's breaker -- enough that a camera nobody has touched still reads
    as `ready` rather than sitting at `not_observed` until a request arrives.
    """

    def __init__(self, path, snapshot_provider, *,
                 interval_seconds=PUBLISH_INTERVAL_SECONDS,
                 refresher=None, refresh_interval_seconds=REFRESH_INTERVAL_SECONDS,
                 clock=time.time, talk_provider=None, talk_refresher=None):
        self._path = path
        self._snapshot_provider = snapshot_provider
        self._talk_provider = talk_provider
        self._talk_refresher = talk_refresher
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
        talk_block = self._talk_provider() if self._talk_provider is not None else None
        write_state(self._path, state_document(self._snapshot_provider(), talk_block=talk_block))

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
            if self._talk_refresher is not None:
                try:
                    # The talk probe keeps its own slow clock; a failure is its
                    # own published reason and must never stop publication.
                    self._talk_refresher()
                except Exception:
                    pass
            try:
                self.publish_once()
            except (OSError, TypeError, ValueError):
                # State publication is best effort and must never affect control.
                pass
            self._stopped.wait(self._interval_seconds)
