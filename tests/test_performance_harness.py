import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


HARNESS_PATH = Path(__file__).parents[1] / "scripts" / "pi-cloudflare-performance-harness.py"


def load_harness():
    spec = importlib.util.spec_from_file_location("pi_cloudflare_performance_harness", HARNESS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PerformanceHarnessTests(unittest.TestCase):
    def test_performance_harness_refuses_to_actuate_without_explicit_flag(self):
        harness = load_harness()

        result = harness.parse_args(["--command-url", "http://127.0.0.1:8765/commands"])

        self.assertFalse(result.actuate)

    def test_performance_harness_outputs_json_summary(self):
        harness = load_harness()

        summary = harness.build_summary(samples=[{"latency_ms": 12.5}], skipped_pi=True)

        self.assertEqual(summary["pi_ssh_tests"], "skipped_until_tailscale_or_home_wifi")

    def test_host_metrics_include_bounded_network_interface_counters(self):
        harness = load_harness()
        proc_values = {
            "/proc/loadavg": None,
            "/proc/meminfo": None,
            "/proc/net/dev": (
                "Inter-| Receive | Transmit\n"
                " face |bytes packets errs drop fifo frame compressed multicast|"
                "bytes packets errs drop fifo colls carrier compressed\n"
                "  eth0: 123 4 0 0 0 0 0 0 567 8 0 0 0 0 0 0\n"
            ),
        }

        with mock.patch.object(
            harness, "_read_proc_value",
            side_effect=lambda path, **kwargs: proc_values[path],
        ) as read_proc:
            metrics = harness.collect_host_metrics()

        self.assertEqual(metrics["network"]["eth0"], {
            "receive_bytes": 123,
            "receive_packets": 4,
            "transmit_bytes": 567,
            "transmit_packets": 8,
        })
        self.assertIn(
            mock.call("/proc/net/dev", max_bytes=harness.MAX_PROC_NET_DEV_BYTES),
            read_proc.call_args_list,
        )

    def test_default_execution_issues_only_get_probes(self):
        harness = load_harness()
        calls = []

        def measure_request(url, **kwargs):
            calls.append((url, kwargs.get("method", "GET")))
            return {"url": url, "method": kwargs.get("method", "GET"), "latency_ms": 1}

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            harness, "measure_request", side_effect=measure_request
        ), mock.patch.object(harness, "collect_host_metrics", return_value={}):
            harness.main(["--output", str(Path(directory) / "summary.json")])

        self.assertEqual([method for _, method in calls], ["GET", "GET"])

    def test_skip_network_execution_issues_no_requests(self):
        harness = load_harness()

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            harness, "measure_request"
        ) as measure_request, mock.patch.object(
            harness, "collect_host_metrics", return_value={}
        ):
            harness.main(["--skip-network", "--output", str(Path(directory) / "summary.json")])

        measure_request.assert_not_called()

    def test_explicit_actuation_execution_issues_the_only_post(self):
        harness = load_harness()
        calls = []

        def measure_request(url, **kwargs):
            calls.append((url, kwargs.get("method", "GET")))
            return {"url": url, "method": kwargs.get("method", "GET"), "latency_ms": 1}

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            harness, "measure_request", side_effect=measure_request
        ), mock.patch.object(harness, "collect_host_metrics", return_value={}):
            harness.main(["--actuate", "--output", str(Path(directory) / "summary.json")])

        self.assertEqual([method for _, method in calls], ["GET", "GET", "POST"])


if __name__ == "__main__":
    unittest.main()
