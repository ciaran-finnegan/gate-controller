"""Entrypoint for the isolated MediaMTX HTTP authorization sidecar."""

import argparse
import hmac
import json
import logging
import os
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gate_media_config import (
    MediaConfigError,
    relevant_auth_environment,
    relevant_talk_credential_environment,
    validate_auth_environment,
    validate_talk_credential_environment,
)

from .capabilities import MediaHealthPublisher
from .token import SESSION_ACTIONS, TokenValidationError, _unique_object, validate_media_token


LOOPBACK_HOST = "127.0.0.1"
TALK_PATH = "talk"
DEFAULT_PORT = 9189
MAX_REQUEST_BYTES = 8 * 1024
_ALLOWED_FIELDS = frozenset({
    "user", "password", "token", "ip", "action", "path", "protocol", "id", "query", "userAgent",
})


class MediaAuthServer(ThreadingHTTPServer):
    """HTTP server that retains only its signing secret and an opaque logger."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, secret: str, *, talk_credential=None, logger=None):
        self.secret = secret
        self.talk_credential = talk_credential
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
            body, self.server.secret, now=int(time.time()),
            talk_credential=self.server.talk_credential,
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


def authorize_body(body: bytes, secret: str, *, now: int, talk_credential=None) -> int:
    """Authorize a bounded MediaMTX request body without emitting request details.

    ``talk_credential`` is this host's loopback RTSP credential, or None on a
    host that has none. None denies the `talk` read outright: a sidecar that
    cannot tell gate-camera-control apart from any other local process must not
    hand either of them the operator's microphone.
    """
    if not isinstance(body, bytes) or len(body) > MAX_REQUEST_BYTES:
        return 401
    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return 401
    if not isinstance(payload, dict) or set(payload) != _ALLOWED_FIELDS:
        return 401
    return 200 if _allows_request(payload, secret, now=now,
                                  talk_credential=talk_credential) else 401


def _allows_request(payload: dict, secret: str, *, now: int, talk_credential) -> bool:
    if any(not isinstance(payload.get(field), str) for field in payload):
        return False
    if _allows_local_rtsp(payload, talk_credential):
        return True
    return _allows_webrtc_session(payload, secret, now=now)


def _allows_local_rtsp(payload: dict, talk_credential) -> bool:
    if payload["protocol"] != "rtsp" or payload["ip"] != LOOPBACK_HOST:
        return False
    if payload["query"]:
        return False
    operation = (payload["action"], payload["path"])
    if operation == ("read", TALK_PATH):
        return _matches_talk_credential(payload, talk_credential)
    if payload["user"] or payload["password"] or payload["token"]:
        return False
    return operation in {
        ("read", "camera"),
        ("read", "clear"),
        ("publish", "gate"),
    }


def _matches_talk_credential(payload: dict, talk_credential) -> bool:
    """Only the holder of this host's credential may read the talk path.

    Everything else on loopback is a camera stream or the transcoder's own
    publish; `talk` carries the operator's live microphone, so it is the one
    path where being a process on the Pi is not enough. Both halves are
    compared in constant time and neither is ever logged.

    `token` is not required to be empty here, as it is for the anonymous paths.
    Verified against MediaMTX 1.19.3 on 2026-09-20: for an RTSP Basic request it
    fills `token` with the password it received as well as `password`, so
    demanding an empty one refused the very credential it had just forwarded.
    It is still pinned to that one value -- a caller cannot smuggle a session
    token past this by putting it there.
    """
    if not talk_credential:
        return False
    username, password = talk_credential
    # Every field is compared, and the results combined afterwards, so the time
    # this takes says nothing about which half was wrong.
    matches = _constant_time_equal(payload["user"], username)
    matches &= _constant_time_equal(payload["password"], password)
    matches &= not payload["token"] or _constant_time_equal(payload["token"], password)
    return matches


def _constant_time_equal(value: str, expected: str) -> bool:
    """Compare a field MediaMTX forwarded against a validated credential half.

    The value came out of JSON and can be any string; `hmac.compare_digest`
    raises TypeError on a str with non-ASCII in it, which would have taken the
    sidecar's handler thread down instead of answering 401. The credential is
    validated URL-safe ASCII, so anything that will not encode cannot be it.
    """
    try:
        candidate = value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(candidate, expected.encode("ascii"))


def validated_talk_credential(environment):
    """This host's talk credential as a (user, password) pair, or None.

    A host that has not been bootstrapped with one has no `talk` path that
    works, which is the safe end of the trade: video and listen are unaffected,
    and one installer or updater run mints the credential.
    """
    selected = relevant_talk_credential_environment(environment)
    if not selected:
        return None
    settings = validate_talk_credential_environment(selected)
    return (settings["GATE_TALK_RTSP_USERNAME"], settings["GATE_TALK_RTSP_PASSWORD"])


def _allows_webrtc_session(payload: dict, secret: str, *, now: int) -> bool:
    """A browser reading the gate, or publishing talk audio, on a matching token."""
    token = payload.get("token")
    if not isinstance(token, str) or not token:
        return False
    if token.startswith("Bearer "):
        token = token[len("Bearer "):]
    if not token:
        return False
    path = payload.get("path")
    if path not in SESSION_ACTIONS or [payload.get("action")] != SESSION_ACTIONS[path]:
        return False
    if "protocol" in payload and payload["protocol"] != "webrtc":
        return False
    try:
        validate_media_token(token, secret, now=now, path=path)
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
    try:
        talk_credential = validated_talk_credential(os.environ)
    except MediaConfigError as error:
        parser.error(str(error))
    secret = environment["GATE_MEDIA_HMAC_SECRET"]
    server = MediaAuthServer(
        (arguments.host, arguments.port), secret, talk_credential=talk_credential,
    )
    publisher = MediaHealthPublisher(
        "/run/gate-media/capabilities.json",
        environment,
    )
    server.logger.info("media authorization sidecar started on loopback")
    if talk_credential is None:
        # Whether one exists, never what it is.
        server.logger.warning(
            "no talk credential is configured; push-to-talk will be refused until "
            "deployment/install-media.sh or install-camera-control.sh is re-run"
        )
    publisher.start()
    try:
        server.serve_forever()
    finally:
        publisher.stop()
        server.server_close()


if __name__ == "__main__":
    main()
