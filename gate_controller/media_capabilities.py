"""Best-effort reader for the media gateway's nonsecret capability snapshot."""

import json
import time
from pathlib import Path


_REASONS = frozenset({"not_configured", "gateway_unhealthy", "hardware_unverified", "ready"})
_FEATURES = ("video", "listen", "talkback")


def read_media_capabilities(path: Path, *, max_age_seconds: float = 30.0, now=None) -> dict:
    """Return conservative media state; malformed files never escape this boundary."""
    target = Path(path)
    current_time = time.time() if now is None else now
    try:
        if target.is_symlink():
            raise OSError("capability file must not be a symlink")
        if target.stat().st_mtime < current_time - max_age_seconds:
            return _unavailable("gateway_unhealthy")
        decoded = json.loads(target.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        media = decoded["media"]
        if not isinstance(media, dict) or set(media) != set(_FEATURES):
            raise ValueError("invalid media snapshot")
        return {feature: _parse_capability(media[feature]) for feature in _FEATURES}
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return _unavailable("not_configured" if not target.exists() else "gateway_unhealthy")


def _parse_capability(value) -> dict:
    if not isinstance(value, dict) or set(value) != {"configured", "ready", "verified", "reason"}:
        raise ValueError("invalid media capability")
    configured, ready, verified, reason = (
        value["configured"], value["ready"], value["verified"], value["reason"],
    )
    if (not all(isinstance(flag, bool) for flag in (configured, ready, verified))
            or reason not in _REASONS):
        raise ValueError("invalid media capability")
    return {"configured": configured, "ready": ready, "verified": verified, "reason": reason}


def _unavailable(reason: str) -> dict:
    return {
        "video": _false_capability(reason),
        "listen": _false_capability(reason),
        "talkback": _false_capability("hardware_unverified"),
    }


def _false_capability(reason: str) -> dict:
    return {"configured": False, "ready": False, "verified": False, "reason": reason}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result
