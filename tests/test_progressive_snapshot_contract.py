import importlib
import tempfile
import unittest
from pathlib import Path


class ProgressiveSnapshotContractTests(unittest.TestCase):
    def test_complete_private_camera_configuration_enables_two_frame_sampling(self):
        try:
            module = importlib.import_module("gate_controller.reolink_snapshots")
        except ModuleNotFoundError:
            self.fail("progressive Reolink snapshot support is missing")

        with tempfile.TemporaryDirectory() as directory:
            config = module.load_reolink_snapshot_config(
                {
                    "GATE_REOLINK_SNAPSHOT_BASE_URL": "https://192.168.10.20",
                    "GATE_REOLINK_SNAPSHOT_USERNAME": "viewer",
                    "GATE_REOLINK_SNAPSHOT_PASSWORD": "secret",
                },
                Path(directory),
                max_candidate_bytes=8 * 1024 * 1024,
            )

        self.assertTrue(config.enabled)
        self.assertEqual(2, config.candidate_count)
        self.assertEqual(2.25, config.timeout_seconds)


if __name__ == "__main__":
    unittest.main()
