"""Bounded Reolink api.cgi client with a cached login token and a circuit breaker.

The RLC-810A firmware answers HTTP 502 for roughly a minute after repeated
``Login`` calls, so this client holds exactly one token, allows one in-flight
login, enforces a minimum re-login interval, and opens a circuit breaker instead
of retrying a busy camera.  Nothing here logs or returns a camera payload.
"""

import json
import math
import os
import secrets
import ssl
import stat
import threading
import time
from http.client import HTTPException, HTTPSConnection
from urllib.parse import quote

from .atomic import atomic_write


API_PATH = "/cgi-bin/api.cgi"
ALLOWED_COMMANDS = frozenset({
    "Login", "GetIrLights", "SetIrLights", "GetWhiteLed", "SetWhiteLed", "Snap",
})
IR_STATES = ("Auto", "Off")
SPOTLIGHT_STATES = ("On", "Off")
# `mode 0` is off/manual: the lamp is exactly what `state` says, and the
# camera's own "auto on AI detection at night" (`mode 1`) cannot re-arm it.
# Every write sends it, so nothing lights the gate behind an expiring lease.
SPOTLIGHT_MANUAL_MODE = 0
DEFAULT_SPOTLIGHT_BRIGHTNESS = 100
DEFAULT_TIMEOUT_SECONDS = 5.0
MIN_LOGIN_INTERVAL_SECONDS = 60.0
BREAKER_SECONDS = 60.0
TOKEN_SAFETY_MARGIN_SECONDS = 60.0
MAX_TOKEN_LEASE_SECONDS = 24 * 60 * 60
MAX_RESPONSE_BYTES = 64 * 1024
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_TOKEN_FILE_BYTES = 4 * 1024
_TOKEN_FILE_KEYS = frozenset({"token", "expires_at", "last_login_at"})
_JPEG_MAGIC = b"\xff\xd8\xff"
_AUTH_ERROR_CODES = frozenset({-6, -7, -14})


class CameraError(Exception):
    """Base class for every camera failure surfaced to the HTTP layer."""

    code = "camera_error"
    retry_after = None


class CameraBusy(CameraError):
    """The camera is rate limiting us, or our own login throttle is holding."""

    code = "camera_busy"

    def __init__(self, retry_after: int = int(BREAKER_SECONDS)):
        super().__init__("camera_busy")
        self.retry_after = max(1, int(retry_after))


class CameraUnreachable(CameraError):
    """The camera did not answer at all."""

    code = "camera_unreachable"


class TokenCache:
    """Owner-only persistence for one login token, and for the last login attempt.

    The last login *attempt* is persisted separately from the token because the
    two fail apart: a restart that finds no usable token would otherwise start
    with no memory of how recently this service last hit ``Login``, and this
    firmware answers 502 for about a minute after repeated logins.  A crash loop
    would then log in on every start and hold the camera in that 502 window.
    """

    def __init__(self, path):
        self._path = None if path is None else os.fspath(path)

    @property
    def path(self):
        return self._path

    def load(self):
        """Return ``(token, expires_at, last_login_at)`` or ``None``.

        ``token`` is ``None`` when the file records only a login attempt.  A bad
        file is simply ignored.
        """
        if self._path is None:
            return None
        flags = os.O_RDONLY | os.O_NONBLOCK
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._path, flags)
        except OSError:
            return None
        try:
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or not 0 < metadata.st_size <= MAX_TOKEN_FILE_BYTES):
                return None
            body = os.read(descriptor, metadata.st_size)
        except OSError:
            return None
        finally:
            os.close(descriptor)
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        if (not isinstance(decoded, dict)
                or not {"token", "expires_at"} <= set(decoded) <= _TOKEN_FILE_KEYS):
            return None
        token, expires_at = decoded["token"], decoded["expires_at"]
        if token is not None and (not isinstance(token, str)
                                  or not 0 < len(token) <= 512):
            return None
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
            return None
        last_login_at = decoded.get("last_login_at")
        if last_login_at is not None and (isinstance(last_login_at, bool)
                                          or not isinstance(last_login_at,
                                                            (int, float))):
            return None
        return (
            token,
            float(expires_at),
            None if last_login_at is None else float(last_login_at),
        )

    def save(self, token, expires_at: float, last_login_at=None) -> None:
        """Record the token, if any, and when the last login was attempted."""
        if self._path is None:
            return
        body = json.dumps({
            "token": token,
            "expires_at": float(expires_at),
            "last_login_at": (None if last_login_at is None else float(last_login_at)),
        }, separators=(",", ":")).encode("utf-8")
        atomic_write(self._path, body, 0o600)


class ReolinkClient:
    """The only object in the deployment that holds camera API credentials."""

    def __init__(self, host, username, password, *, token_path=None, clock=time.time,
                 connection_factory=None, timeout=DEFAULT_TIMEOUT_SECONDS,
                 min_login_interval=MIN_LOGIN_INTERVAL_SECONDS,
                 breaker_seconds=BREAKER_SECONDS, journal=None):
        if not host or not username or not password:
            raise ValueError("camera host and credentials are required")
        self._host = host
        self._username = username
        self._password = password
        self._clock = clock
        self._timeout = float(timeout)
        self._min_login_interval = float(min_login_interval)
        self._breaker_seconds = float(breaker_seconds)
        self._journal = journal or (lambda *_arguments, **_keywords: None)
        self._connection_factory = connection_factory or self._https_connection
        self._cache = TokenCache(token_path)
        self._lock = threading.Lock()
        self._token = None
        self._token_expires_at = 0.0
        self._last_login_at = None
        self._breaker_until = 0.0
        self.login_count = 0
        cached = self._cache.load()
        if cached is not None:
            token, expires_at, last_login_at = cached
            if token is not None:
                self._token, self._token_expires_at = token, expires_at
            # Restored even when the token is gone: without it a restart with no
            # usable token logs in immediately, and a crash loop becomes a login
            # storm the camera answers with 502 for a minute at a time.
            self._last_login_at = last_login_at

    @property
    def host(self):
        return self._host

    def breaker_seconds_remaining(self) -> int:
        remaining = self._breaker_until - self._clock()
        return max(0, math.ceil(remaining))

    def ir_state(self) -> str:
        """Return the camera's current IR illuminator state."""
        value = self._command("GetIrLights", 1, {"channel": 0})
        lights = value.get("IrLights") if isinstance(value, dict) else None
        state = lights.get("state") if isinstance(lights, dict) else None
        if state not in IR_STATES:
            raise CameraError("camera reported an unknown IR state")
        return state

    def set_ir_state(self, state: str) -> None:
        """Set the IR illuminator to one of the exactly two supported states."""
        if state not in IR_STATES:
            raise ValueError("IR state must be Auto or Off")
        self._command("SetIrLights", 0, {"IrLights": {"state": state}})

    def spotlight_state(self) -> str:
        """Return the white spotlight's current state as `On` or `Off`.

        The camera answers with an integer `state` (1 lit, 0 dark) beside a
        `mode`, a `bright`, a `LightingSchedule` and a `wlAiDetectType`. Only
        `state` is read, and it is mapped to the two words the lease speaks;
        nothing else in that payload leaves this client.
        """
        value = self._command("GetWhiteLed", 1, {"channel": 0})
        white_led = value.get("WhiteLed") if isinstance(value, dict) else None
        state = white_led.get("state") if isinstance(white_led, dict) else None
        if isinstance(state, bool) or state not in (0, 1):
            raise CameraError("camera reported an unknown spotlight state")
        return "On" if state == 1 else "Off"

    def set_spotlight_state(
        self, state: str, brightness: int = DEFAULT_SPOTLIGHT_BRIGHTNESS
    ) -> None:
        """Light or extinguish the white spotlight, always in manual mode.

        `LightingSchedule` and `wlAiDetectType` are deliberately absent from the
        write: they are the operator's own camera configuration, and echoing
        back a copy this service invented would overwrite it.
        """
        if state not in SPOTLIGHT_STATES:
            raise ValueError("spotlight state must be On or Off")
        if (isinstance(brightness, bool) or not isinstance(brightness, int)
                or not 1 <= brightness <= 100):
            raise ValueError("spotlight brightness must be a whole number 1-100")
        self._command("SetWhiteLed", 0, {"WhiteLed": {
            "channel": 0,
            "state": 1 if state == "On" else 0,
            "mode": SPOTLIGHT_MANUAL_MODE,
            "bright": int(brightness),
        }})

    def snapshot(self) -> bytes:
        """Return one bounded 4K JPEG from the camera's Snap endpoint."""
        token = self._ensure_token()
        query = (
            f"cmd=Snap&channel=0&rs={secrets.token_hex(8)}"
            f"&token={_quote_token(token)}"
        )
        status, body = self._request("GET", query, None, MAX_SNAPSHOT_BYTES)
        if status == 401:
            self._invalidate_token()
            token = self._ensure_token()
            query = (
                f"cmd=Snap&channel=0&rs={secrets.token_hex(8)}"
                f"&token={_quote_token(token)}"
            )
            status, body = self._request("GET", query, None, MAX_SNAPSHOT_BYTES)
        if status != 200:
            raise CameraError("camera refused the snapshot request")
        if not body.startswith(_JPEG_MAGIC):
            raise CameraError("camera returned a non-JPEG snapshot")
        return body

    def _command(self, command: str, action: int, param: dict):
        if command not in ALLOWED_COMMANDS:
            raise ValueError("camera command is not on the allowlist")
        token = self._ensure_token()
        value, authentication_failed = self._invoke(command, action, param, token)
        if not authentication_failed:
            return value
        self._invalidate_token()
        token = self._ensure_token()
        value, authentication_failed = self._invoke(command, action, param, token)
        if authentication_failed:
            raise CameraError("camera rejected the cached credentials")
        return value

    def _invoke(self, command: str, action: int, param: dict, token: str):
        body = json.dumps(
            [{"cmd": command, "action": action, "param": param}], separators=(",", ":")
        ).encode("utf-8")
        query = f"cmd={command}&token={_quote_token(token)}"
        status, payload = self._request("POST", query, body, MAX_RESPONSE_BYTES)
        if status == 401:
            return None, True
        if status != 200:
            raise CameraError("camera returned an unexpected status")
        return _decode_command_response(command, payload)

    def _ensure_token(self) -> str:
        with self._lock:
            self._raise_if_breaker_open()
            now = self._clock()
            if self._token and self._token_expires_at - TOKEN_SAFETY_MARGIN_SECONDS > now:
                return self._token
            if (self._last_login_at is not None
                    and now - self._last_login_at < self._min_login_interval):
                remaining = self._min_login_interval - (now - self._last_login_at)
                self._journal("login_throttled", retry_after=max(1, math.ceil(remaining)))
                raise CameraBusy(math.ceil(remaining))
            self._last_login_at = now
            # Persisted before the call, not after: a login that hangs, fails or
            # takes the process down with it still has to count against the
            # re-login floor on the next start.
            self._cache.save(None, 0.0, now)
            token, lease_seconds = self._login()
            self._token = token
            self._token_expires_at = now + lease_seconds
            self.login_count += 1
            self._cache.save(token, self._token_expires_at, now)
            self._journal("login", lease_seconds=int(lease_seconds))
            return token

    def _login(self):
        body = json.dumps([{
            "cmd": "Login",
            "action": 0,
            "param": {"User": {"userName": self._username, "password": self._password}},
        }], separators=(",", ":")).encode("utf-8")
        status, payload = self._request("POST", "cmd=Login", body, MAX_RESPONSE_BYTES)
        if status != 200:
            raise CameraError("camera refused the login request")
        value, authentication_failed = _decode_command_response("Login", payload)
        if authentication_failed or not isinstance(value, dict):
            raise CameraError("camera rejected the configured credentials")
        token = value.get("Token")
        if not isinstance(token, dict):
            raise CameraError("camera returned no login token")
        name, lease = token.get("name"), token.get("leaseTime")
        if (not isinstance(name, str) or not 0 < len(name) <= 512
                or isinstance(lease, bool) or not isinstance(lease, (int, float))
                or not 0 < lease <= MAX_TOKEN_LEASE_SECONDS):
            raise CameraError("camera returned an unusable login token")
        return name, float(lease)

    def _invalidate_token(self) -> None:
        with self._lock:
            self._token = None
            self._token_expires_at = 0.0
            # Drop the token but keep the login timestamp, so discarding a
            # rejected token cannot buy a caller a free re-login on restart.
            self._cache.save(None, 0.0, self._last_login_at)

    def _raise_if_breaker_open(self) -> None:
        remaining = self._breaker_until - self._clock()
        if remaining > 0:
            raise CameraBusy(math.ceil(remaining))

    def _open_breaker(self) -> None:
        self._breaker_until = self._clock() + self._breaker_seconds
        self._journal("breaker_open", retry_after=int(self._breaker_seconds))

    def _request(self, method: str, query: str, body, maximum_bytes: int):
        self._raise_if_breaker_open()
        connection = self._connection_factory(self._host, self._timeout)
        headers = {"Accept": "*/*", "Connection": "close"}
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        try:
            connection.request(method, f"{API_PATH}?{query}", body=body, headers=headers)
            response = connection.getresponse()
            status = response.status
            declared = response.getheader("Content-Length")
            if declared is not None:
                try:
                    if int(declared) > maximum_bytes:
                        raise CameraError("camera response is too large")
                except ValueError as error:
                    raise CameraError("camera response length is invalid") from error
            payload = response.read(maximum_bytes + 1)
        except CameraError:
            raise
        except (OSError, HTTPException) as error:
            # A camera that does not answer opens the breaker exactly as a 502
            # does. Without this, every read retried a 5 s connect attempt, and
            # an expired lease's revert queued behind those attempts -- IR
            # staying on past its expiry is the one failure the lease exists to
            # prevent.
            self._open_breaker()
            raise CameraUnreachable("camera did not answer") from error
        finally:
            try:
                connection.close()
            except (OSError, HTTPException):
                pass
        if status in {502, 503}:
            self._open_breaker()
            raise CameraBusy(int(self._breaker_seconds))
        if len(payload) > maximum_bytes:
            raise CameraError("camera response is too large")
        return status, payload

    def _https_connection(self, host, timeout):
        # The camera presents a self-signed certificate on a fixed private
        # address, and the unit pins the reachable peers to loopback plus that
        # address, so verification is enforced by the network boundary.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return HTTPSConnection(host, timeout=timeout, context=context)


def _decode_command_response(command: str, payload: bytes):
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise CameraError("camera response is not JSON") from error
    if not isinstance(decoded, list) or len(decoded) != 1:
        raise CameraError("camera response is not a single-command array")
    entry = decoded[0]
    if not isinstance(entry, dict) or entry.get("cmd") != command:
        raise CameraError("camera answered a different command")
    code = entry.get("code")
    if code == 0:
        return entry.get("value"), False
    detail = entry.get("error")
    error_code = detail.get("rspCode") if isinstance(detail, dict) else None
    if error_code in _AUTH_ERROR_CODES:
        return None, True
    raise CameraError("camera reported a command failure")


def _quote_token(token: str) -> str:
    return quote(token, safe="")
