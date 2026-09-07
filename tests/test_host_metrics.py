import ast
import os
import tempfile
import unittest
from pathlib import Path

from gate_controller import host_metrics
from gate_controller.host_metrics import (
    MAX_PROC_BYTES, decode_throttled, read_host_metrics, read_proc_value,
    read_throttled_flags,
)


TICKS = os.sysconf("SC_CLK_TCK")


def executable_source(path: Path) -> str:
    """The module's code with docstrings and comments removed.

    A prose warning that a module must never import ffmpeg is not the same
    thing as the module importing it, so the contract is asserted against the
    code only.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            docstring = ast.get_docstring(node, clean=False)
            if docstring:
                source = source.replace(docstring, "")
    return "\n".join(line.split("#", 1)[0] for line in source.splitlines())


def write_proc_tree(root: Path, *, uptime: float = 8400.0,
                    started_ticks: int = 60 * TICKS, oom_kills: int = 0,
                    load: str = "0.31 0.42 0.55") -> None:
    (root / "self").mkdir(parents=True, exist_ok=True)
    (root / "loadavg").write_text(f"{load} 1/412 9871\n", encoding="utf-8")
    (root / "meminfo").write_text(
        "MemTotal:        8244188 kB\n"
        "MemFree:          412000 kB\n"
        "MemAvailable:    6120344 kB\n"
        "SwapTotal:        204796 kB\n"
        "SwapFree:         204796 kB\n",
        encoding="utf-8",
    )
    (root / "vmstat").write_text(
        f"nr_free_pages 103000\npgfault 812371\noom_kill {oom_kills}\n",
        encoding="utf-8",
    )
    (root / "uptime").write_text(f"{uptime} 33021.55\n", encoding="utf-8")
    # Fields 4-21, then field 22 (starttime): the twentieth value after the
    # parenthesised comm, which is where the kernel actually puts it.
    trailing = " ".join(["0"] * 18) + f" {started_ticks}"
    (root / "self" / "stat").write_text(
        f"1841 (python3 monitor) S {trailing}\n", encoding="utf-8",
    )
    (root / "diskstats").write_text(
        " 179       0 mmcblk0 51234 1204 2884512 41234 88231 40122 19284416 "
        "998123 0 88123 1039357\n"
        " 179       1 mmcblk0p1 100 0 800 12 30 4 240 8 0 20 20\n",
        encoding="utf-8",
    )


class HostMetricsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        write_proc_tree(self.proc)
        self.thermal = self.root / "temp"
        self.thermal.write_text("74200\n", encoding="utf-8")

    def read(self, **overrides):
        arguments = {
            "proc_root": self.proc,
            "thermal_zone": self.thermal,
            "mount_point": str(self.root),
        }
        arguments.update(overrides)
        return read_host_metrics(**arguments)

    def test_every_measurable_host_field_reaches_the_heartbeat(self):
        metrics = self.read()

        self.assertEqual(74.2, metrics["soc_temp_c"])
        self.assertEqual(0.31, metrics["load_1m"])
        self.assertEqual(0.42, metrics["load_5m"])
        self.assertEqual(0.55, metrics["load_15m"])
        self.assertEqual(8244188, metrics["mem_total_kib"])
        self.assertEqual(6120344, metrics["mem_available_kib"])
        self.assertEqual(204796, metrics["swap_free_kib"])
        self.assertEqual(0, metrics["oom_kill_total"])
        self.assertEqual(8400.0, metrics["uptime_seconds"])
        self.assertEqual(8340.0, metrics["process_uptime_seconds"])
        self.assertEqual(19284416, metrics["disk_sectors_written"])
        self.assertGreater(metrics["disk_total_bytes"], 0)
        self.assertGreaterEqual(metrics["disk_free_bytes"], 0)

    def test_the_oom_counter_that_would_have_named_the_2026_09_07_failure(self):
        write_proc_tree(self.proc, oom_kills=3)

        self.assertEqual(3, self.read()["oom_kill_total"])

    def test_a_service_restart_shows_as_a_shorter_process_uptime_than_the_board(self):
        write_proc_tree(self.proc, uptime=90000.0, started_ticks=89880 * TICKS)

        metrics = self.read()

        self.assertEqual(90000.0, metrics["uptime_seconds"])
        self.assertEqual(120.0, metrics["process_uptime_seconds"])

    def test_an_unreadable_source_omits_its_field_instead_of_looking_healthy(self):
        (self.proc / "meminfo").unlink()
        self.thermal.unlink()

        metrics = self.read()

        self.assertNotIn("soc_temp_c", metrics)
        self.assertNotIn("mem_available_kib", metrics)
        self.assertIn("load_1m", metrics)

    def test_reading_an_entirely_absent_proc_returns_an_empty_block(self):
        metrics = self.read(proc_root=self.root / "missing", thermal_zone=self.root / "missing")

        self.assertNotIn("uptime_seconds", metrics)
        self.assertNotIn("oom_kill_total", metrics)

    def test_a_pathological_proc_file_is_refused_rather_than_read_into_memory(self):
        oversized = self.proc / "meminfo"
        oversized.write_text("x" * (MAX_PROC_BYTES + 1), encoding="utf-8")

        self.assertIsNone(read_proc_value(oversized))
        self.assertNotIn("mem_available_kib", self.read())

    def test_no_host_field_carries_a_path_a_device_name_or_an_address(self):
        rendered = str(self.read(throttled={"raw": "0x0", "under_voltage": False}))

        self.assertNotIn(str(self.proc), rendered)
        self.assertNotIn("mmcblk0", rendered)
        self.assertNotIn("/", rendered)
        self.assertNotRegex(rendered, r"\b\d{1,3}(\.\d{1,3}){3}\b")

    def test_the_firmware_throttle_word_is_merged_in_when_the_probe_supplies_it(self):
        metrics = self.read(throttled={"raw": "0xe0006", "currently_throttled": True})

        self.assertEqual("0xe0006", metrics["throttled"]["raw"])
        self.assertTrue(metrics["throttled"]["currently_throttled"])
        self.assertNotIn("throttled", self.read())


class ThrottleFlagTests(unittest.TestCase):
    def test_the_measured_2026_09_05_word_decodes_to_capped_and_throttled(self):
        flags = decode_throttled("throttled=0xe0006")

        self.assertEqual("0xe0006", flags["raw"])
        self.assertTrue(flags["arm_capped"])
        self.assertTrue(flags["currently_throttled"])
        self.assertFalse(flags["under_voltage"])

    def test_a_healthy_word_decodes_to_every_flag_clear(self):
        flags = decode_throttled("0x0")

        self.assertEqual("0x0", flags["raw"])
        self.assertFalse(any(flags[key] for key in flags if key != "raw"))

    def test_malformed_or_missing_firmware_output_is_not_a_failure(self):
        for value in (None, "", "throttled=", "throttled=not-hex", "x" * 64):
            with self.subTest(value=value):
                self.assertIsNone(decode_throttled(value))

    def test_sysfs_is_preferred_so_the_subprocess_is_never_spawned(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        firmware = Path(directory.name) / "get_throttled"
        firmware.write_text("0x50005\n", encoding="utf-8")
        spawned = []

        flags = read_throttled_flags(
            run_command=lambda command: spawned.append(command),
            firmware_path=firmware,
        )

        self.assertEqual("0x50005", flags["raw"])
        self.assertTrue(flags["under_voltage"])
        self.assertEqual([], spawned)

    def test_importing_the_module_can_never_spawn_a_process_on_its_own(self):
        missing = Path(tempfile.gettempdir()) / "gate-controller-absent-firmware"

        self.assertIsNone(read_throttled_flags(firmware_path=missing))

    def test_a_failing_child_runner_reports_nothing_rather_than_raising(self):
        def explode(command):
            raise OSError("vcgencmd unavailable")

        self.assertIsNone(read_throttled_flags(
            run_command=explode,
            firmware_path=Path(tempfile.gettempdir()) / "gate-controller-absent-firmware",
        ))


class HostMetricsForbiddenDependencyTests(unittest.TestCase):
    """The host path must stay a pure /proc read: no decode, no model, no shell."""

    def test_the_module_never_reaches_for_a_decoder_a_model_or_the_journal(self):
        body = executable_source(Path(host_metrics.__file__))

        for forbidden in ("ffmpeg", "numpy", "onnxruntime", "journalctl", "cv2", "PIL"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, body)


if __name__ == "__main__":
    unittest.main()
