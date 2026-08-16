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
_VIDEO_TRACKS = frozenset({
    "AV1", "VP9", "VP8", "H265", "H264", "MPEG-4 Video", "MPEG-1/2 Video", "M-JPEG",
})
_AUDIO_TRACKS = frozenset({
    "Opus", "FLAC", "Vorbis", "MPEG-4 Audio", "MPEG-4 Audio LATM",
    "MPEG-1/2 Audio", "AC-3", "Speex", "G726", "G722", "G711", "LPCM",
})
_MEDIA_TRACKS = _VIDEO_TRACKS | _AUDIO_TRACKS


def default_capabilities() -> dict:
    return {
        "observed_at": int(time.time()),
        "media": {
            "video": _capability(False, False, False, "not_configured"),
            "listen": _capability(False, False, False, "not_configured"),
            "talkback": _capability(False, False, False, "hardware_unverified"),
        }
    }


def capability_snapshot(environment, gateway_readiness) -> dict:
    """Build a conservative, nonsecret snapshot from explicit operator settings."""
    gateway_readiness = _validated_gateway_readiness(gateway_readiness)
    video_configured = _enabled(environment.get("GATE_MEDIA_VIDEO_CONFIGURED"))
    listen_configured = _enabled(environment.get("GATE_MEDIA_LISTEN_CONFIGURED"))
    talkback_configured = _enabled(environment.get("GATE_MEDIA_TALKBACK_CONFIGURED"))
    video_ready = video_configured and gateway_readiness["video"]
    listen_ready = listen_configured and gateway_readiness["listen"]
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


def _gateway_is_ready(*, opener=urllib.request.urlopen) -> dict:
    try:
        with opener(_GATEWAY_API, timeout=1) as response:
            if response.status != 200:
                return _unready_gateway()
            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) > _MAX_GATEWAY_API_BYTES:
                return _unready_gateway()
            return gateway_status_readiness(response.read(_MAX_GATEWAY_API_BYTES + 1))
    except (AttributeError, OSError, urllib.error.URLError, TypeError, ValueError):
        return _unready_gateway()


def gateway_status_readiness(body: bytes) -> dict:
    """Return independent readiness for recognized gate-path video and audio tracks."""
    if not isinstance(body, bytes) or not body or len(body) > _MAX_GATEWAY_API_BYTES:
        return _unready_gateway()
    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return _unready_gateway()
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return _unready_gateway()
    items = payload["items"]
    if len(items) > _MAX_GATEWAY_PATHS:
        return _unready_gateway()
    gate_paths = [item for item in items if isinstance(item, dict) and item.get("name") == "gate"]
    if len(gate_paths) != 1:
        return _unready_gateway()
    gate_path = gate_paths[0]
    if gate_path.get("ready") is not True and gate_path.get("available") is not True:
        return _unready_gateway()
    tracks = gate_path.get("tracks")
    if (not isinstance(tracks, list)
            or not 0 < len(tracks) <= _MAX_TRACKS
            or not all(isinstance(track, str) and track in _MEDIA_TRACKS for track in tracks)):
        return _unready_gateway()
    return {
        "video": any(track in _VIDEO_TRACKS for track in tracks),
        "listen": any(track in _AUDIO_TRACKS for track in tracks),
    }


def _validated_gateway_readiness(value) -> dict:
    if (not isinstance(value, dict)
            or set(value) != {"video", "listen"}
            or not all(isinstance(value[feature], bool) for feature in ("video", "listen"))):
        return _unready_gateway()
    return {"video": value["video"], "listen": value["listen"]}


def _unready_gateway() -> dict:
    return {"video": False, "listen": False}


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
