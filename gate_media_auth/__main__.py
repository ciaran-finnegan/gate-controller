"""Entrypoint for the isolated MediaMTX HTTP authorization sidecar."""

import argparse
import json
import logging
import os
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gate_media_config import (
    MediaConfigError,
    relevant_auth_environment,
    validate_auth_environment,
)

from .capabilities import MediaHealthPublisher
from .token import TokenValidationError, _unique_object, validate_media_token


LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 9189
MAX_REQUEST_BYTES = 8 * 1024
_ALLOWED_FIELDS = frozenset({
    "user", "password", "token", "ip", "action", "path", "protocol", "id", "query", "userAgent",
})


class MediaAuthServer(ThreadingHTTPServer):
    """HTTP server that retains only its signing secret and an opaque logger."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, secret: str, *, logger=None):
        self.secret = secret
        self.logger = logger or logging.getLogger("gate_media_auth")
        super().__init__(address, _MediaAuthHandler)


class _MediaAuthHandler(BaseHTTPRequestHandler):
    server_version = "gate-media-auth"
    sys_version = ""

    def do_POST(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path != "/auth":
            self._respond(401)
            return
        body = self._read_body()
        status = 401 if body is None else authorize_body(
            body, self.server.secret, now=int(time.time())
        )
        self._respond(status)

    def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        self._respond(401)

    def do_PUT(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        self._respond(401)

    def do_DELETE(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        self._respond(401)

    def log_message(self, _format, *_arguments):
        """Suppress default access logging because it can contain sensitive URLs."""

    def send_error(self, *_arguments, **_keywords):
        """Keep unsupported methods on the same uninformative authorization response."""
        self._respond(401)

    def _read_body(self):
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return None
        if content_length < 0 or content_length > MAX_REQUEST_BYTES:
            return None
        try:
            body = self.rfile.read(content_length)
            if len(body) != content_length:
                return None
            return body
        except OSError:
            return None

    def _respond(self, status: int) -> None:
        body = json.dumps({"request_id": secrets.token_hex(8)}, separators=(",", ":")).encode("ascii")
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def authorize_body(body: bytes, secret: str, *, now: int) -> int:
    """Authorize a bounded MediaMTX request body without emitting request details."""
    if not isinstance(body, bytes) or len(body) > MAX_REQUEST_BYTES:
        return 401
    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return 401
    if not isinstance(payload, dict) or set(payload) != _ALLOWED_FIELDS:
        return 401
    return 200 if _allows_read(payload, secret, now=now) else 401


def _allows_read(payload: dict, secret: str, *, now: int) -> bool:
    token = payload.get("token")
    if not isinstance(token, str) or not token:
        return False
    if token.startswith("Bearer "):
        token = token[len("Bearer "):]
    if not token or any(not isinstance(payload.get(field), str) for field in payload):
        return False
    if payload.get("action") != "read" or payload.get("path") != "gate":
        return False
    if "protocol" in payload and payload["protocol"] != "webrtc":
        return False
    try:
        validate_media_token(token, secret, now=now)
    except TokenValidationError:
        return False
    return True


def validated_auth_environment(environment) -> dict[str, str]:
    """Return only the exact effective auth settings or fail closed."""
    return validate_auth_environment(relevant_auth_environment(environment))


def main() -> None:
    parser = argparse.ArgumentParser(description="Loopback MediaMTX authorization sidecar")
    parser.add_argument("--host", default=LOOPBACK_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    arguments = parser.parse_args()
    if arguments.host != LOOPBACK_HOST:
        parser.error("the media auth sidecar must bind 127.0.0.1")
    if not 1 <= arguments.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        environment = validated_auth_environment(os.environ)
    except MediaConfigError as error:
        parser.error(str(error))
    secret = environment["GATE_MEDIA_HMAC_SECRET"]
    server = MediaAuthServer((arguments.host, arguments.port), secret)
    publisher = MediaHealthPublisher(
        "/run/gate-media/capabilities.json",
        environment,
    )
    server.logger.info("media authorization sidecar started on loopback")
    publisher.start()
    try:
        server.serve_forever()
    finally:
        publisher.stop()
        server.server_close()


if __name__ == "__main__":
    main()
