import unittest

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
