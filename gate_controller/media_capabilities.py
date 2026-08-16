"""Best-effort reader for the media gateway's nonsecret capability snapshot."""

import json
import os
import stat
import time
from pathlib import Path


_REASONS = frozenset({"not_configured", "gateway_unhealthy", "hardware_unverified", "ready"})
_FEATURES = ("video", "listen", "talkback")
_MAX_CAPABILITIES_BYTES = 8 * 1024


def read_media_capabilities(path: Path, *, max_age_seconds: float = 30.0, now=None) -> dict:
    """Return conservative media state; malformed files never escape this boundary."""
    target = Path(path)
    current_time = time.time() if now is None else now
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except FileNotFoundError:
        return _unavailable("not_configured")
    except (OSError, TypeError, ValueError):
        return _unavailable("gateway_unhealthy")
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size <= 0
                or metadata.st_size > _MAX_CAPABILITIES_BYTES):
            raise ValueError("invalid capability file")
        body = _read_exact(descriptor, metadata.st_size)
    except (OSError, TypeError, ValueError):
        return _unavailable("gateway_unhealthy")
    finally:
        os.close(descriptor)
    try:
        decoded = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(decoded, dict) or set(decoded) != {"observed_at", "media"}:
            raise ValueError("invalid media snapshot")
        observed_at = decoded["observed_at"]
        if (not isinstance(observed_at, int) or isinstance(observed_at, bool)
                or observed_at > current_time
                or observed_at < current_time - max_age_seconds):
            raise ValueError("invalid media timestamp")
        media = decoded["media"]
        if not isinstance(media, dict) or set(media) != set(_FEATURES):
            raise ValueError("invalid media snapshot")
        parsed = {feature: _parse_capability(media[feature]) for feature in _FEATURES}
        parsed["talkback"] = {
            "configured": parsed["talkback"]["configured"],
            "ready": False,
            "verified": False,
            "reason": "hardware_unverified",
        }
        return parsed
    except (UnicodeDecodeError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return _unavailable("gateway_unhealthy")


def _parse_capability(value) -> dict:
    if not isinstance(value, dict) or set(value) != {"configured", "ready", "verified", "reason"}:
        raise ValueError("invalid media capability")
    configured, ready, verified, reason = (
        value["configured"], value["ready"], value["verified"], value["reason"],
    )
    if (not all(isinstance(flag, bool) for flag in (configured, ready, verified))
            or reason not in _REASONS):
        raise ValueError("invalid media capability")
    if ready and not configured or verified and not ready:
        raise ValueError("incoherent media capability")
    if reason == "not_configured" and (configured or ready or verified):
        raise ValueError("incoherent media capability")
    if reason == "gateway_unhealthy" and (ready or verified):
        raise ValueError("incoherent media capability")
    if reason == "hardware_unverified" and verified:
        raise ValueError("incoherent media capability")
    if reason == "ready" and not (configured and ready and verified):
        raise ValueError("incoherent media capability")
    return {"configured": configured, "ready": ready, "verified": verified, "reason": reason}


def _unavailable(reason: str) -> dict:
    return {
        "video": _false_capability(reason),
        "listen": _false_capability(reason),
        "talkback": _false_capability("hardware_unverified"),
    }


def _false_capability(reason: str) -> dict:
    return {"configured": False, "ready": False, "verified": False, "reason": reason}


def _read_exact(descriptor: int, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 4096))
        if not chunk:
            raise ValueError("capability file changed while reading")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise ValueError("capability file grew while reading")
    return b"".join(chunks)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result
