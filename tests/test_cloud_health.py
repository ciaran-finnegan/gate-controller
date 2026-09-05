import logging
import unittest

import requests

from gate_controller.cloud_health import TransitionLogger, error_detail


class TransitionLoggerTests(unittest.TestCase):
    def _logger(self, clock):
        return TransitionLogger(
            logging.getLogger("gate_controller.test_cloud_health"), "heartbeat",
            repeat_interval=600.0, clock=lambda: clock[0],
        )

    def test_first_failure_warns_then_repeats_only_after_the_interval(self):
        clock = [1000.0]
        health = self._logger(clock)

        with self.assertLogs("gate_controller.test_cloud_health", level="WARNING") as logs:
            health.failure(TimeoutError("offline"))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("stage=heartbeat_failed error_type=TimeoutError detail=TimeoutError consecutive=1", logs.output[0])

        with self.assertNoLogs("gate_controller.test_cloud_health", level="WARNING"):
            clock[0] += 599.0
            health.failure(TimeoutError("offline"))
            health.failure(TimeoutError("offline"))

        clock[0] += 2.0
        with self.assertLogs("gate_controller.test_cloud_health", level="WARNING") as logs:
            health.failure(TimeoutError("offline"))
        self.assertIn("consecutive=4", logs.output[0])

    def test_recovery_is_logged_once_with_the_failure_count(self):
        clock = [0.0]
        health = self._logger(clock)
        with self.assertLogs("gate_controller.test_cloud_health", level="WARNING"):
            health.failure(RuntimeError("down"))
        health.failure(RuntimeError("down"))

        with self.assertLogs("gate_controller.test_cloud_health", level="INFO") as logs:
            health.success()
        self.assertEqual(len(logs.output), 1)
        self.assertIn("stage=heartbeat_recovered failures=2", logs.output[0])

        with self.assertNoLogs("gate_controller.test_cloud_health", level="INFO"):
            health.success()
        self.assertEqual(health.consecutive_failures, 0)

    def test_detail_exposes_only_the_http_status_never_the_message(self):
        response = requests.Response()
        response.status_code = 500
        error = requests.HTTPError("500 Server Error for url: https://example.test/api?secret=x", response=response)

        self.assertEqual(error_detail(error), "http_500")
        self.assertEqual(error_detail(requests.ConnectionError("host=secret")), "ConnectionError")

    def test_repeat_interval_must_be_positive(self):
        with self.assertRaises(ValueError):
            TransitionLogger(logging.getLogger("x"), "heartbeat", repeat_interval=0)
