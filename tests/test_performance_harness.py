import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY_ROOT = Path(__file__).parents[1]
HARNESS_PATH = REPOSITORY_ROOT / "scripts" / "pi-cloudflare-performance-harness.py"


def load_harness():
    spec = importlib.util.spec_from_file_location("pi_cloudflare_performance_harness", HARNESS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PerformanceHarnessTests(unittest.TestCase):
    def test_performance_harness_rejects_removed_actuation_arguments(self):
        harness = load_harness()

        for arguments in (
            ["--actuate"],
            ["--controller-id", "primary"],
            ["--command-url", "http://127.0.0.1:8765/commands"],
        ):
            with self.subTest(arguments=arguments), mock.patch("sys.stderr"):
                with self.assertRaises(SystemExit):
                    harness.parse_args(arguments)

    def test_removed_actuation_flag_cannot_reach_request_layer(self):
        harness = load_harness()

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            harness, "measure_request", return_value={"latency_ms": 1}
        ) as measure_request, mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                harness.main([
                    "--actuate", "--output", str(Path(directory) / "summary.json")
                ])

        measure_request.assert_not_called()

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

    def test_default_execution_probes_only_read_only_media_endpoints(self):
        harness = load_harness()
        calls = []

        def measure_request(url, **kwargs):
            calls.append(url)
            return {"url": url, "method": "GET", "latency_ms": 1}

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            harness, "measure_request", side_effect=measure_request
        ), mock.patch.object(harness, "collect_host_metrics", return_value={}):
            summary = harness.main(["--output", str(Path(directory) / "summary.json")])

        self.assertEqual(calls, [
            "http://127.0.0.1:9997/v3/paths/list",
            "http://127.0.0.1:9998/metrics",
        ])
        self.assertEqual("passive_endpoint_probe", summary["run_mode"])
        self.assertNotIn("actuation_requested", summary)

    def test_harness_artifacts_do_not_reference_the_command_endpoint(self):
        plan = (
            REPOSITORY_ROOT
            / "docs/superpowers/plans/2026-08-17-cloudflare-replatform.md"
        ).read_text(encoding="utf-8")
        harness_task = plan.split("### Task 7:", 1)[1].split("\n---", 1)[0]
        artifacts = {
            "harness": HARNESS_PATH.read_text(encoding="utf-8"),
            "performance docs": (
                REPOSITORY_ROOT / "docs/pi-cloudflare-performance.md"
            ).read_text(encoding="utf-8"),
            "camera docs": (
                REPOSITORY_ROOT / "docs/reolink-rlc-811a.md"
            ).read_text(encoding="utf-8"),
            "media config": (
                REPOSITORY_ROOT / "deployment/media/mediamtx.yml"
            ).read_text(encoding="utf-8"),
            "historical harness plan": harness_task,
        }

        for name, content in artifacts.items():
            with self.subTest(artifact=name):
                self.assertNotIn("/commands", content)
                self.assertNotIn("--command-url", content)

    def test_skip_network_execution_issues_no_requests(self):
        harness = load_harness()

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            harness, "measure_request"
        ) as measure_request, mock.patch.object(
            harness, "collect_host_metrics", return_value={}
        ):
            summary = harness.main([
                "--skip-network", "--output", str(Path(directory) / "summary.json")
            ])

        measure_request.assert_not_called()
        self.assertEqual("host_metrics_only", summary["run_mode"])
        self.assertNotIn("actuation_requested", summary)


if __name__ == "__main__":
    unittest.main()
