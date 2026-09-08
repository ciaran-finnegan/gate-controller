"""The wire format, checked against a copy of the app's own contract.

``POST /api/controller/metrics`` rejects the **whole body** with ``400`` for a
single unrecognised key -- unlike the heartbeat, which silently drops one. A
controller that guesses a key name therefore does not lose a metric, it loses
every metric, and finds out only from a dashboard tile that never fills in.

So the rules in :func:`validate_controller_metrics` below are transcribed from
``worker/contracts/controller-metrics/contract.ts`` in ciaran-finnegan/access-gate-ui
(``origin/main``, read-only): the key allow-lists, the numeric ceilings, the
forbidden-key expression, the ``schema_version``, the 60-minute cap and the
5-minute future-skew bound. If the app's contract changes, this copy is what
has to change with it -- and the failure is a test here rather than a silent
400 at the gate.
"""
import math
import re
import unittest
from datetime import datetime, timedelta, timezone

from gate_controller.metrics import MetricsRing, MetricsRollupWorker, QuotaLedger
from gate_controller.telemetry import (
    EventTelemetry, LocalOcrTelemetry, OcrAttemptTelemetry, StageDurations,
)


SCHEMA_VERSION = 1
MAX_MINUTES_PER_POST = 60
MAX_METRIC_STRING_LENGTH = 128
CONTROLLER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
METRIC_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_FUTURE_SKEW = timedelta(minutes=5)
FORBIDDEN_METRIC_KEY = re.compile(
    r"token|secret|password|credential|path|raw_response|exception|plate|digest|sha",
    re.IGNORECASE,
)
SKIPPED_REASONS = ("hot", "loaded", "low_memory")

MINUTE_KEYS = (
    "minute_start", "heartbeats", "host", "network", "cloud", "pipeline", "recognition",
)
HOST_KEYS = (
    "soc_temp_c", "load_1m", "load_5m", "load_15m",
    "mem_total_kib", "mem_available_kib", "swap_free_kib",
    "disk_free_bytes", "disk_total_bytes", "oom_kill_total",
    "uptime_seconds", "process_uptime_seconds", "disk_sectors_written",
    "throttled_raw", "under_voltage", "arm_capped", "currently_throttled",
)
NETWORK_KEYS = (
    "router_rtt_ms", "router_loss", "tls_connect_ms",
    "uplink_receive_bytes_per_s", "uplink_transmit_bytes_per_s", "skipped_reason",
)
CLOUD_KEYS = (
    "heartbeat_rtt_ms", "heartbeat_consecutive_failures",
    "recognition_consecutive_failures", "oldest_pending_outbox_age_s", "queue_depth",
    "recognition_transport_failures_total",
    "recognition_lookups_month_to_date", "recognition_lookup_quota",
)
PIPELINE_KEYS = (
    "presence_unresolved", "dropped_frames", "lost_verdicts",
    "skipped_empty_scene", "skipped_clipped", "captures", "failures",
)
RECOGNITION_KEYS = (
    "ocr_attempts", "billed_lookups", "recognized", "unread_frames",
    "ocr_error", "ocr_timeout", "ocr_busy", "http_429",
    "ocr_ms_p50", "ocr_ms_p90", "local_attempts", "local_recognized",
)
NUMERIC_CEILINGS = {
    "soc_temp_c": 150,
    "load_1m": 1_000, "load_5m": 1_000, "load_15m": 1_000,
    "mem_total_kib": 1e12, "mem_available_kib": 1e12, "swap_free_kib": 1e12,
    "disk_free_bytes": 1e15, "disk_total_bytes": 1e15,
    "oom_kill_total": 1e9,
    "uptime_seconds": 1e9, "process_uptime_seconds": 1e9,
    "disk_sectors_written": 1e15,
    "router_rtt_ms": 60_000, "router_loss": 1,
    "tls_connect_ms": 60_000,
    "uplink_receive_bytes_per_s": 1e12, "uplink_transmit_bytes_per_s": 1e12,
    "heartbeat_rtt_ms": 600_000,
    "heartbeat_consecutive_failures": 1e6, "recognition_consecutive_failures": 1e6,
    "oldest_pending_outbox_age_s": 1e9, "queue_depth": 1e7,
    "recognition_transport_failures_total": 1e9,
    "recognition_lookups_month_to_date": 1e9, "recognition_lookup_quota": 1e9,
    "presence_unresolved": 1e7, "dropped_frames": 1e7, "lost_verdicts": 1e7,
    "skipped_empty_scene": 1e7, "skipped_clipped": 1e7, "captures": 1e7, "failures": 1e7,
    "ocr_attempts": 1e7, "billed_lookups": 1e7, "recognized": 1e7, "unread_frames": 1e7,
    "ocr_error": 1e7, "ocr_timeout": 1e7, "ocr_busy": 1e7, "http_429": 1e7,
    "ocr_ms_p50": 600_000, "ocr_ms_p90": 600_000,
    "local_attempts": 1e7, "local_recognized": 1e7,
}


class ContractError(AssertionError):
    """What the Worker would answer 400 for."""


def _assert_allowed_keys(record, allowed, field):
    for key in record:
        if FORBIDDEN_METRIC_KEY.search(key) or key not in allowed:
            raise ContractError(f"{field}.{key} is not allowed")


def _require_bounded_number(value, field, key):
    ceiling = NUMERIC_CEILINGS.get(key)
    if ceiling is None:
        raise ContractError(f"{field} is invalid")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{field} is invalid")
    if not math.isfinite(value) or value < 0 or value > ceiling:
        raise ContractError(f"{field} is invalid")


def _require_timestamp(value, field, now):
    if not isinstance(value, str):
        raise ContractError(f"{field} must be a string")
    if len(value) > MAX_METRIC_STRING_LENGTH:
        raise ContractError(f"{field} is invalid")
    if not re.match(r"^\d{4}-\d{2}-\d{2}T", value):
        raise ContractError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ContractError(f"{field} is invalid") from error
    if parsed > now + MAX_FUTURE_SKEW:
        raise ContractError(f"{field} is too far in the future")


def _narrowed_block(value, allowed, field, now):
    if value is None:
        return
    if not isinstance(value, dict):
        raise ContractError(f"{field} must be an object")
    _assert_allowed_keys(value, allowed, field)
    for key, entry in value.items():
        if entry is None:
            continue
        if key == "skipped_reason":
            if (not isinstance(entry, str) or len(entry) > MAX_METRIC_STRING_LENGTH
                    or not METRIC_TOKEN.match(entry) or entry not in SKIPPED_REASONS):
                raise ContractError(f"{field}.{key} is invalid")
        elif key == "throttled_raw":
            if not isinstance(entry, str) or not re.match(r"^0x[0-9a-f]{1,8}$", entry):
                raise ContractError(f"{field}.{key} is invalid")
        elif key in ("under_voltage", "arm_capped", "currently_throttled"):
            if not isinstance(entry, bool):
                raise ContractError(f"{field}.{key} is invalid")
        else:
            _require_bounded_number(entry, f"{field}.{key}", key)


def validate_controller_metrics(body, *, now=None):
    """A transcription of the app's `validateControllerMetrics`."""
    now = now or datetime.now(timezone.utc)
    if not isinstance(body, dict):
        raise ContractError("body must be an object")
    _assert_allowed_keys(body, ("controller_id", "schema_version", "minutes"), "body")
    controller_id = body.get("controller_id")
    if not isinstance(controller_id, str) or not CONTROLLER_ID.match(controller_id):
        raise ContractError("controller_id is invalid")
    if body.get("schema_version") != SCHEMA_VERSION:
        raise ContractError("schema_version is unsupported")
    minutes = body.get("minutes")
    if not isinstance(minutes, list) or not 1 <= len(minutes) <= MAX_MINUTES_PER_POST:
        raise ContractError("minutes is invalid")
    for index, minute in enumerate(minutes):
        field = f"minutes[{index}]"
        if not isinstance(minute, dict):
            raise ContractError(f"{field} must be an object")
        _assert_allowed_keys(minute, MINUTE_KEYS, field)
        heartbeats = minute.get("heartbeats")
        if (isinstance(heartbeats, bool) or not isinstance(heartbeats, int)
                or not 0 <= heartbeats <= 60):
            raise ContractError(f"{field}.heartbeats is invalid")
        _require_timestamp(minute.get("minute_start"), f"{field}.minute_start", now)
        _narrowed_block(minute.get("host"), HOST_KEYS, f"{field}.host", now)
        _narrowed_block(minute.get("network"), NETWORK_KEYS, f"{field}.network", now)
        _narrowed_block(minute.get("cloud"), CLOUD_KEYS, f"{field}.cloud", now)
        _narrowed_block(minute.get("pipeline"), PIPELINE_KEYS, f"{field}.pipeline", now)
        _narrowed_block(
            minute.get("recognition"), RECOGNITION_KEYS, f"{field}.recognition", now,
        )
    return body


def _walk(value, path=""):
    if isinstance(value, dict):
        for key, entry in value.items():
            yield f"{path}.{key}", key, entry
            yield from _walk(entry, f"{path}.{key}")
    elif isinstance(value, list):
        for index, entry in enumerate(value):
            yield from _walk(entry, f"{path}[{index}]")


class Recorder:
    def __init__(self):
        self.payloads = []

    def __call__(self, payload):
        self.payloads.append(payload)


class FrozenClock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now = self.now + timedelta(**kwargs)


def a_busy_minute(ring):
    ring.record_heartbeat()
    ring.record_heartbeat()
    ring.record_event_telemetry(EventTelemetry(
        trace_id="0123456789abcdef",
        stage_durations=StageDurations(),
        frames=(),
        ocr_attempts=(
            OcrAttemptTelemetry(frame_sequence=0, status="no_plate", duration_ms=940.5),
            OcrAttemptTelemetry(
                frame_sequence=1, status="ocr_error", failure_cause="read_timeout",
                duration_ms=2000.0,
            ),
            OcrAttemptTelemetry(frame_sequence=2, status="recognized", duration_ms=760.0),
        ),
        decision_outcome="opened",
        decision_reason="authorised",
        actuation_claim="won",
        actuation_attempted=True,
        relay_outcome="pulsed",
        outbox_attempt=0,
        delivery_state="pending",
        local_ocr=LocalOcrTelemetry(mode="active", frames=3, status="recognized"),
    ))


class ControllerMetricsContractTests(unittest.TestCase):
    def setUp(self):
        # Recent, but safely inside the app's 5-minute future-skew bound.
        self.clock = FrozenClock(
            datetime.now(timezone.utc).replace(second=30, microsecond=0) - timedelta(minutes=10)
        )
        self.ring = MetricsRing(
            quota=QuotaLedger(None, quota=2500, clock=self.clock),
            clock=self.clock, retry_counts=lambda: {"http_429": 1},
        )
        self.sent = Recorder()
        self.worker = MetricsRollupWorker(self.ring, self.sent, clock=self.clock)

    def payload(self, minutes=1):
        for _ in range(minutes):
            a_busy_minute(self.ring)
            self.clock.advance(minutes=1)
        self.assertEqual(self.worker.run_once(), minutes)
        return self.sent.payloads[0]

    def validate(self, payload):
        # `now` is the moment the worker posted, which is what the Worker's
        # future-skew bound is measured against.
        return validate_controller_metrics(payload, now=self.clock())

    def test_a_real_payload_passes_the_app_contract(self):
        self.validate(self.payload())

    def test_a_full_backfill_of_sixty_minutes_passes_the_contract(self):
        payload = self.payload(minutes=MAX_MINUTES_PER_POST)

        self.assertEqual(len(payload["minutes"]), MAX_MINUTES_PER_POST)
        self.validate(payload)

    def test_an_empty_minute_passes_the_contract(self):
        self.ring.record_heartbeat()
        self.clock.advance(minutes=1)
        self.worker.run_once()

        self.validate(self.sent.payloads[0])

    def test_the_payload_carries_the_recognition_and_quota_keys_the_tiles_read(self):
        minute = self.payload()["minutes"][0]

        self.assertEqual(minute["recognition"], {
            "ocr_attempts": 3, "billed_lookups": 3, "recognized": 1,
            "unread_frames": 1, "ocr_error": 1, "ocr_timeout": 0, "ocr_busy": 0,
            "http_429": 1, "ocr_ms_p50": 940, "ocr_ms_p90": 2000,
            "local_attempts": 3, "local_recognized": 1,
        })
        self.assertEqual(minute["cloud"], {
            "recognition_lookups_month_to_date": 3,
            "recognition_lookup_quota": 2500,
        })

    def test_no_key_in_the_payload_is_one_the_contract_forbids(self):
        payload = self.payload()

        for path, key, _value in _walk(payload):
            with self.subTest(path=path):
                self.assertIsNone(
                    FORBIDDEN_METRIC_KEY.search(key),
                    f"{path} matches the contract's forbidden-key expression",
                )

    def test_every_value_is_a_number_or_a_short_timestamp(self):
        for path, _key, value in _walk(self.payload()):
            if isinstance(value, (dict, list)):
                continue
            with self.subTest(path=path):
                self.assertIsInstance(value, (int, float, str))
                if isinstance(value, str):
                    self.assertLessEqual(len(value), MAX_METRIC_STRING_LENGTH)

    def test_the_contract_copy_rejects_the_key_this_controller_must_not_send(self):
        payload = self.payload()
        payload["minutes"][0]["recognition"]["no_plate"] = 1

        with self.assertRaises(ContractError):
            self.validate(payload)

    def test_the_contract_copy_rejects_an_unknown_key(self):
        payload = self.payload()
        payload["minutes"][0]["recognition"]["ocr_giveups"] = 1

        with self.assertRaises(ContractError):
            self.validate(payload)

    def test_the_contract_copy_rejects_a_counter_above_its_ceiling(self):
        payload = self.payload()
        payload["minutes"][0]["recognition"]["ocr_attempts"] = 1e7 + 1

        with self.assertRaises(ContractError):
            self.validate(payload)

    def test_the_documented_example_payload_is_the_one_that_is_sent(self):
        """docs/deployment.md prints this body; it must be a valid one."""
        example = {
            "controller_id": "primary",
            "schema_version": 1,
            "minutes": [{
                "minute_start": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z"),
                "heartbeats": 4,
                "recognition": {
                    "ocr_attempts": 3, "billed_lookups": 2, "recognized": 1,
                    "unread_frames": 1, "ocr_error": 1, "ocr_timeout": 0,
                    "ocr_busy": 0, "http_429": 1, "ocr_ms_p50": 940,
                    "ocr_ms_p90": 2000, "local_attempts": 3, "local_recognized": 1,
                },
                "cloud": {
                    "recognition_lookups_month_to_date": 412,
                    "recognition_lookup_quota": 2500,
                },
            }],
        }

        validate_controller_metrics(example)
        self.assertEqual(
            set(example["minutes"][0]["recognition"]),
            set(self.payload()["minutes"][0]["recognition"]),
        )


if __name__ == "__main__":
    unittest.main()
