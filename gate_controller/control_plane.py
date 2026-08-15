from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from threading import Event
from time import monotonic

import requests

from .actuation import ActuationCoordinator
from .models import GateEvent
from .runtime import require_https_service_url


class ControlPlaneError(RuntimeError):
    pass


@dataclass(frozen=True)
class GateCommand:
    id: str
    command: str
    expires_at: datetime
    prompt_key: str | None = None
    server_time: datetime | None = None
    server_monotonic: float | None = None


class SupabaseControlPlane:
    def __init__(self, url: str, service_key: str, controller_id: str, *,
                 session=None, timeout: tuple[float, float] = (2, 4), clock=None,
                 monotonic_clock=None):
        self._url = require_https_service_url(url, "SUPABASE_URL")
        self._controller_id = controller_id
        self._session = session or requests.Session()
        self._timeout = timeout
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic_clock = monotonic_clock or monotonic
        self._headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        }

    def claim_command(self) -> GateCommand | None:
        payload, server_time = self._rpc(
            "claim_gate_command", {"p_controller_id": self._controller_id},
            include_server_time=True,
        )
        command_payload = payload[0] if isinstance(payload, list) and payload else payload
        if not command_payload:
            return None
        return _parse_command(
            command_payload, server_time=server_time,
            server_monotonic=self._monotonic_clock(),
        )

    def complete_command(self, command: GateCommand, status: str, detail: str | None = None) -> None:
        if status not in {"completed", "failed", "expired"}:
            raise ValueError("invalid command completion status")
        payload = {
            "p_command_id": command.id,
            "p_status": status,
            "p_controller_id": self._controller_id,
        }
        if detail:
            payload["p_detail"] = detail
        response = self._rpc("complete_gate_command", payload)
        row = response[0] if isinstance(response, list) and response else response
        if not isinstance(row, dict) or row.get("controller_reported_status") != status:
            raise ControlPlaneError(
                "complete_gate_command did not confirm the controller-reported status"
            )

    def heartbeat(self, status: dict) -> None:
        self._rpc(
            "update_controller_status",
            {
                "p_controller_id": self._controller_id,
                "p_camera_timestamp": status.get("last_camera_upload_at"),
                "p_queue_depth": status.get("queue_depth", 0),
                "p_capabilities": {
                    "audio_available": bool(status.get("audio_available")),
                    "audio_prompts": bool(status.get("audio_available")),
                    "camera": {
                        "configured": bool(status.get("camera_configured")),
                        "upload_ready": bool(status.get("camera_upload_ready")),
                        "last_upload_at": status.get("last_camera_upload_at"),
                        "upload_recent": bool(status.get("camera_upload_recent")),
                        "connection_probed": bool(status.get("camera_connection_probed")),
                        "connected": status.get("camera_connected"),
                    },
                    "relay": status.get("relay", {}),
                    "authorisation": status.get("authorisation", {}),
                },
            },
        )

    def _rpc(self, name: str, payload: dict, *, include_server_time: bool = False):
        response = self._session.post(
            f"{self._url}/rest/v1/rpc/{name}", headers=self._headers,
            json=payload, timeout=self._timeout,
        )
        if not 200 <= response.status_code < 300:
            raise ControlPlaneError(f"Supabase RPC {name} returned HTTP {response.status_code}")
        try:
            body = response.json()
        except (TypeError, ValueError) as error:
            raise ControlPlaneError(f"Supabase RPC {name} returned invalid JSON") from error
        if not include_server_time:
            return body
        return body, _response_time(response)


class CommandWorker:
    def __init__(self, control_plane, relay, store, *, prompt_player=None, clock=None,
                 poll_interval: float = 1.0, coordinator=None,
                 max_command_lifetime: timedelta = timedelta(minutes=2),
                 monotonic_clock=None):
        if max_command_lifetime <= timedelta(0):
            raise ValueError("max_command_lifetime must be positive")
        self._control_plane = control_plane
        self._relay = relay
        self._store = store
        self._prompt_player = prompt_player
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic_clock = monotonic_clock or monotonic
        self._poll_interval = poll_interval
        self._max_command_lifetime = max_command_lifetime
        self._coordinator = coordinator or ActuationCoordinator(store, relay, clock=self._clock)

    def run_once(self) -> bool:
        try:
            pending_ack = self._store.pending_command_acks(limit=1)
        except Exception:
            return False
        if pending_ack:
            return self._replay_ack(pending_ack[0])
        try:
            command = self._control_plane.claim_command()
        except Exception:
            return False
        if command is None:
            return False
        expiry_detail = self._expiry_detail(command, "expired_before_execution")
        if expiry_detail is not None:
            return self._acknowledge(command, "expired", expiry_detail)
        if command.command == "open_gate":
            return self._open_gate(command)
        if command.command == "play_prompt":
            return self._play_prompt(command)
        return self._acknowledge(command, "failed", "unsupported_command")

    def run_forever(self, stop_event: Event) -> None:
        while not stop_event.is_set():
            self.run_once()
            stop_event.wait(self._poll_interval)

    def _open_gate(self, command: GateCommand) -> bool:
        idempotency_key = f"command:{command.id}"
        now = self._clock()

        def expiry_inhibition():
            expiry_detail = self._expiry_detail(command, "expired_before_activation")
            if expiry_detail is not None:
                return "expired", expiry_detail
            return None

        execution = self._coordinator.actuate(GateEvent(
            source="remote_command", reason="remote_command", opened=False,
            idempotency_key=idempotency_key, received_at=now, decision_at=now,
        ), command_ack=(command.id, now), pre_activation_inhibit=expiry_inhibition)
        try:
            pending = self._store.pending_command_acks(limit=1)
        except Exception:
            return False
        if pending and pending[0][0] == command.id:
            return self._replay_ack(pending[0])
        return self._acknowledge(command, execution.terminal_status, execution.terminal_detail)

    def _play_prompt(self, command: GateCommand) -> bool:
        if self._prompt_player is None or not self._prompt_player.play(command.prompt_key):
            return self._acknowledge(command, "failed", "invalid_prompt")
        return self._acknowledge(command, "completed")

    def _expiry_detail(self, command: GateCommand, expired_detail: str) -> str | None:
        try:
            current_time = self._clock()
            if command.server_time is not None and command.server_monotonic is not None:
                elapsed = self._monotonic_clock() - command.server_monotonic
                if elapsed < 0:
                    return "invalid_expiry_window"
                current_time = command.server_time + timedelta(seconds=elapsed)
            remaining = command.expires_at - current_time
        except (TypeError, ValueError):
            return "invalid_expiry_window"
        if remaining <= timedelta(0):
            return expired_detail
        if remaining > self._max_command_lifetime:
            return "invalid_expiry_window"
        return None

    def _acknowledge(self, command: GateCommand, status: str, detail: str | None = None) -> bool:
        try:
            self._store.queue_command_ack(command.id, status, detail, self._clock())
            self._control_plane.complete_command(command, status, detail)
        except Exception:
            return False
        try:
            self._store.complete_command_ack(command.id)
        except Exception:
            return False
        return True

    def _replay_ack(self, pending) -> bool:
        command_id, status, detail = pending
        command = GateCommand(command_id, "open_gate", self._clock())
        try:
            self._control_plane.complete_command(command, status, detail)
            self._store.complete_command_ack(command_id)
        except Exception:
            return False
        return True


class HeartbeatWorker:
    def __init__(self, control_plane, status, poll_interval: float = 15.0):
        self._control_plane = control_plane
        self._status = status
        self._poll_interval = poll_interval

    def run_once(self) -> bool:
        try:
            self._control_plane.heartbeat(self._status())
        except Exception:
            return False
        return True

    def run_forever(self, stop_event: Event) -> None:
        while not stop_event.is_set():
            self.run_once()
            stop_event.wait(self._poll_interval)


def _parse_command(payload: dict, *, server_time: datetime | None = None,
                   server_monotonic: float | None = None) -> GateCommand:
    if not isinstance(payload, dict):
        raise ControlPlaneError("Supabase returned an invalid command")
    identifier = payload.get("id")
    command = payload.get("command")
    expires_at = _parse_timestamp(payload.get("expires_at"))
    prompt_key = payload.get("prompt_key")
    if (not isinstance(identifier, str) or not identifier
            or command not in {"open_gate", "play_prompt"}
            or (prompt_key is not None and not isinstance(prompt_key, str))):
        raise ControlPlaneError("Supabase returned an invalid command")
    return GateCommand(
        identifier, command, expires_at, prompt_key, server_time, server_monotonic
    )


def _response_time(response) -> datetime:
    value = getattr(response, "headers", {}).get("Date")
    if not isinstance(value, str):
        raise ControlPlaneError("Supabase command response has no trusted server time")
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError) as error:
        raise ControlPlaneError("Supabase command response has invalid server time") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_timestamp(value) -> datetime:
    if not isinstance(value, str):
        raise ControlPlaneError("Supabase command has no expiry")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ControlPlaneError("Supabase command has an invalid expiry") from error
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
