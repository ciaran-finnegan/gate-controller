import unittest

from gate_controller.cloud_health import TransitionLogger
from gate_controller.cloudflare_client import CloudflareStatusReporter
from gate_controller.control_plane import HeartbeatWorker


class FakeClient:
    def __init__(self):
        self.requests = []

    def post_json(self, path, payload, *, headers=None):
        self.requests.append((path, payload, headers or {}))
        return {}


class ControlPlaneTests(unittest.TestCase):
    def test_cloudflare_status_reporter_binds_the_configured_controller_identity(self):
        reporter = CloudflareStatusReporter(FakeClient(), "primary")
        status = {"controller_id": "secondary", "queue_depth": 2}

        reporter.heartbeat(status)

        self.assertEqual(reporter.client.requests, [(
            "/api/controller/status", {"controller_id": "primary", "queue_depth": 2}, {},
        )])

    def test_heartbeat_worker_forwards_status_to_the_cloudflare_reporter(self):
        reporter = CloudflareStatusReporter(FakeClient(), "primary")

        self.assertTrue(HeartbeatWorker(reporter, lambda: {"queue_depth": 2}).run_once())

        self.assertEqual(reporter.client.requests[0][1], {
            "controller_id": "primary", "queue_depth": 2,
        })

    def test_heartbeat_worker_reports_a_delivery_failure_without_raising(self):
        class FailingReporter:
            @staticmethod
            def heartbeat(status):
                raise TimeoutError("offline")

        self.assertFalse(HeartbeatWorker(FailingReporter(), lambda: {"queue_depth": 2}).run_once())

    def test_heartbeat_failures_are_logged_as_a_transition_and_recovery(self):
        calls = {"fail": True}

        class Reporter:
            @staticmethod
            def heartbeat(status):
                if calls["fail"]:
                    raise TimeoutError("offline")

        import logging
        clock = [0.0]
        worker = HeartbeatWorker(
            Reporter(), lambda: {"queue_depth": 0},
            health=TransitionLogger(
                logging.getLogger("gate_controller.control_plane"), "heartbeat",
                repeat_interval=600.0, clock=lambda: clock[0],
            ),
        )

        with self.assertLogs("gate_controller.control_plane", level="WARNING") as logs:
            self.assertFalse(worker.run_once())
            self.assertFalse(worker.run_once())
        self.assertEqual(len(logs.output), 1)
        self.assertIn("gate_cloud stage=heartbeat_failed error_type=TimeoutError", logs.output[0])

        calls["fail"] = False
        with self.assertLogs("gate_controller.control_plane", level="INFO") as logs:
            self.assertTrue(worker.run_once())
        self.assertIn("stage=heartbeat_recovered failures=2", logs.output[0])

    def test_default_heartbeat_worker_logs_a_failure_without_help(self):
        class FailingReporter:
            @staticmethod
            def heartbeat(status):
                raise TimeoutError("offline")

        with self.assertLogs("gate_controller.control_plane", level="WARNING"):
            self.assertFalse(HeartbeatWorker(FailingReporter(), lambda: {}).run_once())


class HeartbeatRoundTripTests(unittest.TestCase):
    """The Pi to Cloudflare round trip is timed, not probed: zero extra traffic."""

    def test_no_round_trip_is_reported_before_the_first_post(self):
        worker = HeartbeatWorker(
            CloudflareStatusReporter(FakeClient(), "primary"), dict,
        )

        self.assertEqual(
            {"heartbeat_rtt_ms": None, "heartbeat_consecutive_failures": 0},
            worker.metrics(),
        )

    def test_the_post_the_worker_already_makes_is_the_measurement(self):
        elapsed = iter([0.0, 0.1842])
        worker = HeartbeatWorker(
            CloudflareStatusReporter(FakeClient(), "primary"), dict,
            clock=lambda: next(elapsed),
        )

        self.assertTrue(worker.run_once())
        self.assertEqual(184.2, worker.metrics()["heartbeat_rtt_ms"])

    def test_a_failed_post_still_reports_its_duration_and_the_failure_count(self):
        class FailingReporter:
            @staticmethod
            def heartbeat(status):
                raise TimeoutError("offline")

        elapsed = iter([0.0, 3.0, 3.0, 6.0])
        worker = HeartbeatWorker(FailingReporter(), dict, clock=lambda: next(elapsed))

        self.assertFalse(worker.run_once())
        self.assertFalse(worker.run_once())

        self.assertEqual(3000.0, worker.metrics()["heartbeat_rtt_ms"])
        self.assertEqual(2, worker.metrics()["heartbeat_consecutive_failures"])

    def test_recovery_clears_the_consecutive_failure_count(self):
        reporter = CloudflareStatusReporter(FakeClient(), "primary")
        failing = True

        class Flaky:
            @staticmethod
            def heartbeat(status):
                if failing:
                    raise TimeoutError("offline")
                reporter.heartbeat(status)

        worker = HeartbeatWorker(Flaky(), dict)
        worker.run_once()
        self.assertEqual(1, worker.metrics()["heartbeat_consecutive_failures"])

        failing = False
        worker.run_once()

        self.assertEqual(0, worker.metrics()["heartbeat_consecutive_failures"])

    def test_a_clock_that_misbehaves_costs_the_measurement_not_the_heartbeat(self):
        def bad_clock():
            raise RuntimeError("no monotonic clock")

        reporter = CloudflareStatusReporter(FakeClient(), "primary")
        worker = HeartbeatWorker(reporter, lambda: {"queue_depth": 2}, clock=bad_clock)

        self.assertTrue(worker.run_once())
        self.assertEqual(1, len(reporter.client.requests))
        self.assertIsNone(worker.metrics()["heartbeat_rtt_ms"])
