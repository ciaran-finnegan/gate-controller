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
_TALK_REASONS = frozenset({
    "ready", "not_enabled", "not_probed", "ffmpeg_missing", "no_credential",
    "unsupported", "camera_auth", "camera_busy", "camera_unreachable",
    "camera_error",
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
    """Return a canonical bounded camera-control object or a conservative default."""
    try:
        if not isinstance(value, dict) or not (
            {"available", "reason", "ir"} <= set(value)
            <= {"available", "reason", "ir", "clock", "spotlight", "talkback"}
        ):
            raise ValueError("invalid camera control snapshot")
        available, reason = value["available"], value["reason"]
        if not isinstance(available, bool) or reason not in _REASONS:
            raise ValueError("invalid camera control snapshot")
        if reason in {"not_configured", "service_unhealthy"}:
            raise ValueError("the service never publishes a controller-side reason")
        infrared = _parse_ir(value["ir"])
        if available != (infrared["state"] in _IR_STATES):
            raise ValueError("incoherent camera control availability")
        if available != (reason == "ready"):
            raise ValueError("incoherent camera control reason")
        # A service from before talkback publishes no block; that is "not enabled".
        talkback = _parse_talkback(value.get("talkback", default_talkback()))
        block = {"available": available, "reason": reason, "ir": infrared, "talkback": talkback}
        if "clock" in value:
            block["clock"] = _parse_clock(value["clock"])
        if "spotlight" in value:
            # The spotlight rides alongside IR. It does not decide `available`:
            # a camera without one is still a camera whose IR works.
            spotlight = _parse_light(value["spotlight"], _SPOTLIGHT_STATES, "spotlight")
            # The service only ever leases it lit; a published lit default is a
            # document that cannot have come from it.
            if spotlight["default"] != "Off":
                raise ValueError("the spotlight default is always Off")
            block["spotlight"] = spotlight
        return block
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
        "talkback": default_talkback(),
    }


def default_talkback() -> dict:
    return {"available": False, "reason": "not_enabled", "active": False}


def _parse_talkback(value) -> dict:
    if not isinstance(value, dict) or set(value) != {"available", "reason", "active"}:
        raise ValueError("invalid talkback snapshot")
    available, reason, active = value["available"], value["reason"], value["active"]
    if (not isinstance(available, bool) or not isinstance(active, bool)
            or reason not in _TALK_REASONS):
        raise ValueError("invalid talkback snapshot")
    if available != (reason == "ready"):
        raise ValueError("incoherent talkback availability")
    return {"available": available, "reason": reason, "active": active}


def _parse_ir(value) -> dict:
    return _parse_light(value, _IR_STATES, "IR")


def _parse_light(value, states, name) -> dict:
    """One light's lease block -- IR or spotlight -- in its exact published shape."""
    if (not isinstance(value, dict)
            or set(value) != {"state", "default", "effective_until", "revert_failed"}):
        raise ValueError(f"invalid {name} snapshot")
    state, default = value["state"], value["default"]
    effective_until, revert_failed = value["effective_until"], value["revert_failed"]
    if (state not in states and state != "unknown"
            or default not in states
            or not isinstance(revert_failed, bool)):
        raise ValueError(f"invalid {name} snapshot")
    if effective_until is not None and (
        not isinstance(effective_until, str)
        or not 0 < len(effective_until) <= _MAX_TIMESTAMP_LENGTH
    ):
        raise ValueError(f"invalid {name} lease expiry")
    return {
        "state": state,
        "default": default,
        "effective_until": effective_until,
        "revert_failed": revert_failed,
    }


_CLOCK_OUTCOMES = frozenset({
    "not_checked", "ok", "corrected", "skipped_config", "camera_busy",
    "camera_unreachable", "camera_error", "disabled",
})
_MAX_CLOCK_SKEW_SECONDS = 10_000_000


def _parse_clock(value) -> dict:
    """The camera clock block: skew against the Pi and the last reconcile outcome."""
    if (not isinstance(value, dict)
            or set(value) != {"synced", "outcome", "skew_seconds", "checked_at", "corrections"}):
        raise ValueError("invalid camera clock block")
    synced, outcome = value["synced"], value["outcome"]
    skew, checked_at, corrections = value["skew_seconds"], value["checked_at"], value["corrections"]
    if not isinstance(synced, bool) or outcome not in _CLOCK_OUTCOMES:
        raise ValueError("invalid camera clock block")
    if skew is not None and (
        isinstance(skew, bool) or not isinstance(skew, int)
        or abs(skew) > _MAX_CLOCK_SKEW_SECONDS
    ):
        raise ValueError("invalid camera clock skew")
    if checked_at is not None and (
        not isinstance(checked_at, str) or not 0 < len(checked_at) <= _MAX_TIMESTAMP_LENGTH
    ):
        raise ValueError("invalid camera clock timestamp")
    if isinstance(corrections, bool) or not isinstance(corrections, int) or corrections < 0:
        raise ValueError("invalid camera clock corrections")
    if synced != (outcome in {"ok", "corrected"}):
        raise ValueError("incoherent camera clock block")
    return {
        "synced": synced, "outcome": outcome, "skew_seconds": skew,
        "checked_at": checked_at, "corrections": corrections,
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
