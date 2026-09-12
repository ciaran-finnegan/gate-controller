"""Best-effort reader for the camera-control service's nonsecret state snapshot.

The controller holds no camera credentials and no camera address; everything it
reports about the camera comes through this bounded, fail-closed reader.
"""

import json
import os
import stat
import time
from pathlib import Path


CAMERA_CONTROL_STATE_PATH = Path("/run/gate-camera/state.json")
_IR_STATES = frozenset({"Auto", "Off"})
_SPOTLIGHT_STATES = frozenset({"On", "Off"})
_REASONS = frozenset({
    "ready", "not_configured", "service_unhealthy", "not_observed",
    "camera_busy", "camera_unreachable", "camera_error",
})
_MAX_STATE_BYTES = 8 * 1024
_MAX_TIMESTAMP_LENGTH = 40


def read_camera_control_state(path, *, max_age_seconds: float = 30.0, now=None) -> dict:
    """Return a conservative camera-control block; bad files never escape here."""
    target = Path(path)
    current_time = time.time() if now is None else now
    flags = os.O_RDONLY | os.O_NONBLOCK
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except FileNotFoundError:
        return unavailable("not_configured")
    except (OSError, TypeError, ValueError):
        return unavailable("service_unhealthy")
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size <= 0
                or metadata.st_size > _MAX_STATE_BYTES):
            raise ValueError("invalid camera control state file")
        body = _read_exact(descriptor, metadata.st_size)
    except (OSError, TypeError, ValueError):
        return unavailable("service_unhealthy")
    finally:
        os.close(descriptor)
    try:
        decoded = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
        if (not isinstance(decoded, dict)
                or set(decoded) != {"observed_at", "camera_control"}):
            raise ValueError("invalid camera control snapshot")
        observed_at = decoded["observed_at"]
        if (not isinstance(observed_at, int) or isinstance(observed_at, bool)
                or observed_at > current_time
                or observed_at < current_time - max_age_seconds):
            raise ValueError("invalid camera control timestamp")
        return validated_camera_control(decoded["camera_control"])
    except (UnicodeDecodeError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return unavailable("service_unhealthy")


def validated_camera_control(value) -> dict:
    """Return a canonical bounded camera-control object or a conservative default.

    Two shapes are accepted, and both must keep working: the pre-spotlight
    document, which has no `spotlight` at all, and the current one, which
    carries it beside `ir`. A controller that refused the older shape would
    report a perfectly healthy older service as `service_unhealthy`; one that
    refused the newer shape would do the same to a service that had merely been
    upgraded first. The two roll out separately, so neither order may break.

    A missing `spotlight` becomes an `unknown` block with `supported: false`,
    which is how the app tells "this deployment has no spotlight control" from
    "the spotlight is there and its state is not currently known".

    `available` and `reason` stay the illuminator's. A spotlight that cannot be
    read is a spotlight the app should not offer; it is not a reason to take the
    whole camera control away.
    """
    try:
        if not isinstance(value, dict) or set(value) not in (
            {"available", "reason", "ir"},
            {"available", "reason", "ir", "spotlight"},
        ):
            raise ValueError("invalid camera control snapshot")
        available, reason = value["available"], value["reason"]
        if not isinstance(available, bool) or reason not in _REASONS:
            raise ValueError("invalid camera control snapshot")
        if reason in {"not_configured", "service_unhealthy"}:
            raise ValueError("the service never publishes a controller-side reason")
        infrared = _parse_light(value["ir"], _IR_STATES)
        if available != (infrared["state"] in _IR_STATES):
            raise ValueError("incoherent camera control availability")
        if available != (reason == "ready"):
            raise ValueError("incoherent camera control reason")
        if "spotlight" in value:
            spotlight = _parse_light(value["spotlight"], _SPOTLIGHT_STATES)
            spotlight["supported"] = True
        else:
            spotlight = _unsupported_spotlight()
        return {
            "available": available,
            "reason": reason,
            "ir": infrared,
            "spotlight": spotlight,
        }
    except (TypeError, ValueError, KeyError):
        return unavailable("service_unhealthy")


def unavailable(reason: str) -> dict:
    return {
        "available": False,
        "reason": reason,
        "ir": {
            "state": "unknown",
            "default": "Off",
            "effective_until": None,
            "revert_failed": False,
        },
        "spotlight": _unsupported_spotlight(),
    }


def _unsupported_spotlight() -> dict:
    return {
        "state": "unknown",
        "default": "Off",
        "effective_until": None,
        "revert_failed": False,
        "supported": False,
    }


def _parse_light(value, states) -> dict:
    if (not isinstance(value, dict)
            or set(value) != {"state", "default", "effective_until", "revert_failed"}):
        raise ValueError("invalid light snapshot")
    state, default = value["state"], value["default"]
    effective_until, revert_failed = value["effective_until"], value["revert_failed"]
    if (state not in states and state != "unknown"
            or default not in states
            or not isinstance(revert_failed, bool)):
        raise ValueError("invalid light snapshot")
    if effective_until is not None and (
        not isinstance(effective_until, str)
        or not 0 < len(effective_until) <= _MAX_TIMESTAMP_LENGTH
    ):
        raise ValueError("invalid light lease expiry")
    return {
        "state": state,
        "default": default,
        "effective_until": effective_until,
        "revert_failed": revert_failed,
    }


def _read_exact(descriptor: int, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 4096))
        if not chunk:
            raise ValueError("camera control state changed while reading")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise ValueError("camera control state grew while reading")
    return b"".join(chunks)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result
