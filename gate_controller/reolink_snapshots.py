"""Bounded Reolink HTTPS snapshots used only as OCR burst candidates."""

import errno
import ipaddress
import json
import logging
import math
import os
import secrets
import ssl
import stat
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from threading import Lock
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .images import is_decodable_jpeg


DEFAULT_REOLINK_SNAPSHOT_COUNT = 2
MAX_REOLINK_SNAPSHOT_COUNT = 4
DEFAULT_REOLINK_SNAPSHOT_TIMEOUT_SECONDS = 2.25
MAX_REOLINK_SNAPSHOT_TIMEOUT_SECONDS = 3.0
DEFAULT_REOLINK_SNAPSHOT_MAX_BYTES = 4 * 1024 * 1024
MAX_REOLINK_LOGIN_BYTES = 64 * 1024
SAMPLER_POLL_SECONDS = 0.1
PRIVATE_CAMERA_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "fc00::/7",
))


LOGGER = logging.getLogger(__name__)


class SnapshotFailure(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class SnapshotResponse:
    data: bytes
    content_type: str


@dataclass(frozen=True)
class ReolinkSnapshotConfig:
    base_url: str | None
    username: str | None
    password: str | None = field(repr=False)
    allow_self_signed: bool = False
    candidate_count: int = DEFAULT_REOLINK_SNAPSHOT_COUNT
    timeout_seconds: float = DEFAULT_REOLINK_SNAPSHOT_TIMEOUT_SECONDS
    max_response_bytes: int = DEFAULT_REOLINK_SNAPSHOT_MAX_BYTES
    output_directory: Path = Path(".reolink-snapshots")
    disabled_reason: str | None = None

    @property
    def enabled(self) -> bool:
        return self.disabled_reason is None


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_reolink_snapshot_config(environment, upload_directory: Path, *,
                                  max_candidate_bytes: int) -> ReolinkSnapshotConfig:
    base_url = str(environment.get("GATE_REOLINK_SNAPSHOT_BASE_URL", "")).strip()
    username = str(environment.get("GATE_REOLINK_SNAPSHOT_USERNAME", "")).strip()
    password_value = environment.get("GATE_REOLINK_SNAPSHOT_PASSWORD", "")
    password = str(password_value) if password_value is not None else ""
    output_directory = Path(upload_directory) / ".reolink-snapshots"
    configured = (bool(base_url), bool(username), bool(password.strip()))
    if not any(configured):
        return ReolinkSnapshotConfig(
            None, None, None, output_directory=output_directory,
            disabled_reason="unconfigured",
        )
    if not all(configured):
        raise ValueError(
            "GATE_REOLINK_SNAPSHOT_BASE_URL, GATE_REOLINK_SNAPSHOT_USERNAME, and "
            "GATE_REOLINK_SNAPSHOT_PASSWORD must be configured together"
        )
    base_url = _private_https_origin(base_url)
    allow_self_signed = _strict_boolean(
        environment.get("GATE_REOLINK_SNAPSHOT_ALLOW_SELF_SIGNED", "false")
    )
    candidate_count = _bounded_integer(
        environment.get("GATE_REOLINK_SNAPSHOT_COUNT", DEFAULT_REOLINK_SNAPSHOT_COUNT),
        "count", MAX_REOLINK_SNAPSHOT_COUNT,
    )
    timeout_seconds = _bounded_float(
        environment.get(
            "GATE_REOLINK_SNAPSHOT_TIMEOUT_SECONDS",
            DEFAULT_REOLINK_SNAPSHOT_TIMEOUT_SECONDS,
        ),
        "timeout", MAX_REOLINK_SNAPSHOT_TIMEOUT_SECONDS,
    )
    default_max_bytes = min(DEFAULT_REOLINK_SNAPSHOT_MAX_BYTES, max_candidate_bytes)
    max_response_bytes = _bounded_integer(
        environment.get("GATE_REOLINK_SNAPSHOT_MAX_BYTES", default_max_bytes),
        "response byte limit", max_candidate_bytes,
    )
    return ReolinkSnapshotConfig(
        base_url=base_url,
        username=username,
        password=password,
        allow_self_signed=allow_self_signed,
        candidate_count=candidate_count,
        timeout_seconds=timeout_seconds,
        max_response_bytes=max_response_bytes,
        output_directory=output_directory,
    )


def _private_https_origin(value: str) -> str:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError(
            "Reolink snapshot base URL must be a private literal HTTPS origin"
        ) from error
    invalid = (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or bool(parsed.params or parsed.query or parsed.fragment)
        or any(ord(character) < 32 for character in value)
        or value != value.strip()
        or (port is not None and not 1 <= port <= 65535)
    )
    try:
        address = ipaddress.ip_address(parsed.hostname) if parsed.hostname else None
    except ValueError:
        address = None
    if invalid or address is None or not _is_private_camera_address(address):
        raise ValueError(
            "Reolink snapshot base URL must be a private literal HTTPS origin"
        )
    return value.rstrip("/")


def _is_private_camera_address(address) -> bool:
    return address.is_loopback or any(
        address.version == network.version and address in network
        for network in PRIVATE_CAMERA_NETWORKS
    )


def _strict_boolean(value) -> bool:
    normalised = str(value).strip().lower()
    if normalised == "true":
        return True
    if normalised == "false":
        return False
    raise ValueError("Reolink snapshot self-signed TLS opt-in must be true or false")


def _bounded_integer(value, label: str, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Reolink snapshot {label} must be an integer") from error
    if not 1 <= parsed <= maximum:
        raise ValueError(f"Reolink snapshot {label} must be between 1 and {maximum}")
    return parsed


def _bounded_float(value, label: str, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Reolink snapshot {label} must be a number") from error
    if not math.isfinite(parsed) or not 0 < parsed <= maximum:
        raise ValueError(
            f"Reolink snapshot {label} must be greater than zero and at most {maximum}"
        )
    return parsed


class ReolinkSnapshotClient:
    def __init__(self, config: ReolinkSnapshotConfig, *, opener=None, clock=monotonic):
        self._config = config
        self._opener = opener or _camera_opener(config.allow_self_signed)
        self._clock = clock

    def login(self, timeout: float) -> str:
        payload = [{
            "cmd": "Login",
            "action": 0,
            "param": {"User": {
                "userName": self._config.username,
                "password": self._config.password,
            }},
        }]
        response = self._json_request(
            "Login", payload, timeout, failure_reason="login_http"
        )
        try:
            result = response[0]
            token = result["value"]["Token"]["name"]
            if result.get("code") != 0 or not isinstance(token, str) or not token:
                raise KeyError("invalid token")
        except (IndexError, KeyError, TypeError) as error:
            raise SnapshotFailure("login_invalid") from error
        return token

    def snapshot(self, token: str, sequence: int, timeout: float) -> SnapshotResponse:
        deadline = self._clock() + timeout
        request = Request(self._api_url(
            "Snap", channel="0", rs=f"{sequence}-{secrets.token_hex(4)}", token=token
        ), method="GET", headers={"Accept": "image/jpeg"})
        try:
            with self._opener.open(request, timeout=timeout) as response:
                if getattr(response, "status", 200) != 200:
                    raise SnapshotFailure("snapshot_http")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                data = _read_bounded(
                    response, self._config.max_response_bytes, deadline, self._clock
                )
        except HTTPError as error:
            error.close()
            raise SnapshotFailure(
                "redirect_rejected" if 300 <= error.code < 400 else "snapshot_http"
            ) from None
        except (TimeoutError, URLError, OSError) as error:
            raise SnapshotFailure(_transport_failure_reason(error, "snapshot_http")) from None
        if len(data) > self._config.max_response_bytes:
            raise SnapshotFailure("output_limit")
        return SnapshotResponse(data, content_type)

    def logout(self, token: str, timeout: float) -> None:
        self._json_request(
            "Logout", [{"cmd": "Logout", "action": 0, "param": {}}], timeout,
            failure_reason="logout_http", token=token,
        )

    def _json_request(self, command: str, payload, timeout: float, *,
                      failure_reason: str, token: str | None = None):
        deadline = self._clock() + timeout
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(
            self._api_url(command, **({"token": token} if token else {})),
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                if getattr(response, "status", 200) != 200:
                    raise SnapshotFailure(failure_reason)
                body = _read_bounded(
                    response, MAX_REOLINK_LOGIN_BYTES, deadline, self._clock
                )
        except HTTPError as error:
            error.close()
            raise SnapshotFailure(
                "redirect_rejected" if 300 <= error.code < 400 else failure_reason
            ) from None
        except (TimeoutError, URLError, OSError) as error:
            raise SnapshotFailure(_transport_failure_reason(error, failure_reason)) from None
        if len(body) > MAX_REOLINK_LOGIN_BYTES:
            raise SnapshotFailure(failure_reason)
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SnapshotFailure(failure_reason) from error

    def _api_url(self, command: str, **parameters) -> str:
        query = urlencode({"cmd": command, **parameters})
        return f"{self._config.base_url}/cgi-bin/api.cgi?{query}"


def _camera_opener(allow_self_signed: bool):
    context = ssl.create_default_context()
    if allow_self_signed:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return build_opener(RejectRedirects(), HTTPSHandler(context=context))


def _read_bounded(response, max_bytes: int, deadline: float, clock) -> bytes:
    read_chunk = getattr(response, "read1", None)
    if not callable(read_chunk):
        data = response.read(max_bytes + 1)
        if clock() >= deadline:
            raise SnapshotFailure("timeout")
        return data

    chunks = []
    total = 0
    while total <= max_bytes:
        if clock() >= deadline:
            raise SnapshotFailure("timeout")
        chunk = read_chunk(min(64 * 1024, max_bytes + 1 - total))
        if clock() >= deadline:
            raise SnapshotFailure("timeout")
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _transport_failure_reason(error: BaseException, fallback: str) -> str:
    reason = getattr(error, "reason", None)
    if isinstance(error, TimeoutError) or isinstance(reason, TimeoutError):
        return "timeout"
    if getattr(error, "errno", None) == errno.ETIMEDOUT:
        return "timeout"
    if getattr(reason, "errno", None) == errno.ETIMEDOUT:
        return "timeout"
    return fallback


class ReolinkSnapshotSampler:
    """Serially augment one FTP burst without participating in authorization."""

    def __init__(self, config: ReolinkSnapshotConfig, add_candidate, *,
                 client_factory=ReolinkSnapshotClient, clock=monotonic,
                 run_id=lambda: secrets.token_hex(8)):
        self._config = config
        self._add_candidate = add_candidate
        self._client_factory = client_factory
        self._clock = clock
        self._run_id = run_id
        self._requests = Queue(maxsize=1)
        self._lock = Lock()
        self._active = False
        self._started_at: float | None = None
        self._deadline: float | None = None
        self._closed = False
        self._cleanup_all()

    @property
    def output_directory(self) -> Path:
        return self._config.output_directory

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    def request(self, received_at=None) -> bool:
        if not self._config.enabled:
            self._log("disabled", 0, self._config.disabled_reason or "unconfigured", 0)
            return False
        with self._lock:
            if self._closed or self._active:
                reason = "shutdown" if self._closed else "busy"
            else:
                started_at = self._clock()
                self._active = True
                self._started_at = started_at
                self._deadline = started_at + self._config.timeout_seconds
                self._requests.put_nowait(received_at)
                return True
        self._log("skipped", 0, reason, 0)
        return False

    def run_forever(self, stop_event) -> None:
        while not stop_event.is_set():
            try:
                received_at = self._requests.get(timeout=SAMPLER_POLL_SECONDS)
            except Empty:
                continue
            self._capture(stop_event, received_at)

    def run_once(self, stop_event) -> bool:
        try:
            received_at = self._requests.get_nowait()
        except Empty:
            return False
        self._capture(stop_event, received_at)
        return True

    def _capture(self, stop_event, received_at) -> None:
        with self._lock:
            started_at = self._started_at if self._started_at is not None else self._clock()
            deadline = self._deadline if self._deadline is not None else started_at
        accepted: list[Path] = []
        published: list[Path] = []
        token = None
        reason = None
        client = None
        prefix = None
        directory_fd = None
        try:
            prefix = self._run_id()
            directory_fd = self._open_output_directory(create=True)
            client = self._client_factory(self._config)
            token = client.login(self._remaining(deadline, stop_event))
            for sequence in range(1, self._config.candidate_count + 1):
                response = client.snapshot(
                    token, sequence, self._remaining(deadline, stop_event)
                )
                self._remaining(deadline, stop_event)
                path = self._store_candidate(directory_fd, prefix, sequence, response)
                accepted.append(path)
        except SnapshotFailure as error:
            reason = error.reason
        except (OSError, ValueError):
            reason = "io_error"
        except Exception:
            reason = "internal_error"
        finally:
            if reason != "shutdown":
                for path in accepted:
                    try:
                        self._add_candidate(path, received_at)
                    except Exception:
                        reason = reason or "collector_error"
                        break
                    published.append(path)
            if token is not None and client is not None and not stop_event.is_set():
                try:
                    remaining = deadline - self._clock()
                    if remaining > 0:
                        client.logout(token, remaining)
                except Exception:
                    pass
            if prefix is not None and directory_fd is not None:
                self._cleanup_prefix(
                    prefix, keep=set(published), directory_fd=directory_fd,
                )
            if directory_fd is not None:
                os.close(directory_fd)
            with self._lock:
                self._active = False
                self._started_at = None
                self._deadline = None
            duration_ms = max(0, round((self._clock() - started_at) * 1000))
            if reason is None and len(published) != self._config.candidate_count:
                reason = "incomplete"
            self._log(
                "completed" if reason is None else "failed",
                len(published), reason or "none", duration_ms,
            )

    def _remaining(self, deadline: float, stop_event) -> float:
        if stop_event.is_set():
            raise SnapshotFailure("shutdown")
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise SnapshotFailure("timeout")
        return remaining

    def _store_candidate(self, directory_fd: int, prefix: str, sequence: int,
                         response: SnapshotResponse) -> Path:
        if response.content_type.lower() != "image/jpeg":
            raise SnapshotFailure("invalid_content")
        if len(response.data) > self._config.max_response_bytes:
            raise SnapshotFailure("output_limit")
        if not is_decodable_jpeg(response.data):
            raise SnapshotFailure("invalid_jpeg")
        final_name = f"{prefix}-{sequence:02d}.jpg"
        temporary_name = f"{prefix}-{sequence:02d}.part"
        final_path = self._config.output_directory / final_name
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            file_descriptor = os.open(
                temporary_name, flags, 0o600, dir_fd=directory_fd,
            )
            with os.fdopen(file_descriptor, "wb") as destination:
                destination.write(response.data)
            os.replace(
                temporary_name, final_name,
                src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
            )
        except Exception:
            for name in (temporary_name, final_name):
                try:
                    os.unlink(name, dir_fd=directory_fd)
                except OSError:
                    pass
            raise
        return final_path

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._active = False
        self._cleanup_all()

    def _open_output_directory(self, *, create: bool) -> int:
        output_directory = self._config.output_directory
        if create:
            output_directory.parent.mkdir(parents=True, exist_ok=True)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        parent_fd = os.open(output_directory.parent, flags)
        try:
            if create:
                try:
                    os.mkdir(output_directory.name, mode=0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            directory_fd = os.open(output_directory.name, flags, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        if create:
            os.fchmod(directory_fd, 0o700)
        return directory_fd

    def _cleanup_prefix(self, prefix: str, *, keep: set[Path],
                        directory_fd: int | None = None) -> None:
        close_directory = directory_fd is None
        try:
            if directory_fd is None:
                directory_fd = self._open_output_directory(create=False)
            keep_names = {path.name for path in keep}
            for name in os.listdir(directory_fd):
                if name.startswith(f"{prefix}-") and name not in keep_names:
                    self._unlink_file_entry(directory_fd, name)
        except FileNotFoundError:
            return
        except OSError:
            LOGGER.warning(
                "gate_camera source=camera_ftp subtype=unverified "
                "augmentation=reolink_snapshot outcome=cleanup_failed reason=io_error"
            )
        finally:
            if close_directory and directory_fd is not None:
                os.close(directory_fd)

    def _cleanup_all(self) -> None:
        directory_fd = None
        try:
            directory_fd = self._open_output_directory(create=False)
            for name in os.listdir(directory_fd):
                self._unlink_file_entry(directory_fd, name)
        except FileNotFoundError:
            return
        except OSError:
            LOGGER.warning(
                "gate_camera source=camera_ftp subtype=unverified "
                "augmentation=reolink_snapshot outcome=cleanup_failed reason=io_error"
            )
        finally:
            if directory_fd is not None:
                os.close(directory_fd)

    @staticmethod
    def _unlink_file_entry(directory_fd: int, name: str) -> None:
        entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode):
            os.unlink(name, dir_fd=directory_fd)

    @staticmethod
    def _log(outcome: str, count: int, reason: str, duration_ms: int) -> None:
        message = (
            "gate_camera source=camera_ftp subtype=unverified "
            "augmentation=reolink_snapshot outcome=%s candidate_count=%d "
            "reason=%s duration_ms=%d"
        )
        log = LOGGER.warning if outcome == "failed" else LOGGER.info
        log(message, outcome, count, reason, duration_ms)
