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
IR_STATES = ("Auto", "Off")
REASONS = ("ready", "camera_busy", "camera_unreachable", "camera_error")


def default_state() -> dict:
    return {
        "observed_at": int(time.time()),
        "camera_control": {
            "available": False,
            "reason": "camera_unreachable",
            "ir": {
                "state": "unknown",
                "default": "Off",
                "effective_until": None,
                "revert_failed": False,
            },
        },
    }


def state_document(ir_snapshot, *, now=None) -> dict:
    """Convert an IR snapshot into the exact nonsecret heartbeat document."""
    state = ir_snapshot.get("state")
    if state not in IR_STATES:
        state = "unknown"
    default = ir_snapshot.get("default")
    if default not in IR_STATES:
        default = "Off"
    last_error = ir_snapshot.get("last_error")
    if state == "unknown":
        reason = last_error if last_error in REASONS else "camera_unreachable"
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
        },
    }


def write_state(path, document: dict) -> None:
    """Atomically replace the world-readable, nonsecret state file."""
    body = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    atomic_write(path, body, 0o644)


class StatePublisher:
    """Publishes the last known IR state without ever calling the camera itself."""

    def __init__(self, path, snapshot_provider, *,
                 interval_seconds=PUBLISH_INTERVAL_SECONDS):
        self._path = path
        self._snapshot_provider = snapshot_provider
        self._interval_seconds = float(interval_seconds)
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
        write_state(self._path, state_document(self._snapshot_provider()))

    def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                self.publish_once()
            except (OSError, TypeError, ValueError):
                # State publication is best effort and must never affect control.
                pass
            self._stopped.wait(self._interval_seconds)
