import json
import socket
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock

from .models import GateEvent


MAX_REQUEST_BYTES = 4096
MAX_COMMAND_LIFETIME = timedelta(seconds=10)
COMMAND_SERVER_HOST = "127.0.0.1"
COMMAND_SERVER_PORT = 8765


class DirectCommandExecutor:
    def __init__(self, controller_id, coordinator, store, *, prompt_player=None, clock=None,
                 max_command_lifetime=MAX_COMMAND_LIFETIME):
        if not isinstance(controller_id, str) or not controller_id:
            raise ValueError("controller_id is required")
        if max_command_lifetime <= timedelta(0):
            raise ValueError("max_command_lifetime must be positive")
        self._controller_id = controller_id
        self._coordinator = coordinator
        self._store = store
        self._prompt_player = prompt_player
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_command_lifetime = max_command_lifetime
        self._prompt_lock = Lock()

    @property
    def coordinator(self):
        return self._coordinator

    def execute(self, payload, *, now=None) -> dict:
        command = self._parse(payload)
        if command is None:
            return {"status": "failed", "detail": "invalid_command"}
        if command["controller_id"] != self._controller_id:
            return {"status": "failed", "detail": "wrong_controller"}
        if command["command"] == "play_prompt":
            return self._play_prompt(command)

        received_at = now or self._clock()

        def expiry_inhibition():
            return self._expiry_inhibition(command["expires_at"], now=now)

        execution = self._coordinator.actuate(GateEvent(
            source="remote_command", reason="remote_command", opened=False,
            idempotency_key=f"command:{command['idempotency_key']}",
            received_at=received_at, decision_at=received_at,
        ), pre_activation_inhibit=expiry_inhibition)
        response = {"status": execution.terminal_status}
        if execution.terminal_detail is not None:
            response["detail"] = execution.terminal_detail
        return response

    def _parse(self, payload):
        if not isinstance(payload, dict):
            return None
        controller_id = payload.get("controller_id")
        command = payload.get("command")
        idempotency_key = payload.get("idempotency_key")
        prompt_key = payload.get("prompt_key")
        if (not isinstance(controller_id, str) or not controller_id
                or command not in {"open_gate", "play_prompt"}
                or not isinstance(idempotency_key, str) or not idempotency_key
                or (prompt_key is not None and not isinstance(prompt_key, str))):
            return None
        try:
            expires_at = _parse_timestamp(payload.get("expires_at"))
        except ValueError:
            return None
        return {
            "controller_id": controller_id,
            "command": command,
            "idempotency_key": idempotency_key,
            "expires_at": expires_at,
            "prompt_key": prompt_key,
        }

    def _expiry_inhibition(self, expires_at, *, now=None):
        try:
            remaining = expires_at - (now or self._clock())
        except (TypeError, ValueError):
            return "expired", "invalid_expiry_window"
        if remaining <= timedelta(0):
            return "expired", "expired_before_activation"
        if remaining > self._max_command_lifetime:
            return "expired", "invalid_expiry_window"
        return None

    def _play_prompt(self, command):
        idempotency_key = f"command:{command['idempotency_key']}"
        with self._prompt_lock:
            terminal = self._store.terminal_outcome(idempotency_key)
            if terminal is not None:
                response = {"status": terminal.status}
                if terminal.detail is not None:
                    response["detail"] = terminal.detail
                return response
            inhibition = self._expiry_inhibition(command["expires_at"])
            if inhibition is not None:
                status, detail = inhibition
            elif self._prompt_player is None or not self._prompt_player.play(command["prompt_key"]):
                status, detail = "failed", "invalid_prompt"
            else:
                status, detail = "completed", None
            self._store.record_terminal_outcome(GateEvent(
                source="remote_command", reason="remote_command" if status == "completed" else detail,
                opened=False, idempotency_key=idempotency_key,
                received_at=self._clock(), decision_at=self._clock(),
            ), status=status, detail=detail)
            response = {"status": status}
            if detail is not None:
                response["detail"] = detail
            return response


class CommandRequestHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/commands":
            self._respond(404, {"status": "failed", "detail": "not_found"})
            return
        content_length = self.headers.get("Content-Length")
        try:
            length = int(content_length)
        except (TypeError, ValueError):
            self._respond(400, {"status": "failed", "detail": "invalid_request"})
            return
        if length < 0 or length > MAX_REQUEST_BYTES:
            self._respond(413, {"status": "failed", "detail": "request_too_large"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            self._respond(400, {"status": "failed", "detail": "invalid_request"})
            return
        try:
            response = self.server.executor.execute(payload)
        except Exception:
            self._respond(500, {"status": "failed", "detail": "command_execution_error"})
            return
        self._respond(200, response)

    def do_GET(self):
        self._respond(404, {"status": "failed", "detail": "not_found"})

    def log_message(self, format, *args):
        return

    def _respond(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def build_command_server(host, port, executor) -> ThreadingHTTPServer:
    if not _is_loopback(host):
        raise ValueError("command server must bind to a loopback address")
    server_class = ThreadingHTTPServer
    if host == "::1":
        server_class = type("IPv6CommandServer", (ThreadingHTTPServer,), {
            "address_family": socket.AF_INET6,
        })
    server = server_class((host, port), CommandRequestHandler)
    server.executor = executor
    return server


def run_command_server(host, port, executor, stop_event):
    server = build_command_server(host, port, executor)
    server.timeout = 0.2
    try:
        while not stop_event.is_set():
            server.handle_request()
    finally:
        server.server_close()


class CommandServerWorker:
    def __init__(self, executor, *, host=COMMAND_SERVER_HOST, port=COMMAND_SERVER_PORT,
                 server_runner=run_command_server):
        self.executor = executor
        self._host = host
        self._port = port
        self._server_runner = server_runner

    def run_forever(self, stop_event):
        self._server_runner(self._host, self._port, self.executor, stop_event)


def _is_loopback(host) -> bool:
    return host in {"127.0.0.1", "::1"}


def _parse_timestamp(value):
    if not isinstance(value, str):
        raise ValueError("expires_at is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("expires_at is invalid") from error
    if parsed.tzinfo is None:
        raise ValueError("expires_at must include a timezone")
    return parsed.astimezone(timezone.utc)
