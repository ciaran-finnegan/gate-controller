"""Nonsecret, atomic media capability publication for controller heartbeats."""

import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


_GATEWAY_API = "http://127.0.0.1:9997/v3/paths/list"
_MAX_GATEWAY_API_BYTES = 64 * 1024
_MAX_GATEWAY_PATHS = 64
_MAX_TRACKS = 16


def default_capabilities() -> dict:
    return {
        "observed_at": int(time.time()),
        "media": {
            "video": _capability(False, False, False, "not_configured"),
            "listen": _capability(False, False, False, "not_configured"),
            "talkback": _capability(False, False, False, "hardware_unverified"),
        }
    }


def capability_snapshot(environment, gateway_ready: bool) -> dict:
    """Build a conservative, nonsecret snapshot from explicit operator settings."""
    video_configured = _enabled(environment.get("GATE_MEDIA_VIDEO_CONFIGURED"))
    listen_configured = _enabled(environment.get("GATE_MEDIA_LISTEN_CONFIGURED"))
    talkback_configured = _enabled(environment.get("GATE_MEDIA_TALKBACK_CONFIGURED"))
    video_ready = video_configured and gateway_ready
    listen_ready = listen_configured and gateway_ready
    video_verified = video_ready and _enabled(environment.get("GATE_MEDIA_VIDEO_VERIFIED"))
    listen_verified = listen_ready and _enabled(environment.get("GATE_MEDIA_LISTEN_VERIFIED"))
    return {
        "observed_at": int(time.time()),
        "media": {
            "video": _capability(video_configured, video_ready, video_verified,
                                  _reason(video_configured, video_ready, video_verified)),
            "listen": _capability(listen_configured, listen_ready, listen_verified,
                                   _reason(listen_configured, listen_ready, listen_verified)),
            # Backchannel support is intentionally never claimed until separately verified.
            "talkback": _capability(talkback_configured, False, False, "hardware_unverified"),
        }
    }


def write_capabilities(path: Path, capabilities: dict) -> None:
    """Atomically replace the public, nonsecret capability file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(capabilities, sort_keys=True, separators=(",", ":")).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".capabilities.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            descriptor = None
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, target)
        _fsync_directory(target.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


class MediaHealthPublisher:
    """Publishes gateway readiness without sharing secrets or controller state."""

    def __init__(self, path: Path, environment, *, interval_seconds: float = 5.0):
        self._path = Path(path)
        self._environment = environment
        self._interval_seconds = interval_seconds
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name="media-health", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._thread.join(timeout=self._interval_seconds + 1)

    def publish_once(self) -> None:
        write_capabilities(
            self._path, capability_snapshot(self._environment, _gateway_is_ready())
        )

    def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                self.publish_once()
            except OSError:
                # Capability publication is best effort and cannot affect authorization.
                pass
            self._stopped.wait(self._interval_seconds)


def _gateway_is_ready(*, opener=urllib.request.urlopen) -> bool:
    try:
        with opener(_GATEWAY_API, timeout=1) as response:
            if response.status != 200:
                return False
            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) > _MAX_GATEWAY_API_BYTES:
                return False
            return gateway_status_ready(response.read(_MAX_GATEWAY_API_BYTES + 1))
    except (OSError, urllib.error.URLError, TypeError, ValueError):
        return False


def gateway_status_ready(body: bytes) -> bool:
    """Return true only for a bounded API response with a ready gate path and tracks."""
    if not isinstance(body, bytes) or not body or len(body) > _MAX_GATEWAY_API_BYTES:
        return False
    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return False
    items = payload["items"]
    if len(items) > _MAX_GATEWAY_PATHS:
        return False
    for item in items:
        if not isinstance(item, dict) or item.get("name") != "gate":
            continue
        if item.get("ready") is not True and item.get("available") is not True:
            return False
        tracks = item.get("tracks")
        return (
            isinstance(tracks, list)
            and 0 < len(tracks) <= _MAX_TRACKS
            and all(isinstance(track, str) and 0 < len(track) <= 64 for track in tracks)
        )
    return False


def _enabled(value) -> bool:
    return isinstance(value, str) and value.strip().lower() == "true"


def _capability(configured: bool, ready: bool, verified: bool, reason: str) -> dict:
    return {"configured": configured, "ready": ready, "verified": verified, "reason": reason}


def _reason(configured: bool, ready: bool, verified: bool) -> str:
    if not configured:
        return "not_configured"
    if not ready:
        return "gateway_unhealthy"
    return "ready" if verified else "hardware_unverified"


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_DIRECTORY)
    except (AttributeError, OSError):
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result
