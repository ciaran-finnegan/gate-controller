"""Loopback HTTP entrypoint for the isolated gate camera-control service.

The service binds 127.0.0.1 only.  Remote callers reach it through the existing
Cloudflare Tunnel with its own Access application; nothing here authenticates a
caller, exactly as the controller's own loopback command server does not.
"""

import argparse
import json
import logging
import os
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from gate_media_config import (
    MediaConfigError,
    relevant_camera_control_environment,
    validate_camera_control_environment,
)

from .ir import DIRECT_READ_MAX_AGE_SECONDS, IrController, RevertWorker
from .reolink import IR_STATES, CameraBusy, CameraError, CameraUnreachable, ReolinkClient
from .state import STATE_PATH, StatePublisher


LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8767
MAX_REQUEST_BYTES = 4096
RUNTIME_ROOT = "/run/gate-camera"
TOKEN_PATH = f"{RUNTIME_ROOT}/token.json"
LEASE_PATH = f"{RUNTIME_ROOT}/lease.json"
SNAPSHOT_MIN_INTERVAL_SECONDS = 2.0
IDEMPOTENCY_TTL_SECONDS = 300.0
MAX_IDEMPOTENCY_ENTRIES = 64
MAX_IDEMPOTENCY_KEY_LENGTH = 128
_IR_BODY_FIELDS = frozenset({"state", "lease_minutes", "ttl_seconds", "idempotency_key"})
_STATE_PATHS = frozenset({"/camera/state", "/camera/ir"})
_SNAPSHOT_PATHS = frozenset({"/camera/snap", "/camera/snapshot"})


def journal(logger, stage, **fields) -> None:
    """Emit one structured, credential-free journal line."""
    parts = [f"gate_camera_control stage={_journal_value(stage)}"]
    for key in sorted(fields):
        value = fields[key]
        if value is None:
            continue
        parts.append(f"{key}={_journal_value(value)}")
    logger.info(" ".join(parts))


def _journal_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    return "".join(character for character in text if character.isalnum()
                   or character in "-_.:+")[:64] or "unknown"


class CameraControlService:
    """Everything the HTTP layer is allowed to do, with no camera details leaked."""

    def __init__(self, controller, client, *, clock=time.time, logger=None,
                 snapshot_min_interval=SNAPSHOT_MIN_INTERVAL_SECONDS):
        self._controller = controller
        self._client = client
        self._clock = clock
        self._logger = logger or logging.getLogger("gate_camera_control")
        self._snapshot_min_interval = float(snapshot_min_interval)
        self._snapshot_lock = threading.Lock()
        self._last_snapshot_at = None
        self._idempotency = OrderedDict()
        self._idempotency_lock = threading.Lock()

    @property
    def controller(self):
        return self._controller

    def state(self) -> dict:
        snapshot = self._controller.state(refresh_max_age=DIRECT_READ_MAX_AGE_SECONDS)
        return self._envelope(snapshot)

    def set_ir(self, payload) -> dict:
        request = _parse_ir_request(payload, self._controller.max_lease_minutes)
        key = request["idempotency_key"]
        if key is not None:
            replay = self._replayed(key)
            if replay is not None:
                return replay
        snapshot = self._controller.set_state(request["state"], request["lease_minutes"])
        response = self._envelope(snapshot)
        response["status"] = "completed"
        if key is not None:
            response["idempotency_key"] = key
            self._remember(key, response)
        return response

    def snapshot(self) -> bytes:
        with self._snapshot_lock:
            now = self._clock()
            if (self._last_snapshot_at is not None
                    and now - self._last_snapshot_at < self._snapshot_min_interval):
                remaining = self._snapshot_min_interval - (now - self._last_snapshot_at)
                journal(self._logger, "snapshot_rate_limited",
                        retry_after=max(1, int(remaining + 0.999)))
                raise SnapshotRateLimited(max(1, int(remaining + 0.999)))
            self._last_snapshot_at = now
        image = self._client.snapshot()
        journal(self._logger, "snapshot", bytes=len(image), outcome="completed")
        return image

    def _envelope(self, ir_snapshot) -> dict:
        return {
            "observed_at": datetime.now(timezone.utc).replace(
                microsecond=0
            ).isoformat(),
            "ir": ir_snapshot,
        }

    def _replayed(self, key):
        with self._idempotency_lock:
            self._expire_locked()
            entry = self._idempotency.get(key)
            return None if entry is None else dict(entry[1])

    def _remember(self, key, response) -> None:
        with self._idempotency_lock:
            self._expire_locked()
            self._idempotency[key] = (self._clock(), dict(response))
            while len(self._idempotency) > MAX_IDEMPOTENCY_ENTRIES:
                self._idempotency.popitem(last=False)

    def _expire_locked(self) -> None:
        cutoff = self._clock() - IDEMPOTENCY_TTL_SECONDS
        for key in [k for k, (at, _) in self._idempotency.items() if at < cutoff]:
            self._idempotency.pop(key, None)


class SnapshotRateLimited(Exception):
    """One snapshot per interval; the caller is told exactly how long to wait."""

    def __init__(self, retry_after: int):
        super().__init__("rate_limited")
        self.retry_after = max(1, int(retry_after))


def _parse_ir_request(payload, max_lease_minutes) -> dict:
    if not isinstance(payload, dict) or not set(payload) <= _IR_BODY_FIELDS:
        raise ValueError("invalid_request")
    state = payload.get("state")
    if state not in IR_STATES:
        raise ValueError("invalid_request")
    lease_minutes = payload.get("lease_minutes")
    ttl_seconds = payload.get("ttl_seconds")
    if lease_minutes is not None and ttl_seconds is not None:
        raise ValueError("invalid_request")
    if ttl_seconds is not None:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise ValueError("invalid_request")
        if ttl_seconds % 60 or not 60 <= ttl_seconds <= max_lease_minutes * 60:
            raise ValueError("invalid_request")
        lease_minutes = ttl_seconds // 60
    if lease_minutes is not None:
        if isinstance(lease_minutes, bool) or not isinstance(lease_minutes, int):
            raise ValueError("invalid_request")
        if not 1 <= lease_minutes <= max_lease_minutes:
            raise ValueError("invalid_request")
    key = payload.get("idempotency_key")
    if key is not None and (not isinstance(key, str) or not key
                            or len(key) > MAX_IDEMPOTENCY_KEY_LENGTH):
        raise ValueError("invalid_request")
    return {"state": state, "lease_minutes": lease_minutes, "idempotency_key": key}


class CameraControlServer(ThreadingHTTPServer):
    """HTTP server that owns nothing but the service facade."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, service, *, logger=None):
        self.service = service
        self.logger = logger or logging.getLogger("gate_camera_control")
        super().__init__(address, CameraControlHandler)


class CameraControlHandler(BaseHTTPRequestHandler):
    server_version = "gate-camera-control"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        path = self._route()
        if path is None:
            return
        if path in _STATE_PATHS:
            self._guarded(lambda: self._respond_json(200, self.server.service.state()))
        elif path in _SNAPSHOT_PATHS:
            self._guarded(self._respond_snapshot)
        else:
            self._respond_json(404, {"error": "not_found"})

    def do_POST(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        path = self._route()
        if path is None:
            return
        if path in _SNAPSHOT_PATHS:
            self._guarded(self._respond_snapshot)
            return
        if path in _STATE_PATHS and path != "/camera/ir":
            self._respond_json(405, {"error": "method_not_allowed"})
            return
        if path != "/camera/ir":
            self._respond_json(404, {"error": "not_found"})
            return
        payload = self._read_json_body()
        if payload is _INVALID:
            return
        self._guarded(lambda: self._respond_json(
            200, self.server.service.set_ir(payload)
        ))

    def do_PUT(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        self._respond_json(405, {"error": "method_not_allowed"})

    def do_DELETE(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        self._respond_json(405, {"error": "method_not_allowed"})

    def log_message(self, _format, *_arguments):
        """Access logging is suppressed; the service journals its own decisions."""

    def send_error(self, *_arguments, **_keywords):
        self._respond_json(400, {"error": "invalid_request"})

    def _route(self):
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment:
            self._respond_json(404, {"error": "not_found"})
            return None
        return parsed.path

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", ""))
        except (TypeError, ValueError):
            self._respond_json(400, {"error": "invalid_request"})
            return _INVALID
        if length < 0:
            self._respond_json(400, {"error": "invalid_request"})
            return _INVALID
        if length > MAX_REQUEST_BYTES:
            self._respond_json(413, {"error": "request_too_large"})
            return _INVALID
        try:
            body = self.rfile.read(length)
        except OSError:
            self._respond_json(400, {"error": "invalid_request"})
            return _INVALID
        if len(body) != length:
            self._respond_json(400, {"error": "invalid_request"})
            return _INVALID
        try:
            return json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, ValueError):
            self._respond_json(400, {"error": "invalid_request"})
            return _INVALID

    def _respond_snapshot(self) -> None:
        image = self.server.service.snapshot()
        self._respond(200, "image/jpeg", image)

    def _guarded(self, action) -> None:
        try:
            action()
        except SnapshotRateLimited as error:
            self._respond_json(
                429, {"error": "rate_limited", "retry_after": error.retry_after},
                retry_after=error.retry_after,
            )
        except CameraBusy as error:
            journal(self.server.logger, "camera_busy", retry_after=error.retry_after)
            self._respond_json(
                503, {"error": "camera_busy", "retry_after": error.retry_after},
                retry_after=error.retry_after,
            )
        except CameraUnreachable:
            journal(self.server.logger, "camera_unreachable")
            self._respond_json(503, {"error": "camera_unreachable"})
        except CameraError:
            journal(self.server.logger, "camera_error")
            self._respond_json(502, {"error": "camera_error"})
        except ValueError:
            self._respond_json(400, {"error": "invalid_request"})
        except Exception:
            journal(self.server.logger, "internal_error")
            self._respond_json(500, {"error": "internal_error"})

    def _respond_json(self, status, payload, *, retry_after=None) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._respond(status, "application/json", body, retry_after=retry_after)

    def _respond(self, status, content_type, body, *, retry_after=None) -> None:
        try:
            self.send_response_only(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if retry_after is not None:
                self.send_header("Retry-After", str(int(retry_after)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            self.close_connection = True


class _Invalid:
    """Sentinel meaning the handler has already answered the request."""


_INVALID = _Invalid()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def validated_camera_control_environment(environment) -> dict[str, str]:
    """Return only the exact effective camera-control settings or fail closed."""
    return validate_camera_control_environment(
        relevant_camera_control_environment(environment)
    )


def build_service(settings, *, token_path=TOKEN_PATH, lease_path=LEASE_PATH,
                  logger=None, connection_factory=None) -> CameraControlService:
    """Wire the client and the IR controller from validated settings."""
    logger = logger or logging.getLogger("gate_camera_control")
    client = ReolinkClient(
        settings["GATE_CAMERA_HOST"],
        settings["GATE_CAMERA_USERNAME"],
        settings["GATE_CAMERA_PASSWORD"],
        token_path=token_path,
        connection_factory=connection_factory,
        journal=lambda stage, **fields: journal(logger, stage, **fields),
    )
    controller = IrController(
        client,
        default_state=settings["GATE_CAMERA_IR_DEFAULT"],
        lease_path=lease_path,
        default_lease_minutes=int(settings["GATE_CAMERA_IR_LEASE_DEFAULT_MINUTES"]),
        max_lease_minutes=int(settings["GATE_CAMERA_IR_LEASE_MAX_MINUTES"]),
        journal=lambda stage, **fields: journal(logger, stage, **fields),
    )
    return CameraControlService(controller, client, logger=logger)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Loopback gate camera-control service")
    parser.add_argument("--host", default=LOOPBACK_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--state-path", default=STATE_PATH)
    arguments = parser.parse_args(argv)
    if arguments.host != LOOPBACK_HOST:
        parser.error("the camera-control service must bind 127.0.0.1")
    if not 1 <= arguments.port <= 65535:
        parser.error("port must be between 1 and 65535")
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    logger = logging.getLogger("gate_camera_control")
    try:
        settings = validated_camera_control_environment(os.environ)
    except MediaConfigError as error:
        parser.error(str(error))
    os.makedirs(RUNTIME_ROOT, mode=0o755, exist_ok=True)
    service = build_service(settings, logger=logger)
    # A lease that outlived the previous process must never leave IR on.
    service.controller.restore_default_on_start()
    server = CameraControlServer((arguments.host, arguments.port), service, logger=logger)
    reverts = RevertWorker(service.controller)
    publisher = StatePublisher(arguments.state_path, service.controller.snapshot)
    journal(logger, "started", port=arguments.port,
            ir_default=service.controller.default_state,
            lease_default_minutes=service.controller.default_lease_minutes,
            lease_max_minutes=service.controller.max_lease_minutes)
    reverts.start()
    publisher.start()
    try:
        server.serve_forever()
    finally:
        publisher.stop()
        reverts.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
