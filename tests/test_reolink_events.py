import json
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from socket import SHUT_RDWR
from socketserver import ThreadingMixIn
from threading import BoundedSemaphore, Event
from unittest.mock import Mock, patch

from gate_controller.reolink_events import (
    MAX_REOLINK_WEBHOOK_BODY_BYTES,
    ReolinkEventCorrelator,
    ReolinkWebhookEndpoint,
    ReolinkWebhookWorker,
    SanitizedCameraEvent,
    _ReolinkRequestHandler,
    load_reolink_webhook_config,
)


class ReolinkWebhookTests(unittest.TestCase):
    secret = "correct-horse-battery-staple"
    now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)

    def payload(self, **alarm_overrides):
        alarm = {
            "alarmTime": "2026-08-20T10:00:00Z",
            "channel": "0",
            "channelName": "Front Gate",
            "device": "camera-private-serial",
            "deviceModel": "RLC-810A",
            "message": "Vehicle crossed Line crossing inbound",
            "name": "Line crossing inbound",
            "time": "2026-08-20 10:00:00",
            "type": "LINE_CROSSING",
        }
        alarm.update(alarm_overrides)
        return {"alarm": alarm, "secret": self.secret, "type": "alarm"}

    def request(
        self, endpoint, payload, *, content_length=None,
        content_type="application/json", received_at=None,
    ):
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Length": str(len(body) if content_length is None else content_length),
            "Content-Type": content_type,
        }
        return endpoint.handle(
            "POST", "/reolink/events", headers, BytesIO(body).read,
            received_at=received_at or self.now,
        )

    def test_accepts_default_reolink_json_and_exposes_only_sanitized_provenance(self):
        correlator = ReolinkEventCorrelator()
        endpoint = ReolinkWebhookEndpoint(self.secret, correlator)

        response = self.request(endpoint, self.payload())
        trigger = correlator.correlate(
            self.now + timedelta(milliseconds=125), now=self.now,
        )

        self.assertEqual(response.status, 202)
        self.assertEqual(trigger.to_wire(), {
            "source": "reolink_webhook",
            "event_type": "line_crossing",
            "rule_id": "line_crossing_inbound",
            "correlation": "matched",
            "event_at": "2026-08-20T10:00:00+00:00",
            "delta_ms": 125,
        })
        encoded = repr(trigger) + json.dumps(trigger.to_wire())
        self.assertNotIn(self.secret, encoded)
        self.assertNotIn("camera-private-serial", encoded)
        self.assertNotIn("Vehicle crossed", encoded)

    def test_accepts_numeric_channel_and_offset_alarm_time_from_camera_firmware(self):
        # Real firmware sends "channel": 0 (JSON number), "+0000" offsets and
        # extra fields such as "title"; none of these may reject the event.
        correlator = ReolinkEventCorrelator()
        endpoint = ReolinkWebhookEndpoint(self.secret, correlator)
        payload = self.payload(
            channel=0, alarmTime="2026-08-20T10:00:00.000+0000", title="Alarm",
        )

        response = self.request(endpoint, payload)
        trigger = correlator.correlate(
            self.now + timedelta(milliseconds=125), now=self.now,
        )

        self.assertEqual(response.status, 202)
        self.assertEqual(trigger.to_wire()["source"], "reolink_webhook")
        self.assertEqual(trigger.to_wire()["event_type"], "line_crossing")
        self.assertEqual(trigger.to_wire()["event_at"], "2026-08-20T10:00:00+00:00")

    def test_compact_offset_alarm_time_is_used_for_staleness(self):
        # An hour-old "+0000" alarm time must be parsed, not ignored, so the
        # stale check still applies on every supported Python version.
        correlator = ReolinkEventCorrelator()
        endpoint = ReolinkWebhookEndpoint(self.secret, correlator)
        payload = self.payload(alarmTime="2026-08-20T09:00:00.000+0000")

        response = self.request(endpoint, payload)

        self.assertEqual(response.status, 422)
        self.assertEqual(correlator.pending_count, 0)

    def test_rejects_non_index_channel_values(self):
        for channel in (True, -1, 256, 1.5, ["0"]):
            with self.subTest(channel=channel):
                correlator = ReolinkEventCorrelator()
                endpoint = ReolinkWebhookEndpoint(self.secret, correlator)
                response = self.request(endpoint, self.payload(channel=channel))
                self.assertEqual(response.status, 400)
                self.assertEqual(correlator.pending_count, 0)

    def test_accepted_events_notify_the_capture_hook_and_rejections_do_not(self):
        correlator = ReolinkEventCorrelator()
        notified = []
        endpoint = ReolinkWebhookEndpoint(
            self.secret, correlator, on_accepted=notified.append,
        )

        accepted = self.request(endpoint, self.payload())
        duplicate = self.request(endpoint, self.payload())
        unauthorized = self.request(endpoint, {**self.payload(), "secret": "wrong"})

        self.assertEqual((accepted.status, duplicate.status, unauthorized.status), (202, 200, 401))
        self.assertEqual(len(notified), 1)
        self.assertIsInstance(notified[0], SanitizedCameraEvent)
        self.assertEqual(notified[0].event_type, "line_crossing")
        self.assertNotIn(self.secret, repr(notified[0]))

    def test_capture_hook_failure_never_changes_the_webhook_response(self):
        correlator = ReolinkEventCorrelator()

        def explode(_event):
            raise RuntimeError("capture unavailable")

        endpoint = ReolinkWebhookEndpoint(self.secret, correlator, on_accepted=explode)

        response = self.request(endpoint, self.payload())

        self.assertEqual(response.status, 202)
        self.assertEqual(correlator.pending_count, 1)

    def test_maps_bounded_top_level_test_and_manual_types_to_manual_test(self):
        for webhook_type in ("test", "manual"):
            with self.subTest(webhook_type=webhook_type):
                correlator = ReolinkEventCorrelator()
                endpoint = ReolinkWebhookEndpoint(self.secret, correlator)
                payload = self.payload(type="alarm", name="Front Gate")
                payload["type"] = webhook_type

                response = self.request(endpoint, payload)
                trigger = correlator.correlate(self.now, now=self.now)

                self.assertEqual(response.status, 202)
                self.assertEqual(trigger.to_wire()["event_type"], "manual_test")

    def test_rejects_unauthorized_input_without_recording_or_calling_a_relay(self):
        correlator = ReolinkEventCorrelator()
        endpoint = ReolinkWebhookEndpoint(self.secret, correlator)
        payload = self.payload()
        payload["secret"] = "wrong-secret"
        relay = Mock()

        with self.assertLogs("gate_controller.reolink_events", level="WARNING") as logs:
            response = self.request(endpoint, payload)

        self.assertEqual(response.status, 401)
        self.assertEqual(
            correlator.correlate(self.now).to_wire(),
            {
                "source": "camera_ftp",
                "event_type": "unverified",
                "correlation": "unverified",
            },
        )
        relay.trigger.assert_not_called()
        combined = "\n".join(logs.output)
        self.assertNotIn(self.secret, combined)
        self.assertNotIn("wrong-secret", combined)
        self.assertNotIn("camera-private-serial", combined)

    def test_rejects_malformed_json_and_never_logs_the_raw_body(self):
        correlator = ReolinkEventCorrelator()
        endpoint = ReolinkWebhookEndpoint(self.secret, correlator)
        raw = b'{"secret":"private-webhook-secret","alarm":'
        headers = {
            "Content-Length": str(len(raw)),
            "Content-Type": "application/json",
        }

        with self.assertLogs("gate_controller.reolink_events", level="WARNING") as logs:
            response = endpoint.handle(
                "POST", "/reolink/events", headers, BytesIO(raw).read,
                received_at=self.now,
            )

        self.assertEqual(response.status, 400)
        self.assertNotIn("private-webhook-secret", "\n".join(logs.output))
        self.assertNotIn(raw.decode("utf-8"), "\n".join(logs.output))

    def test_rejects_excessively_nested_bounded_json(self):
        correlator = ReolinkEventCorrelator()
        endpoint = ReolinkWebhookEndpoint(self.secret, correlator)
        raw = ("[" * 1100 + "0" + "]" * 1100).encode("utf-8")

        with self.assertLogs("gate_controller.reolink_events", level="WARNING"):
            response = endpoint.handle(
                "POST", "/reolink/events",
                {
                    "Content-Length": str(len(raw)),
                    "Content-Type": "application/json",
                },
                BytesIO(raw).read,
                received_at=self.now,
            )

        self.assertEqual(response.status, 400)
        self.assertEqual(correlator.pending_count, 0)

    def test_rejects_oversized_body_before_reading_it(self):
        correlator = ReolinkEventCorrelator()
        endpoint = ReolinkWebhookEndpoint(self.secret, correlator)
        reader = Mock()

        response = endpoint.handle(
            "POST",
            "/reolink/events",
            {
                "Content-Length": str(MAX_REOLINK_WEBHOOK_BODY_BYTES + 1),
                "Content-Type": "application/json",
            },
            reader,
            received_at=self.now,
        )

        self.assertEqual(response.status, 413)
        reader.assert_not_called()

    def test_rejects_unbounded_relevant_fields(self):
        correlator = ReolinkEventCorrelator()
        endpoint = ReolinkWebhookEndpoint(self.secret, correlator)

        response = self.request(endpoint, self.payload(name="x" * 129))

        self.assertEqual(response.status, 400)
        self.assertEqual(correlator.pending_count, 0)

    def test_duplicate_delivery_is_acknowledged_without_adding_a_second_event(self):
        correlator = ReolinkEventCorrelator()
        endpoint = ReolinkWebhookEndpoint(self.secret, correlator)

        first = self.request(endpoint, self.payload())
        second = self.request(endpoint, self.payload())

        self.assertEqual((first.status, second.status), (202, 200))
        self.assertEqual(correlator.pending_count, 1)

    def test_fallback_camera_time_deduplicates_network_retries(self):
        correlator = ReolinkEventCorrelator()
        endpoint = ReolinkWebhookEndpoint(self.secret, correlator)
        payload = self.payload(alarmTime=None)

        first = self.request(endpoint, payload, received_at=self.now)
        second = self.request(
            endpoint, payload, received_at=self.now + timedelta(seconds=1),
        )

        self.assertEqual((first.status, second.status), (202, 200))
        self.assertEqual(correlator.pending_count, 1)

    def test_rejects_hour_old_parseable_alarm_time_and_cannot_correlate(self):
        correlator = ReolinkEventCorrelator(
            ttl_seconds=15, correlation_window_seconds=5,
        )
        endpoint = ReolinkWebhookEndpoint(self.secret, correlator)
        payload = self.payload(alarmTime="2026-08-20T09:00:00Z")

        with self.assertLogs("gate_controller.reolink_events", level="WARNING"):
            response = self.request(endpoint, payload, received_at=self.now)
        trigger = correlator.correlate(self.now, now=self.now)

        self.assertEqual(response.status, 422)
        self.assertEqual(correlator.pending_count, 0)
        self.assertEqual(trigger.to_wire(), {
            "source": "camera_ftp",
            "event_type": "unverified",
            "correlation": "unverified",
        })

    def test_derives_a_valid_rule_id_when_normalization_is_empty(self):
        correlator = ReolinkEventCorrelator()
        endpoint = ReolinkWebhookEndpoint(self.secret, correlator)
        payload = self.payload(name="!!!", type="***", message="")

        response = self.request(endpoint, payload)
        trigger = correlator.correlate(self.now, now=self.now)

        self.assertEqual(response.status, 202)
        self.assertEqual(trigger.to_wire(), {
            "source": "reolink_webhook",
            "event_type": "other",
            "rule_id": "reolink_other",
            "correlation": "matched",
            "event_at": "2026-08-20T10:00:00+00:00",
            "delta_ms": 0,
        })


class ReolinkEventCorrelatorTests(unittest.TestCase):
    now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)

    def event(self, event_id, offset, *, event_type="vehicle", rule_id="vehicle_alert"):
        return SanitizedCameraEvent(
            event_id=event_id,
            event_type=event_type,
            rule_id=rule_id,
            received_at=self.now + timedelta(seconds=offset),
            event_at=self.now + timedelta(seconds=offset),
        )

    def test_rejects_stale_events(self):
        correlator = ReolinkEventCorrelator(ttl_seconds=10)

        status = correlator.record(self.event("old", -11), now=self.now)

        self.assertEqual(status, "stale")
        self.assertEqual(correlator.pending_count, 0)

    def test_correlates_the_nearest_event_and_consumes_only_that_event(self):
        correlator = ReolinkEventCorrelator(correlation_window_seconds=3)
        correlator.record(self.event("early", -1), now=self.now)
        correlator.record(
            self.event("nearest", 0.2, event_type="line_crossing", rule_id="inbound"),
            now=self.now,
        )

        matched = correlator.correlate(self.now, now=self.now)

        self.assertEqual(matched.event_type, "line_crossing")
        self.assertEqual(matched.rule_id, "inbound")
        self.assertEqual(matched.delta_ms, 200)
        self.assertEqual(correlator.pending_count, 1)

    def test_expired_or_unmatched_events_preserve_the_ftp_fallback(self):
        correlator = ReolinkEventCorrelator(
            ttl_seconds=10, correlation_window_seconds=2,
        )
        correlator.record(self.event("far", -4), now=self.now)

        fallback = correlator.correlate(self.now, now=self.now)

        self.assertEqual(fallback.to_wire(), {
            "source": "camera_ftp",
            "event_type": "unverified",
            "correlation": "unverified",
        })

    def test_pending_and_dedup_state_are_bounded(self):
        correlator = ReolinkEventCorrelator(max_events=2)

        for index in range(4):
            correlator.record(self.event(f"event-{index}", index / 10), now=self.now)

        self.assertEqual(correlator.pending_count, 2)
        self.assertLessEqual(correlator.dedup_count, 4)


class ReolinkWebhookConfigurationTests(unittest.TestCase):
    def test_request_handler_bounds_client_read_time(self):
        handler = object.__new__(_ReolinkRequestHandler)
        handler.request = Mock()

        with patch("http.server.BaseHTTPRequestHandler.setup") as setup:
            handler.setup()

        handler.request.settimeout.assert_called_once_with(1.0)
        setup.assert_called_once_with()

    def test_default_server_bounds_concurrent_client_connections(self):
        config = load_reolink_webhook_config({
            "GATE_REOLINK_WEBHOOK_SECRET": "correct-horse-battery-staple",
        })
        worker = ReolinkWebhookWorker(config, ReolinkEventCorrelator())
        server_type = worker._server_factory

        self.assertTrue(issubclass(server_type, ThreadingMixIn))
        self.assertEqual(server_type.max_concurrent_connections, 4)
        self.assertTrue(server_type.daemon_threads)
        self.assertFalse(server_type.block_on_close)

        server = object.__new__(server_type)
        server._connection_slots = BoundedSemaphore(1)
        server.shutdown_request = Mock()
        with patch("socketserver.ThreadingMixIn.process_request") as dispatch:
            server.process_request("first", ("127.0.0.1", 1000))
            server.process_request("second", ("127.0.0.1", 1001))

        dispatch.assert_called_once_with("first", ("127.0.0.1", 1000))
        server.shutdown_request.assert_called_once_with("second")

        with patch("http.server.HTTPServer.__init__", return_value=None):
            with self.assertRaises(ValueError):
                server_type(
                    ("127.0.0.1", 0), object,
                    max_concurrent_connections=0,
                )

    def test_request_handler_closes_stalled_client_at_absolute_deadline(self):
        handler = object.__new__(_ReolinkRequestHandler)
        handler.request = Mock()
        handler.close_connection = False
        timer = Mock()
        callback = {}

        def timer_factory(interval, deadline_callback):
            callback["interval"] = interval
            callback["fire"] = deadline_callback
            return timer

        def stall_until_deadline():
            fire = callback.get("fire")
            if fire is not None:
                fire()

        with patch(
            "gate_controller.reolink_events.Timer",
            side_effect=timer_factory,
            create=True,
        ) as timer_type, patch(
            "http.server.BaseHTTPRequestHandler.handle",
            side_effect=stall_until_deadline,
        ):
            handler.handle()

        timer_type.assert_called_once()
        self.assertEqual(callback["interval"], 1.0)
        self.assertTrue(timer.daemon)
        timer.start.assert_called_once_with()
        timer.cancel.assert_called_once_with()
        handler.request.shutdown.assert_called_once_with(SHUT_RDWR)
        handler.request.close.assert_called_once_with()
        self.assertTrue(handler.close_connection)

    def test_request_deadline_late_callback_does_not_close_completed_client(self):
        handler = object.__new__(_ReolinkRequestHandler)
        handler.request = Mock()
        timer = Mock()
        callback = {}

        def timer_factory(_interval, deadline_callback):
            callback["fire"] = deadline_callback
            return timer

        with patch(
            "gate_controller.reolink_events.Timer",
            side_effect=timer_factory,
            create=True,
        ) as timer_type, patch("http.server.BaseHTTPRequestHandler.handle"):
            handler.handle()

        timer_type.assert_called_once()
        timer.cancel.assert_called_once_with()
        callback["fire"]()
        handler.request.shutdown.assert_not_called()
        handler.request.close.assert_not_called()

    def test_request_handler_enforces_a_whole_body_deadline(self):
        class AdvancingClock:
            def __init__(self):
                self.value = 0.0

            def __call__(self):
                return self.value

        class SlowReader:
            def __init__(self, clock):
                self.clock = clock
                self.reads = 0

            def read(self, _size):
                self.reads += 1
                self.clock.value += 0.4
                return b"x"

        clock = AdvancingClock()
        handler = object.__new__(_ReolinkRequestHandler)
        handler.request = Mock()
        handler.rfile = SlowReader(clock)

        with patch("gate_controller.reolink_events.monotonic", clock):
            with self.assertRaises(TimeoutError):
                handler._read_body(3)

        self.assertEqual(handler.rfile.reads, 3)
        configured_timeouts = [
            call.args[0] for call in handler.request.settimeout.call_args_list
        ]
        for actual, expected in zip(configured_timeouts[:3], (1.0, 0.6, 0.2)):
            self.assertAlmostEqual(actual, expected)

    def test_request_handler_rechecks_deadline_after_each_buffered_raw_read(self):
        class AdvancingClock:
            def __init__(self):
                self.value = 0.0

            def __call__(self):
                return self.value

        class SlowBufferedReader:
            def __init__(self, clock):
                self.clock = clock
                self.read_calls = 0
                self.read1_calls = 0

            def read(self, _size):
                self.read_calls += 1
                raise AssertionError("buffered read can hide repeated socket reads")

            def read1(self, _size):
                self.read1_calls += 1
                self.clock.value += 0.4
                return b"x"

        clock = AdvancingClock()
        handler = object.__new__(_ReolinkRequestHandler)
        handler.request = Mock()
        handler.rfile = SlowBufferedReader(clock)

        with patch("gate_controller.reolink_events.monotonic", clock):
            with self.assertRaises(TimeoutError):
                handler._read_body(4)

        self.assertEqual(handler.rfile.read_calls, 0)
        self.assertEqual(handler.rfile.read1_calls, 3)
        configured_timeouts = [
            call.args[0] for call in handler.request.settimeout.call_args_list
        ]
        for actual, expected in zip(configured_timeouts[:3], (1.0, 0.6, 0.2)):
            self.assertAlmostEqual(actual, expected)

    def test_disabled_without_a_secret_and_uses_a_lan_listener_when_enabled(self):
        disabled = load_reolink_webhook_config({})
        configured = load_reolink_webhook_config({
            "GATE_REOLINK_WEBHOOK_SECRET": "correct-horse-battery-staple",
        })

        self.assertFalse(disabled.enabled)
        self.assertTrue(configured.enabled)
        self.assertEqual(configured.host, "0.0.0.0")
        self.assertEqual(configured.port, 8766)
        self.assertNotIn("correct-horse-battery-staple", repr(configured))

    def test_rejects_weak_or_unsafe_listener_configuration(self):
        invalid = (
            {"GATE_REOLINK_WEBHOOK_SECRET": "short"},
            {
                "GATE_REOLINK_WEBHOOK_SECRET": "correct-horse-battery-staple",
                "GATE_REOLINK_WEBHOOK_HOST": "public.example.com",
            },
            {
                "GATE_REOLINK_WEBHOOK_SECRET": "correct-horse-battery-staple",
                "GATE_REOLINK_WEBHOOK_HOST": "::1",
            },
            {
                "GATE_REOLINK_WEBHOOK_SECRET": "correct-horse-battery-staple",
                "GATE_REOLINK_WEBHOOK_PORT": "80",
            },
        )
        for environment in invalid:
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                load_reolink_webhook_config(environment)

    def test_worker_is_a_metadata_only_background_worker(self):
        config = load_reolink_webhook_config({
            "GATE_REOLINK_WEBHOOK_SECRET": "correct-horse-battery-staple",
        })
        correlator = ReolinkEventCorrelator()
        server = Mock()
        server_factory = Mock(return_value=server)
        server.handle_request.side_effect = lambda: stop.set()
        stop = Event()
        worker = ReolinkWebhookWorker(
            config, correlator, server_factory=server_factory,
        )

        worker.run_forever(stop)

        server.handle_request.assert_called_once_with()
        server.server_close.assert_called_once_with()
        self.assertFalse(hasattr(worker, "relay"))


if __name__ == "__main__":
    unittest.main()


class ReolinkWebhookUrlSecretTests(unittest.TestCase):
    """Reolink firmware posts a custom body verbatim, so the shared secret
    can travel as a URL query parameter alongside the camera's default body."""

    secret = "correct-horse-battery-staple"
    now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)

    def default_body(self, **alarm_overrides):
        alarm = {
            "alarmTime": "2026-08-20T10:00:00.000+0000",
            "channel": 0,
            "channelName": "Front Gate",
            "device": "Front Gate",
            "deviceModel": "RLC-810A",
            "message": "Vehicle detected",
            "name": "Vehicle rule",
            "title": "Alarm",
            "type": "VEHICLE",
        }
        alarm.update(alarm_overrides)
        return {"alarm": alarm}

    def post(self, endpoint, path, payload):
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Length": str(len(body)), "Content-Type": "application/json",
        }
        return endpoint.handle(
            "POST", path, headers, BytesIO(body).read, received_at=self.now,
        )

    def test_accepts_the_camera_default_body_with_a_url_secret(self):
        correlator = ReolinkEventCorrelator()
        endpoint = ReolinkWebhookEndpoint(self.secret, correlator)

        response = self.post(
            endpoint, f"/reolink/events?secret={self.secret}", self.default_body(),
        )
        trigger = correlator.correlate(
            self.now + timedelta(milliseconds=125), now=self.now,
        )

        self.assertEqual(response.status, 202)
        self.assertEqual(trigger.to_wire()["event_type"], "vehicle")
        self.assertEqual(trigger.to_wire()["correlation"], "matched")

    def test_rejects_a_wrong_missing_or_repeated_url_secret(self):
        for path in (
            "/reolink/events?secret=wrong-secret-of-adequate-length",
            "/reolink/events",
            "/reolink/events?secret=",
            f"/reolink/events?secret={self.secret}&secret={self.secret}",
            f"/reolink/events?token={self.secret}",
        ):
            with self.subTest(path=path):
                endpoint = ReolinkWebhookEndpoint(self.secret, ReolinkEventCorrelator())
                response = self.post(endpoint, path, self.default_body())
                self.assertEqual(response.status, 401)

    def test_a_wrong_body_secret_is_not_rescued_by_the_url(self):
        endpoint = ReolinkWebhookEndpoint(self.secret, ReolinkEventCorrelator())
        payload = self.default_body()
        payload["secret"] = "wrong-secret-of-adequate-length"

        response = self.post(endpoint, f"/reolink/events?secret={self.secret}", payload)

        self.assertEqual(response.status, 401)

    def test_the_query_never_changes_the_route(self):
        endpoint = ReolinkWebhookEndpoint(self.secret, ReolinkEventCorrelator())

        response = self.post(endpoint, f"/other?secret={self.secret}", self.default_body())

        self.assertEqual(response.status, 404)

    def test_a_test_notification_is_recorded_as_manual_test(self):
        correlator = ReolinkEventCorrelator()
        endpoint = ReolinkWebhookEndpoint(self.secret, correlator)

        response = self.post(
            endpoint, f"/reolink/events?secret={self.secret}",
            self.default_body(
                type="TEST", title="Test message",
                name="A webhook test message from Front Gate",
                message=(
                    "If you receive this message it means you have "
                    "successfully set up your device: Front Gate"
                ),
            ),
        )

        self.assertEqual(response.status, 202)
        self.assertEqual(
            correlator.correlate(self.now, now=self.now).to_wire()["event_type"],
            "manual_test",
        )
