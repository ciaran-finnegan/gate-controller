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
from threading import Event, Lock, Thread
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .images import wait_until_readable


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
    username: str | None = field(repr=False)
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


class _SnapshotSpool:
    """Own a private directory without ever traversing a replacement symlink."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        parent_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        parent_fd = os.open(self.path.parent, parent_flags)
        try:
            try:
                entry = os.stat(
                    self.path.name, dir_fd=parent_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                entry = None
            if entry is not None and stat.S_ISLNK(entry.st_mode):
                raise OSError(errno.ELOOP, "snapshot spool must not be a symlink")
            if entry is not None and not stat.S_ISDIR(entry.st_mode):
                raise OSError(errno.ENOTDIR, "snapshot spool is not a directory")
            if entry is None:
                os.mkdir(self.path.name, mode=0o700, dir_fd=parent_fd)
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_flags |= (
                getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            self._directory_fd = os.open(
                self.path.name, directory_flags, dir_fd=parent_fd
            )
        finally:
            os.close(parent_fd)
        if not stat.S_ISDIR(os.fstat(self._directory_fd).st_mode):
            os.close(self._directory_fd)
            raise OSError(errno.ENOTDIR, "snapshot spool is not a directory")
        os.fchmod(self._directory_fd, 0o700)
        self._closed = False

    def store(self, filename: str, data: bytes):
        temporary = f"{filename}.part"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600, dir_fd=self._directory_fd)
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError(errno.EIO, "snapshot spool write made no progress")
                view = view[written:]
            os.fchmod(descriptor, 0o600)
        except BaseException:
            self._unlink(temporary)
            raise
        finally:
            os.close(descriptor)
        try:
            os.replace(
                temporary, filename,
                src_dir_fd=self._directory_fd, dst_dir_fd=self._directory_fd,
            )
        except BaseException:
            self._unlink(temporary)
            raise
        return _AnchoredSnapshot(
            self._directory_fd, filename, self.path / filename
        )

    def cleanup_prefix(self, prefix: str, *, keep: set[Path]) -> None:
        keep_names = {path.name for path in keep}
        for name in os.listdir(self._directory_fd):
            if name.startswith(f"{prefix}-") and name not in keep_names:
                self._unlink_non_directory(name)

    def cleanup_all(self) -> None:
        for name in os.listdir(self._directory_fd):
            self._unlink_non_directory(name)

    def close(self) -> None:
        if self._closed:
            return
        os.close(self._directory_fd)
        self._closed = True

    def _unlink_non_directory(self, name: str) -> None:
        try:
            entry = os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode):
            self._unlink(name)

    def _unlink(self, name: str) -> None:
        try:
            os.unlink(name, dir_fd=self._directory_fd)
        except FileNotFoundError:
            pass


class _AnchoredSnapshot:
    """A snapshot whose reads and deletion stay pinned to its spool directory."""

    _descriptor_anchored = True

    def __init__(self, directory_fd: int, filename: str, display_path: Path):
        self._directory_fd = os.dup(directory_fd)
        self._filename = filename
        self._display_path = Path(display_path)
        self._lock = Lock()
        self._closed = False

    @property
    def name(self) -> str:
        return self._filename

    @property
    def suffix(self) -> str:
        return Path(self._filename).suffix

    def open(self, mode: str = "rb"):
        if mode not in {"rb", "br"}:
            raise ValueError("anchored snapshots are read-only")
        with self._lock:
            if self._closed:
                raise OSError(errno.EBADF, "snapshot candidate is closed")
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(
                self._filename, flags, dir_fd=self._directory_fd
            )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise OSError(errno.EINVAL, "snapshot candidate is not a file")
        return os.fdopen(descriptor, "rb")

    def stat(self):
        with self._lock:
            if self._closed:
                raise OSError(errno.EBADF, "snapshot candidate is closed")
            result = os.stat(
                self._filename,
                dir_fd=self._directory_fd,
                follow_symlinks=False,
            )
        if not stat.S_ISREG(result.st_mode):
            raise OSError(errno.EINVAL, "snapshot candidate is not a file")
        return result

    def unlink(self, missing_ok: bool = False) -> None:
        try:
            with self._lock:
                if self._closed:
                    if missing_ok:
                        return
                    raise OSError(errno.EBADF, "snapshot candidate is closed")
                try:
                    entry = os.stat(
                        self._filename,
                        dir_fd=self._directory_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    if not missing_ok:
                        raise
                else:
                    if stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode):
                        os.unlink(self._filename, dir_fd=self._directory_fd)
        finally:
            self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            os.close(self._directory_fd)
            self._closed = True

    def __str__(self) -> str:
        return str(self._display_path)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class ReolinkSnapshotSampler:
    """Serially augment one FTP burst without participating in authorization."""

    def __init__(self, config: ReolinkSnapshotConfig, complete, *,
                 client_factory=ReolinkSnapshotClient, clock=monotonic,
                 run_id=lambda: secrets.token_hex(8)):
        self._config = config
        self._complete = complete
        self._client_factory = client_factory
        self._clock = clock
        self._run_id = run_id
        self._requests = Queue(maxsize=1)
        self._lock = Lock()
        self._active = False
        self._started_at: float | None = None
        self._deadline: float | None = None
        self._wall_deadline: float | None = None
        self._operation_thread: Thread | None = None
        self._operation_done: Event | None = None
        self._operation_cancel: Event | None = None
        self._cancel_reason: str | None = None
        self._completion_reported = False
        self._completion_delivered = False
        self._closed = False
        self._resources_closed = False
        self._spool = None
        if config.enabled:
            try:
                self._spool = _SnapshotSpool(config.output_directory)
            except OSError:
                pass
        if self._spool is not None:
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
                self._wall_deadline = monotonic() + self._config.timeout_seconds
                self._completion_delivered = False
                self._requests.put_nowait(received_at)
                return True
        self._log("skipped", 0, reason, 0)
        return False

    def run_forever(self, stop_event) -> None:
        while True:
            if stop_event.is_set():
                self._complete_queued_shutdown()
                return
            try:
                received_at = self._requests.get(timeout=SAMPLER_POLL_SECONDS)
            except Empty:
                continue
            if stop_event.is_set():
                self._complete_without_capture(received_at, "shutdown")
                return
            self._capture(stop_event, received_at)

    def run_once(self, stop_event) -> bool:
        try:
            received_at = self._requests.get_nowait()
        except Empty:
            return False
        if stop_event.is_set():
            self._complete_without_capture(received_at, "shutdown")
            return True
        self._capture(stop_event, received_at)
        return True

    def _complete_queued_shutdown(self) -> bool:
        try:
            received_at = self._requests.get_nowait()
        except Empty:
            return False
        self._complete_without_capture(received_at, "shutdown")
        return True

    def _complete_without_capture(self, received_at, reason: str) -> None:
        started_at = self._clock()
        with self._lock:
            if self._started_at is not None:
                started_at = self._started_at
        try:
            self._deliver_completion((), received_at)
        except Exception:
            reason = "collector_error"
        finally:
            with self._lock:
                self._active = False
                self._started_at = None
                self._deadline = None
                self._wall_deadline = None
                self._operation_thread = None
                self._operation_done = None
                self._operation_cancel = None
                self._cancel_reason = None
        duration_ms = max(0, round((self._clock() - started_at) * 1000))
        self._log("failed", 0, reason, duration_ms)

    def _capture(self, stop_event, received_at) -> None:
        with self._lock:
            started_at = self._started_at if self._started_at is not None else self._clock()
            deadline = self._deadline if self._deadline is not None else started_at
            wall_deadline = (
                self._wall_deadline
                if self._wall_deadline is not None
                else monotonic()
            )
            operation_done = Event()
            operation_cancel = Event()
            self._operation_done = operation_done
            self._operation_cancel = operation_cancel
            self._cancel_reason = None
            self._completion_reported = False
            operation_thread = Thread(
                target=self._capture_operation,
                args=(
                    stop_event, received_at, started_at, deadline, wall_deadline,
                    operation_done, operation_cancel,
                ),
                name="gate-reolink-operation",
                daemon=True,
            )
            self._operation_thread = operation_thread
        operation_thread.start()

        while True:
            if stop_event.is_set():
                self._cancel_active_operation("shutdown")
                break
            remaining = min(deadline - self._clock(), wall_deadline - monotonic())
            if remaining <= 0:
                self._cancel_active_operation("timeout")
                break
            if operation_done.wait(min(SAMPLER_POLL_SECONDS, remaining)):
                return

        try:
            self._deliver_completion((), received_at)
        except Exception:
            pass
        duration_ms = max(0, round((monotonic() - (
            wall_deadline - self._config.timeout_seconds
        )) * 1000))
        reason = "shutdown" if stop_event.is_set() else "timeout"
        self._log("failed", 0, reason, duration_ms)

    def _capture_operation(self, stop_event, received_at, started_at: float,
                           deadline: float, wall_deadline: float,
                           operation_done: Event, operation_cancel: Event) -> None:
        accepted = []
        published = []
        token = None
        reason = None
        client = None
        prefix = None
        try:
            prefix = self._run_id()
            if self._spool is None:
                raise SnapshotFailure("io_error")
            client = self._client_factory(self._config)
            token = client.login(self._remaining(
                deadline, wall_deadline, stop_event, operation_cancel
            ))
            self._remaining(deadline, wall_deadline, stop_event, operation_cancel)
            for sequence in range(1, self._config.candidate_count + 1):
                response = client.snapshot(
                    token, sequence, self._remaining(
                        deadline, wall_deadline, stop_event, operation_cancel
                    )
                )
                self._remaining(
                    deadline, wall_deadline, stop_event, operation_cancel
                )
                path = self._store_candidate(prefix, sequence, response)
                self._remaining(
                    deadline, wall_deadline, stop_event, operation_cancel
                )
                accepted.append(path)
        except SnapshotFailure as error:
            reason = error.reason
        except (OSError, ValueError):
            reason = "io_error"
        except Exception:
            reason = "internal_error"
        finally:
            if token is not None and client is not None:
                try:
                    client.logout(token, self._remaining(
                        deadline, wall_deadline, stop_event, operation_cancel
                    ))
                    self._remaining(
                        deadline, wall_deadline, stop_event, operation_cancel
                    )
                except SnapshotFailure as error:
                    if error.reason in {"timeout", "shutdown"}:
                        reason = error.reason
                except Exception:
                    pass
            completion_paths = (
                () if reason in {"shutdown", "timeout"} else tuple(accepted)
            )
            try:
                if completion_paths:
                    self._remaining(
                        deadline, wall_deadline, stop_event, operation_cancel
                    )
                delivered = self._deliver_completion(completion_paths, received_at)
            except Exception:
                delivered = False
            if delivered:
                published.extend(completion_paths)
            elif delivered is False:
                reason = reason or "collector_error"
            for candidate in accepted:
                if candidate not in published:
                    candidate.close()
            if prefix is not None:
                self._cleanup_prefix(prefix, keep=set(published))
            with self._lock:
                reported = self._completion_reported
                if self._closed:
                    self._close_resources_locked()
                self._active = False
                self._started_at = None
                self._deadline = None
                self._wall_deadline = None
                self._operation_thread = None
                self._operation_done = None
                self._operation_cancel = None
                self._cancel_reason = None
                operation_done.set()
            duration_ms = max(0, round((self._clock() - started_at) * 1000))
            if reason is None and len(published) != self._config.candidate_count:
                reason = "incomplete"
            if not reported:
                self._log(
                    "completed" if reason is None else "failed",
                    len(published), reason or "none", duration_ms,
                )

    def _remaining(self, deadline: float, wall_deadline: float, stop_event,
                   operation_cancel: Event) -> float:
        if stop_event.is_set():
            raise SnapshotFailure("shutdown")
        if operation_cancel.is_set():
            with self._lock:
                reason = self._cancel_reason or "shutdown"
            raise SnapshotFailure(reason)
        remaining = min(deadline - self._clock(), wall_deadline - monotonic())
        if remaining <= 0:
            raise SnapshotFailure("timeout")
        return remaining

    def _cancel_active_operation(self, reason: str) -> None:
        with self._lock:
            if self._cancel_reason is None:
                self._cancel_reason = reason
            self._completion_reported = True
            operation_cancel = self._operation_cancel
        if operation_cancel is not None:
            operation_cancel.set()

    def _deliver_completion(self, paths: tuple, received_at) -> bool | None:
        with self._lock:
            if self._completion_delivered:
                return None
            self._completion_delivered = True
        self._complete(paths, received_at)
        return True

    def _store_candidate(self, prefix: str, sequence: int,
                         response: SnapshotResponse):
        if response.content_type.lower() != "image/jpeg":
            raise SnapshotFailure("invalid_content")
        if len(response.data) > self._config.max_response_bytes:
            raise SnapshotFailure("output_limit")
        filename = f"{prefix}-{sequence:02d}.jpg"
        candidate = None
        if self._spool is None:
            raise SnapshotFailure("disabled")
        try:
            candidate = self._spool.store(filename, response.data)
            if not wait_until_readable(candidate, timeout=0, poll_interval=0):
                raise SnapshotFailure("invalid_jpeg")
        except Exception:
            if candidate is not None:
                candidate.close()
            self._spool.cleanup_prefix(prefix, keep=set())
            raise
        return candidate

    def close(self) -> None:
        complete_queued = False
        with self._lock:
            self._closed = True
            if self._operation_thread is not None:
                if self._cancel_reason is None:
                    self._cancel_reason = "shutdown"
                if self._operation_cancel is not None:
                    self._operation_cancel.set()
                return
            complete_queued = self._active
        if complete_queued:
            self._complete_queued_shutdown()
        with self._lock:
            self._close_resources_locked()

    def _close_resources_locked(self) -> None:
        if self._resources_closed:
            return
        try:
            self._cleanup_all()
        finally:
            if self._spool is not None:
                self._spool.close()
            self._resources_closed = True

    def _cleanup_prefix(self, prefix: str, *, keep: set[Path]) -> None:
        if self._spool is None:
            return
        try:
            self._spool.cleanup_prefix(prefix, keep=keep)
        except OSError:
            LOGGER.warning(
                "gate_camera source=camera_ftp subtype=unverified "
                "augmentation=reolink_snapshot outcome=cleanup_failed reason=io_error"
            )

    def _cleanup_all(self) -> None:
        if self._spool is None:
            return
        try:
            self._spool.cleanup_all()
        except OSError:
            LOGGER.warning(
                "gate_camera source=camera_ftp subtype=unverified "
                "augmentation=reolink_snapshot outcome=cleanup_failed reason=io_error"
            )

    @staticmethod
    def _log(outcome: str, count: int, reason: str, duration_ms: int) -> None:
        message = (
            "gate_camera source=camera_ftp subtype=unverified "
            "augmentation=reolink_snapshot outcome=%s candidate_count=%d "
            "reason=%s duration_ms=%d"
        )
        log = LOGGER.warning if outcome == "failed" else LOGGER.info
        log(message, outcome, count, reason, duration_ms)
