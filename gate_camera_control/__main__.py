"""Loopback HTTP entrypoint for the isolated gate camera-control service.

The service binds 127.0.0.1 only.  Remote callers reach it through the existing
Cloudflare Tunnel with its own Access application; nothing here authenticates a
caller, exactly as the controller's own loopback command server does not.
"""

import argparse
import json
import logging
import math
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

from .baichuan import BaichuanClient
from .ir import DIRECT_READ_MAX_AGE_SECONDS, IrController, RevertWorker
from .reolink import IR_STATES, CameraBusy, CameraError, CameraUnreachable, ReolinkClient
from .state import STATE_PATH, StatePublisher
from .talk import TalkBusySession, TalkController, TalkUnavailable


LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8767
MAX_REQUEST_BYTES = 4096
# Per-connection socket timeout. Long enough for cloudflared to send a request
# it has already opened a connection for, short enough that a half-open
# connection cannot hold one of the service's few threads indefinitely.
REQUEST_TIMEOUT_SECONDS = 10
RUNTIME_ROOT = "/run/gate-camera"
TOKEN_PATH = f"{RUNTIME_ROOT}/token.json"
# The lease outlives the machine, so it cannot live on tmpfs. `/run` is recreated
# empty at boot, which is exactly the moment a lease matters most: the camera is
# separately powered, so a power cut during a lease used to leave it holding the
# leased state with nothing left on the Pi that knew to put it back.
STATE_ROOT = "/var/lib/gate-camera"
LEASE_PATH = f"{STATE_ROOT}/lease.json"
SNAPSHOT_MIN_INTERVAL_SECONDS = 2.0
IDEMPOTENCY_TTL_SECONDS = 300.0
IDEMPOTENCY_WAIT_SECONDS = 15.0
MAX_IDEMPOTENCY_ENTRIES = 64
MAX_IDEMPOTENCY_KEY_LENGTH = 128
# Generous for one operator on one page, and far below what it takes to keep a
# 5 s camera call permanently in front of an expiring lease's revert.
STATE_BURST = 10
STATE_REFILL_PER_SECOND = 2.0
IR_BURST = 6
IR_REFILL_PER_SECOND = 0.5
# Arming is one Baichuan login; a page cannot usefully do it more than this.
TALK_BURST = 6
TALK_REFILL_PER_SECOND = 0.5
_IR_BODY_FIELDS = frozenset({"state", "lease_minutes", "ttl_seconds", "idempotency_key"})
_TALK_BODY_FIELDS = frozenset({"max_seconds"})
_STATE_PATHS = frozenset({"/camera/state", "/camera/ir"})
_SNAPSHOT_PATHS = frozenset({"/camera/snap", "/camera/snapshot"})
_TALK_PATH = "/camera/talk"


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
                 snapshot_min_interval=SNAPSHOT_MIN_INTERVAL_SECONDS,
                 state_rate=None, ir_rate=None, talk=None, talk_rate=None):
        self._controller = controller
        self._client = client
        self._talk = talk
        self._clock = clock
        self._logger = logger or logging.getLogger("gate_camera_control")
        self._snapshot_min_interval = float(snapshot_min_interval)
        self._snapshot_lock = threading.Lock()
        self._last_snapshot_at = None
        # Reads and lease changes are bounded independently of the snapshot.
        # Nothing behind this service scales: an unbounded read loop used to put
        # a fresh 5 s camera call in front of an expired lease's revert.
        self._state_limiter = state_rate or _RateLimiter(
            STATE_BURST, STATE_REFILL_PER_SECOND, clock=clock
        )
        self._ir_limiter = ir_rate or _RateLimiter(
            IR_BURST, IR_REFILL_PER_SECOND, clock=clock
        )
        self._talk_limiter = talk_rate or _RateLimiter(
            TALK_BURST, TALK_REFILL_PER_SECOND, clock=clock
        )
        self._idempotency = OrderedDict()
        self._idempotency_lock = threading.Lock()

    @property
    def controller(self):
        return self._controller

    @property
    def talk(self):
        return self._talk

    def talk_state(self) -> dict:
        self._admit(self._state_limiter, "state_rate_limited")
        return self._talk_envelope(self._talk_snapshot())

    def arm_talk(self, payload) -> dict:
        """Open one bounded talk session; the Worker then hands the browser its token."""
        request = _parse_talk_request(payload)
        self._admit(self._talk_limiter, "talk_rate_limited")
        if self._talk is None:
            raise TalkUnavailable("not_enabled")
        snapshot = self._talk.arm(request["max_seconds"])
        response = self._talk_envelope(snapshot)
        response["status"] = "armed"
        return response

    def release_talk(self) -> dict:
        self._admit(self._talk_limiter, "talk_rate_limited")
        snapshot = self._talk_snapshot() if self._talk is None else self._talk.release()
        response = self._talk_envelope(snapshot)
        response["status"] = "released"
        return response

    def _talk_snapshot(self) -> dict:
        if self._talk is None:
            return {
                "available": False, "reason": "not_enabled", "active": False,
                "max_seconds": 0, "state": "idle", "session_id": None, "armed_at": None,
                "expires_at": None, "seconds_remaining": None, "last_outcome": None,
                "last_ended_at": None,
            }
        return self._talk.snapshot()

    def _talk_envelope(self, talk_snapshot) -> dict:
        return {
            "observed_at": datetime.now(timezone.utc).replace(
                microsecond=0
            ).isoformat(),
            "talk": talk_snapshot,
        }

    def state(self) -> dict:
        self._admit(self._state_limiter, "state_rate_limited")
        snapshot = self._controller.state(refresh_max_age=DIRECT_READ_MAX_AGE_SECONDS)
        return self._envelope(snapshot)

    def set_ir(self, payload) -> dict:
        request = _parse_ir_request(payload, self._controller.max_lease_minutes)
        key = request["idempotency_key"]
        if key is None:
            self._admit(self._ir_limiter, "ir_rate_limited")
            return self._completed(
                self._controller.set_state(request["state"], request["lease_minutes"])
            )
        # Reserved before the camera is touched, so two concurrent posts of one
        # key produce one lease, not two: the loser waits for the winner and
        # then answers from the lease the winner actually created -- or repeats
        # the failure the winner met, which is the only honest answer when the
        # camera call the key stands for did not succeed.
        call, owned = self._reserve(key)
        if not owned:
            return self._replay(key, call)
        try:
            self._admit(self._ir_limiter, "ir_rate_limited")
        except RateLimited as error:
            # Refused before the camera was touched, so the key stands for
            # nothing: it is released for a later retry, and only the caller
            # already queued behind it is told what happened.
            self._release(key, call)
            call.fail(error)
            raise
        try:
            snapshot = self._controller.set_state(
                request["state"], request["lease_minutes"]
            )
        except BaseException as error:
            # The camera was called and did not confirm. The key keeps that
            # outcome for its whole life, so a replay is answered with the same
            # failure and the same status rather than with a "completed" that
            # carries the state from before the change.
            call.fail(error)
            raise
        call.complete()
        response = self._completed(snapshot)
        response["idempotency_key"] = key
        return response

    def _completed(self, ir_snapshot) -> dict:
        response = self._envelope(ir_snapshot)
        response["status"] = "completed"
        return response

    def _admit(self, limiter, stage) -> None:
        retry_after = limiter.reject_after()
        if retry_after is None:
            return
        journal(self._logger, stage, retry_after=retry_after)
        raise RateLimited(retry_after)

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

    def _replay(self, key, call) -> dict:
        """Answer a repeat post from the live lease, never from a frozen copy.

        A stored response would keep serving the `observed_at` and the
        `lease_seconds_remaining` of the original call, so a client that retried
        a minute later would be told the lease had a minute more left than it
        really did -- and would leave IR on trusting it.

        Three outcomes, and only one of them is `completed`. The owner failed:
        its error is raised here too, with the status it earned, because
        answering `completed` with a snapshot taken *before* the change told the
        app a change had landed when the camera had refused it. The owner is
        still in flight when the wait runs out: neither answer is known to be
        true, so the caller is told exactly that.
        """
        if not call.event.wait(timeout=IDEMPOTENCY_WAIT_SECONDS):
            raise IdempotentCallInFlight()
        if call.error is not None:
            raise call.error
        response = self._completed(self._controller.snapshot())
        response["idempotency_key"] = key
        return response

    def _reserve(self, key):
        """Return ``(call, owned)``: the shared outcome record and who owns it."""
        with self._idempotency_lock:
            self._expire_locked()
            entry = self._idempotency.get(key)
            if entry is not None:
                return entry[1], False
            call = _IdempotentCall()
            self._idempotency[key] = (self._clock(), call)
            while len(self._idempotency) > MAX_IDEMPOTENCY_ENTRIES:
                self._idempotency.popitem(last=False)
            return call, True

    def _release(self, key, call) -> None:
        """Drop a key whose call never reached the camera, so it may be retried.

        The record itself is settled by the caller either way: a waiter holds a
        reference to it, so it is woken even when the key has been evicted from
        the map by the entry cap or by the TTL.
        """
        with self._idempotency_lock:
            if self._idempotency.get(key, (None, None))[1] is call:
                self._idempotency.pop(key, None)

    def _expire_locked(self) -> None:
        cutoff = self._clock() - IDEMPOTENCY_TTL_SECONDS
        for key in [k for k, (at, _) in self._idempotency.items() if at < cutoff]:
            self._idempotency.pop(key, None)


class _IdempotentCall:
    """One idempotency key's shared outcome: in flight, completed, or failed."""

    def __init__(self):
        self.event = threading.Event()
        self.error = None

    def complete(self) -> None:
        self.event.set()

    def fail(self, error) -> None:
        self.error = error
        self.event.set()


class IdempotentCallInFlight(Exception):
    """A replay whose owner had still not answered when the wait ran out."""


class _RateLimiter:
    """A token bucket: `burst` requests at once, then `refill` a second."""

    def __init__(self, burst: int, refill_per_second: float, *, clock=time.time):
        self._burst = float(burst)
        self._refill = float(refill_per_second)
        self._clock = clock
        self._tokens = float(burst)
        self._updated_at = clock()
        self._lock = threading.Lock()

    def reject_after(self):
        """Spend one token, or return the whole seconds until one exists."""
        with self._lock:
            now = self._clock()
            self._tokens = min(
                self._burst, self._tokens + (now - self._updated_at) * self._refill
            )
            self._updated_at = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return None
            return max(1, math.ceil((1.0 - self._tokens) / self._refill))


class RateLimited(Exception):
    """The caller is over its budget and is told exactly how long to wait."""

    def __init__(self, retry_after: int):
        super().__init__("rate_limited")
        self.retry_after = max(1, int(retry_after))


class SnapshotRateLimited(RateLimited):
    """One snapshot per interval; the caller is told exactly how long to wait."""


def _parse_talk_request(payload) -> dict:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict) or not set(payload) <= _TALK_BODY_FIELDS:
        raise ValueError("invalid_request")
    max_seconds = payload.get("max_seconds")
    if max_seconds is not None and (isinstance(max_seconds, bool)
                                    or not isinstance(max_seconds, int)):
        raise ValueError("invalid_request")
    return {"max_seconds": max_seconds}


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
    # Every socket read on a connection is bounded, so a client that opens one
    # and then says nothing -- or stops mid-headers, or holds a keep-alive
    # connection open after its last request -- is dropped instead of parking a
    # thread for ever. `TasksMax=64` makes twenty such connections an outage.
    # It bounds reads and writes, not the handler: a `POST /camera/ir` that has
    # to log in first takes 10-20 s inside the service and is unaffected.
    timeout = REQUEST_TIMEOUT_SECONDS

    def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        path = self._route()
        if path is None:
            return
        if path in _STATE_PATHS:
            self._guarded(lambda: self._respond_json(200, self.server.service.state()))
        elif path in _SNAPSHOT_PATHS:
            self._guarded(self._respond_snapshot)
        elif path == _TALK_PATH:
            self._guarded(lambda: self._respond_json(200, self.server.service.talk_state()))
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
        if path == _TALK_PATH:
            payload = self._read_json_body(allow_empty=True)
            if payload is _INVALID:
                return
            self._guarded(lambda: self._respond_json(
                200, self.server.service.arm_talk(payload)
            ))
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

    def do_HEAD(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        """Answer the headers of the matching GET, and no body.

        Without this, BaseHTTPRequestHandler answers 501 through `send_error`,
        and the body it writes for a HEAD desynchronises a keep-alive
        connection -- through cloudflared, the next response on that connection
        is read as the tail of this one.
        """
        path = self._route()
        if path is None:
            return
        if path in _STATE_PATHS:
            self._guarded(lambda: self._respond_json(
                200, self.server.service.state(), body_only_headers=True
            ))
        elif path == _TALK_PATH:
            self._guarded(lambda: self._respond_json(
                200, self.server.service.talk_state(), body_only_headers=True
            ))
        elif path in _SNAPSHOT_PATHS:
            # Deliberately not a snapshot: a HEAD must not spend the camera's
            # one-every-two-seconds budget to report a length nobody reads.
            self._respond(200, "image/jpeg", b"", body_only_headers=True)
        else:
            self._respond_json(404, {"error": "not_found"}, body_only_headers=True)

    def do_PUT(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        self._respond_json(405, {"error": "method_not_allowed"})

    def do_DELETE(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        path = self._route()
        if path is None:
            return
        if path == _TALK_PATH:
            self._guarded(lambda: self._respond_json(
                200, self.server.service.release_talk()
            ))
            return
        self._respond_json(405, {"error": "method_not_allowed"})

    def log_message(self, _format, *_arguments):
        """Access logging is suppressed; the service journals its own decisions."""

    def send_error(self, *_arguments, **_keywords):
        """Answer every framing failure with one bounded JSON body, then close.

        `send_error` is the path a malformed request line, an over-long header
        block or an unsupported method takes. On that path the parser has left
        `request_version` at HTTP/0.9, under which `send_response_only` and
        `send_header` emit nothing at all -- so the old override answered a bad
        request line with a bare JSON body and no status line. The connection is
        then closed, because a request that never parsed leaves no way to know
        where the next one on this connection begins.
        """
        self.request_version = self.protocol_version
        self.close_connection = True
        self._respond_json(400, {"error": "invalid_request"})

    def _route(self):
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment:
            self._respond_json(404, {"error": "not_found"})
            return None
        return parsed.path

    def _read_json_body(self, *, allow_empty=False):
        try:
            length = int(self.headers.get("Content-Length", ""))
        except (TypeError, ValueError):
            if allow_empty and self.headers.get("Content-Length") is None:
                return None
            self._respond_json(400, {"error": "invalid_request"})
            return _INVALID
        if length == 0 and allow_empty:
            return None
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
        except RateLimited as error:
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
        except TalkBusySession:
            journal(self.server.logger, "talk_busy")
            self._respond_json(409, {"error": "talk_busy"})
        except TalkUnavailable as error:
            journal(self.server.logger, "talk_unavailable", reason=error.reason)
            self._respond_json(503, {"error": "talk_unavailable", "reason": error.reason})
        except IdempotentCallInFlight:
            # Not `completed`, and not a definite failure either: the first post
            # of this key is still talking to the camera. `502
            # camera_indeterminate` is the one answer the app records as
            # indeterminate rather than as a landed change or a refusal.
            journal(self.server.logger, "ir_idempotent_in_flight")
            self._respond_json(502, {"error": "camera_indeterminate"})
        except ValueError:
            self._respond_json(400, {"error": "invalid_request"})
        except Exception:
            journal(self.server.logger, "internal_error")
            self._respond_json(500, {"error": "internal_error"})

    def _respond_json(self, status, payload, *, retry_after=None,
                      body_only_headers=False) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._respond(status, "application/json", body, retry_after=retry_after,
                      body_only_headers=body_only_headers)

    def _respond(self, status, content_type, body, *, retry_after=None,
                 body_only_headers=False) -> None:
        try:
            self.send_response_only(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if retry_after is not None:
                self.send_header("Retry-After", str(int(retry_after)))
            if self.close_connection:
                # Announced rather than merely done, so an intermediary stops
                # reusing the connection instead of reading the next response
                # off a socket we are about to drop.
                self.send_header("Connection", "close")
            self.end_headers()
            if not body_only_headers:
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
                  logger=None, connection_factory=None, clock=time.time,
                  talk_client_factory=None, talk_options=None) -> CameraControlService:
    """Wire the client and the IR controller from validated settings.

    One clock is threaded through the whole service -- the token bucket, the
    snapshot interval, the lease expiry and the login throttle all read it. In
    production it is the wall clock. A caller that passes its own gets a
    service whose every deadline it controls, which is what lets the tests
    assert on the limiter rather than on how fast the machine happened to be.
    """
    logger = logger or logging.getLogger("gate_camera_control")
    client = ReolinkClient(
        settings["GATE_CAMERA_HOST"],
        settings["GATE_CAMERA_USERNAME"],
        settings["GATE_CAMERA_PASSWORD"],
        token_path=token_path,
        clock=clock,
        connection_factory=connection_factory,
        journal=lambda stage, **fields: journal(logger, stage, **fields),
    )
    controller = IrController(
        client,
        default_state=settings["GATE_CAMERA_IR_DEFAULT"],
        lease_path=lease_path,
        default_lease_minutes=int(settings["GATE_CAMERA_IR_LEASE_DEFAULT_MINUTES"]),
        max_lease_minutes=int(settings["GATE_CAMERA_IR_LEASE_MAX_MINUTES"]),
        clock=clock,
        journal=lambda stage, **fields: journal(logger, stage, **fields),
    )
    talk = None
    if settings.get("GATE_CAMERA_TALK_ENABLED") == "true":
        talk = TalkController(
            talk_client_factory or (lambda: BaichuanClient(
                settings["GATE_CAMERA_HOST"],
                settings["GATE_CAMERA_USERNAME"],
                settings["GATE_CAMERA_PASSWORD"],
            )),
            max_seconds=int(settings["GATE_CAMERA_TALK_MAX_SECONDS"]),
            clock=clock,
            journal=lambda stage, **fields: journal(logger, stage, **fields),
            **(talk_options or {}),
        )
    return CameraControlService(controller, client, clock=clock, logger=logger, talk=talk)


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
    # `mode=` is masked by the unit's UMask=0077, so a directory this call had
    # to create lands at 0700 and the controller loses the read access the whole
    # heartbeat path depends on. tmpfiles.d normally gets there first; this is
    # the case where it did not.
    os.chmod(RUNTIME_ROOT, 0o755)
    # The lease directory is the opposite: nothing outside this service ever
    # reads it, so it stays owner-only. `StateDirectory=gate-camera` in the unit
    # normally creates it; under `ProtectSystem=strict` this call can only
    # succeed when it did, which is the loud failure we want if it is missing.
    os.makedirs(STATE_ROOT, mode=0o700, exist_ok=True)
    os.chmod(STATE_ROOT, 0o700)
    service = build_service(settings, logger=logger)
    # A lease that outlived the previous process must never leave IR on.
    service.controller.restore_default_on_start()
    server = CameraControlServer((arguments.host, arguments.port), service, logger=logger)
    reverts = RevertWorker(service.controller)
    talk = service.talk
    publisher = StatePublisher(
        arguments.state_path, service.controller.snapshot,
        # One bounded observation behind the publication, so an idle camera is
        # reported as it is rather than as never-observed until someone asks.
        refresher=service.controller.refresh_observation,
        talk_provider=None if talk is None else talk.state_block,
        talk_refresher=None if talk is None else talk.refresh_if_due,
    )
    journal(logger, "started", port=arguments.port,
            ir_default=service.controller.default_state,
            lease_default_minutes=service.controller.default_lease_minutes,
            lease_max_minutes=service.controller.max_lease_minutes,
            talk_enabled=talk is not None,
            talk_max_seconds=None if talk is None else talk.max_seconds)
    reverts.start()
    publisher.start()
    try:
        server.serve_forever()
    finally:
        publisher.stop()
        reverts.stop()
        if talk is not None:
            talk.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
