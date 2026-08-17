import importlib.util
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
