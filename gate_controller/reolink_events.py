"""Bounded Reolink trigger ingestion and FTP burst correlation."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import re
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from socket import SHUT_RDWR
from socketserver import ThreadingMixIn
from threading import BoundedSemaphore, Lock, Timer
from time import monotonic
from typing import Callable, Mapping

from .telemetry import TriggerTelemetry, ftp_fallback_trigger


LOGGER = logging.getLogger(__name__)

DEFAULT_REOLINK_WEBHOOK_HOST = "0.0.0.0"
DEFAULT_REOLINK_WEBHOOK_PORT = 8766
REOLINK_WEBHOOK_PATH = "/reolink/events"
MAX_REOLINK_WEBHOOK_BODY_BYTES = 4096
MAX_REOLINK_BODY_READ_BYTES = 1024
MAX_REOLINK_WEBHOOK_FIELD_LENGTH = 128
MIN_REOLINK_WEBHOOK_SECRET_LENGTH = 20
MAX_REOLINK_WEBHOOK_SECRET_LENGTH = 128
MAX_REOLINK_EVENTS = 64
MAX_REOLINK_EVENT_TTL_SECONDS = 60.0
MAX_REOLINK_CORRELATION_WINDOW_SECONDS = 15.0
REOLINK_CONNECTION_TIMEOUT_SECONDS = 1.0
REOLINK_REQUEST_DEADLINE_SECONDS = 1.0
REOLINK_BODY_DEADLINE_SECONDS = 1.0
MAX_REOLINK_CONCURRENT_CONNECTIONS = 4

_RULE_CHARACTER = re.compile(r"[^a-z0-9]+")


class InvalidReolinkWebhook(ValueError):
    pass


class UnauthorizedReolinkWebhook(PermissionError):
    pass


@dataclass(frozen=True)
class ReolinkWebhookConfig:
    enabled: bool
    host: str = DEFAULT_REOLINK_WEBHOOK_HOST
    port: int = DEFAULT_REOLINK_WEBHOOK_PORT
    secret: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class SanitizedCameraEvent:
    event_id: str
    event_type: str
    rule_id: str | None
    received_at: datetime
    event_at: datetime | None = None


@dataclass(frozen=True)
class WebhookResponse:
    status: int


def load_reolink_webhook_config(environment: Mapping[str, str]) -> ReolinkWebhookConfig:
    secret = environment.get("GATE_REOLINK_WEBHOOK_SECRET")
    if secret is None or secret == "":
        return ReolinkWebhookConfig(enabled=False)
    if (
        not isinstance(secret, str)
        or not MIN_REOLINK_WEBHOOK_SECRET_LENGTH <= len(secret) <= MAX_REOLINK_WEBHOOK_SECRET_LENGTH
        or any(ord(character) < 33 or ord(character) > 126 for character in secret)
    ):
        raise ValueError(
            "GATE_REOLINK_WEBHOOK_SECRET must be 20-128 printable non-space characters"
        )

    host = environment.get(
        "GATE_REOLINK_WEBHOOK_HOST", DEFAULT_REOLINK_WEBHOOK_HOST
    )
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError("GATE_REOLINK_WEBHOOK_HOST must be an IP literal") from error
    if address.version != 4:
        raise ValueError("GATE_REOLINK_WEBHOOK_HOST must be an IPv4 literal")
    if not (address.is_unspecified or address.is_private or address.is_loopback):
        raise ValueError("GATE_REOLINK_WEBHOOK_HOST must be private or unspecified")

    try:
        port = int(environment.get(
            "GATE_REOLINK_WEBHOOK_PORT", str(DEFAULT_REOLINK_WEBHOOK_PORT)
        ))
    except (TypeError, ValueError) as error:
        raise ValueError("GATE_REOLINK_WEBHOOK_PORT must be an integer") from error
    if not 1024 <= port <= 65535:
        raise ValueError("GATE_REOLINK_WEBHOOK_PORT must be between 1024 and 65535")
    return ReolinkWebhookConfig(True, str(address), port, secret)


class ReolinkEventCorrelator:
    """Retain a small, expiring set of sanitized camera trigger summaries."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 15.0,
        correlation_window_seconds: float = 5.0,
        max_events: int = MAX_REOLINK_EVENTS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 0 < ttl_seconds <= MAX_REOLINK_EVENT_TTL_SECONDS:
            raise ValueError("event TTL exceeds the safe range")
        if not 0 < correlation_window_seconds <= min(
            ttl_seconds, MAX_REOLINK_CORRELATION_WINDOW_SECONDS
        ):
            raise ValueError("correlation window exceeds the safe range")
        if not 1 <= max_events <= MAX_REOLINK_EVENTS:
            raise ValueError("event capacity exceeds the safe range")
        self._ttl = timedelta(seconds=ttl_seconds)
        self._window = timedelta(seconds=correlation_window_seconds)
        self._max_events = max_events
        self._max_seen = max_events * 2
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._events: deque[SanitizedCameraEvent] = deque()
        self._seen: OrderedDict[str, datetime] = OrderedDict()
        self._lock = Lock()

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._events)

    @property
    def dedup_count(self) -> int:
        with self._lock:
            return len(self._seen)

    def record(
        self, event: SanitizedCameraEvent, *, now: datetime | None = None
    ) -> str:
        now = _utc(now or self._clock())
        received_at = _utc(event.received_at)
        event_at = _utc(event.event_at) if event.event_at is not None else None
        with self._lock:
            self._prune(now)
            age = now - received_at
            if age > self._ttl or age < -self._window:
                return "stale"
            if event_at is not None:
                event_age = now - event_at
                if event_age > self._ttl or event_age < -self._window:
                    return "stale"
            if event.event_id in self._seen:
                return "duplicate"
            self._seen[event.event_id] = received_at
            while len(self._seen) > self._max_seen:
                self._seen.popitem(last=False)
            self._events.append(event)
            while len(self._events) > self._max_events:
                self._events.popleft()
        return "accepted"

    def correlate(
        self, received_at: datetime, *, now: datetime | None = None
    ) -> TriggerTelemetry:
        received_at = _utc(received_at)
        now = _utc(now or self._clock())
        with self._lock:
            self._prune(now)
            candidates = [
                event for event in self._events
                if abs(event.received_at - received_at) <= self._window
            ]
            if not candidates:
                return _ftp_fallback()
            matched = min(
                candidates,
                key=lambda event: (
                    abs(event.received_at - received_at), event.received_at,
                    event.event_id,
                ),
            )
            self._events.remove(matched)
        return TriggerTelemetry(
            source="reolink_webhook",
            event_type=matched.event_type,
            rule_id=matched.rule_id,
            correlation="matched",
            event_at=matched.event_at,
            delta_ms=abs((matched.received_at - received_at).total_seconds()) * 1000,
        )

    def _prune(self, now: datetime) -> None:
        self._events = deque(
            event for event in self._events
            if now - _utc(event.received_at) <= self._ttl
        )
        for event_id, observed_at in tuple(self._seen.items()):
            if now - _utc(observed_at) > self._ttl:
                self._seen.pop(event_id, None)


class ReolinkWebhookEndpoint:
    """Parse one bounded HTTP request into metadata-only correlation state."""

    def __init__(self, secret: str, correlator: ReolinkEventCorrelator,
                 on_accepted=None) -> None:
        self._secret = secret
        self._correlator = correlator
        self._on_accepted = on_accepted

    def handle(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        read_body: Callable[[int], bytes],
        *,
        received_at: datetime | None = None,
    ) -> WebhookResponse:
        if path != REOLINK_WEBHOOK_PATH:
            return WebhookResponse(404)
        if method != "POST":
            return WebhookResponse(405)
        content_type = (_header(headers, "Content-Type") or "").split(";", 1)[0]
        if content_type.strip().lower() != "application/json":
            return self._reject(415, "content_type")
        raw_length = _header(headers, "Content-Length")
        try:
            content_length = int(raw_length) if raw_length is not None else 0
        except (TypeError, ValueError):
            return self._reject(400, "content_length")
        if content_length <= 0:
            return self._reject(411, "content_length")
        if content_length > MAX_REOLINK_WEBHOOK_BODY_BYTES:
            return self._reject(413, "body_too_large")
        try:
            body = read_body(content_length)
        except Exception:
            return self._reject(400, "body_read")
        if not isinstance(body, bytes) or len(body) != content_length:
            return self._reject(400, "body_length")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return self._reject(400, "json")

        received_at = _utc(received_at or datetime.now(timezone.utc))
        try:
            event = _sanitize_event(payload, self._secret, received_at)
        except UnauthorizedReolinkWebhook:
            return self._reject(401, "unauthorized")
        except InvalidReolinkWebhook:
            return self._reject(400, "payload")
        outcome = self._correlator.record(event, now=received_at)
        if outcome == "accepted":
            self._notify_accepted(event)
            return WebhookResponse(202)
        if outcome == "duplicate":
            return WebhookResponse(200)
        return self._reject(422, "stale")

    def _notify_accepted(self, event: SanitizedCameraEvent) -> None:
        # The callback only schedules a bounded frame capture; it must never
        # delay or change the webhook response, and it receives only the
        # sanitized event.
        if self._on_accepted is None:
            return
        try:
            self._on_accepted(event)
        except Exception:
            LOGGER.warning("reolink_webhook on_accepted=failed", exc_info=True)

    @staticmethod
    def _reject(status: int, reason: str) -> WebhookResponse:
        LOGGER.warning("reolink_webhook status=rejected reason=%s", reason)
        return WebhookResponse(status)


class BoundedReolinkHTTPServer(ThreadingMixIn, HTTPServer):
    """Serve a fixed number of short webhook requests concurrently."""

    max_concurrent_connections = MAX_REOLINK_CONCURRENT_CONNECTIONS
    request_queue_size = MAX_REOLINK_CONCURRENT_CONNECTIONS
    daemon_threads = True
    block_on_close = False

    def __init__(
        self, *args, max_concurrent_connections: int | None = None, **kwargs
    ) -> None:
        capacity = (
            self.max_concurrent_connections
            if max_concurrent_connections is None
            else max_concurrent_connections
        )
        if not 1 <= capacity <= MAX_REOLINK_CONCURRENT_CONNECTIONS:
            raise ValueError("concurrent webhook capacity exceeds the safe range")
        self._connection_slots = BoundedSemaphore(capacity)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address) -> None:
        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()


class ReolinkWebhookWorker:
    def __init__(
        self,
        config: ReolinkWebhookConfig,
        correlator: ReolinkEventCorrelator,
        *,
        server_factory=BoundedReolinkHTTPServer,
        on_accepted=None,
    ) -> None:
        if not config.enabled or config.secret is None:
            raise ValueError("Reolink webhook worker requires enabled configuration")
        self._config = config
        self._endpoint = ReolinkWebhookEndpoint(
            config.secret, correlator, on_accepted=on_accepted,
        )
        self._server_factory = server_factory

    def run_forever(self, stop_event) -> None:
        endpoint = self._endpoint

        class Handler(_ReolinkRequestHandler):
            pass

        Handler.endpoint = endpoint
        server = self._server_factory(
            (self._config.host, self._config.port), Handler
        )
        server.timeout = 0.25
        try:
            while not stop_event.is_set():
                server.handle_request()
        finally:
            server.server_close()


class _ReolinkRequestHandler(BaseHTTPRequestHandler):
    endpoint: ReolinkWebhookEndpoint
    protocol_version = "HTTP/1.0"

    def setup(self) -> None:
        self.request.settimeout(REOLINK_CONNECTION_TIMEOUT_SECONDS)
        super().setup()

    def handle(self) -> None:
        self._request_deadline_lock = Lock()
        self._request_deadline_active = True
        deadline_timer = Timer(
            REOLINK_REQUEST_DEADLINE_SECONDS, self._expire_request
        )
        deadline_timer.daemon = True
        deadline_timer.start()
        try:
            super().handle()
        finally:
            with self._request_deadline_lock:
                self._request_deadline_active = False
            deadline_timer.cancel()

    def _expire_request(self) -> None:
        with self._request_deadline_lock:
            if not self._request_deadline_active:
                return
            self._request_deadline_active = False
        self.close_connection = True
        try:
            self.request.shutdown(SHUT_RDWR)
        except OSError:
            pass
        try:
            self.request.close()
        except OSError:
            pass

    def do_POST(self) -> None:
        self._handle()

    def do_GET(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def _handle(self) -> None:
        response = self.endpoint.handle(
            self.command, self.path, self.headers, self._read_body
        )
        self.send_response(response.status)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _read_body(self, content_length: int) -> bytes:
        deadline = monotonic() + REOLINK_BODY_DEADLINE_SECONDS
        remaining = content_length
        chunks = []
        read_one = getattr(self.rfile, "read1", None)
        if not callable(read_one):
            read_one = self.rfile.read
        try:
            while remaining:
                time_left = deadline - monotonic()
                if time_left <= 0:
                    raise TimeoutError("webhook body deadline exceeded")
                self.request.settimeout(time_left)
                chunk = read_one(min(remaining, MAX_REOLINK_BODY_READ_BYTES))
                if monotonic() > deadline:
                    raise TimeoutError("webhook body deadline exceeded")
                if not chunk:
                    break
                chunk = chunk[:remaining]
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
        finally:
            self.request.settimeout(REOLINK_CONNECTION_TIMEOUT_SECONDS)

    def log_message(self, _format: str, *_args) -> None:
        return


def _sanitize_event(
    payload: object, secret: str, received_at: datetime
) -> SanitizedCameraEvent:
    if not isinstance(payload, dict):
        raise InvalidReolinkWebhook("payload must be an object")
    supplied_secret = payload.get("secret")
    if not isinstance(supplied_secret, str) or not hmac.compare_digest(
        supplied_secret, secret
    ):
        raise UnauthorizedReolinkWebhook("invalid secret")
    alarm = payload.get("alarm")
    if not isinstance(alarm, dict):
        raise InvalidReolinkWebhook("alarm must be an object")

    alarm_type = _bounded_string(alarm.get("type"), required=True)
    outer_type = _bounded_string(payload.get("type"), required=False)
    name = _bounded_string(alarm.get("name"), required=False)
    message = _bounded_string(alarm.get("message"), required=False)
    channel = _bounded_scalar(alarm.get("channel"))
    alarm_time = _bounded_string(alarm.get("alarmTime"), required=False)
    fallback_time = _bounded_string(alarm.get("time"), required=False)
    event_at = _parse_event_time(alarm_time) or _parse_event_time(fallback_time)
    event_type = _event_type(outer_type, alarm_type, name, message)
    rule_id = (
        _rule_id(name)
        or _rule_id(alarm_type)
        or f"reolink_{event_type}"
    )
    identity = json.dumps(
        {
            "alarm_time": alarm_time or fallback_time,
            "channel": channel,
            "event_type": event_type,
            "outer_type": outer_type,
            "rule_id": rule_id,
            "received_at": (
                None if alarm_time or fallback_time else received_at.isoformat()
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return SanitizedCameraEvent(
        event_id=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        event_type=event_type,
        rule_id=rule_id,
        received_at=received_at,
        event_at=event_at,
    )


def _bounded_string(value: object, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if (
        not isinstance(value, str)
        or (required and not value)
        or len(value) > MAX_REOLINK_WEBHOOK_FIELD_LENGTH
        or any(ord(character) < 32 for character in value)
    ):
        raise InvalidReolinkWebhook("invalid bounded field")
    return value or None


def _bounded_scalar(value: object) -> str | None:
    """Accept the numeric channel index Reolink firmware sends as JSON."""
    if isinstance(value, bool):
        raise InvalidReolinkWebhook("invalid bounded field")
    if isinstance(value, int):
        if not 0 <= value <= 255:
            raise InvalidReolinkWebhook("invalid bounded field")
        return str(value)
    return _bounded_string(value, required=False)


def _event_type(*values: str | None) -> str:
    combined = " ".join(value.lower() for value in values if value)
    normalized = _RULE_CHARACTER.sub("_", combined)
    outer_type = _RULE_CHARACTER.sub("_", (values[0] or "").lower()).strip("_")
    if outer_type in {"test", "manual", "manual_test"}:
        return "manual_test"
    if "line_cross" in normalized:
        return "line_crossing"
    if "vehicle" in normalized or "car" in normalized:
        return "vehicle"
    return "other"


def _rule_id(value: str | None) -> str | None:
    if not value:
        return None
    normalized = _RULE_CHARACTER.sub("_", value.lower()).strip("_")
    return normalized[:64] or None


_COMPACT_OFFSET = re.compile(r"([+-])(\d{2})(\d{2})$")


def _parse_event_time(value: str | None) -> datetime | None:
    """Parse the camera's alarm time, including the +0000 style offset
    Reolink firmware sends, which Python 3.10 fromisoformat rejects."""
    if not value:
        return None
    normalized = _COMPACT_OFFSET.sub(r"\1\2:\3", value.replace("Z", "+00:00"))
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    expected = name.lower()
    for key, value in headers.items():
        if str(key).lower() == expected:
            return value
    return None


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("timestamp must be a datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _ftp_fallback() -> TriggerTelemetry:
    return ftp_fallback_trigger()
