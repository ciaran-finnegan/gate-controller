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
