import ast
import os
import resource
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event, Thread

from gate_controller import net_probe
from gate_controller.net_probe import (
    CHILD_ADDRESS_SPACE_BYTES, CHILD_TIMEOUT_SECONDS, DEFAULT_MAX_LOAD,
    DEFAULT_MAX_TEMP_C, DEFAULT_MIN_AVAILABLE_BYTES, MAX_CHILD_OUTPUT_BYTES,
    NetProbeConfig, NetProbeWorker, _limit_address_space, default_gateway,
    default_interface, interface_counters, load_net_probe_config, parse_ping,
)


HEALTHY_PING = (
    "--- gateway ping statistics ---\n"
    "5 packets transmitted, 5 received, 0% packet loss, time 803ms\n"
    "rtt min/avg/max/mdev = 2.914/3.412/4.881/0.702 ms\n"
)
DEGRADED_PING = (
    "--- gateway ping statistics ---\n"
    "5 packets transmitted, 2 received, 60% packet loss, time 812ms\n"
    "rtt min/avg/max/mdev = 118.221/213.884/261.442/48.113 ms\n"
)
LOST_PING = (
    "--- gateway ping statistics ---\n"
    "5 packets transmitted, 0 received, 100% packet loss, time 4082ms\n"
)
ROUTE = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "eth0\t00000000\t0100A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
    "eth0\t0000A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n"
)


def executable_source(path: Path) -> str:
    """The module's code with docstrings and comments removed."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            docstring = ast.get_docstring(node, clean=False)
            if docstring:
                source = source.replace(docstring, "")
    return "\n".join(line.split("#", 1)[0] for line in source.splitlines())


class FakeProcess:
    """A child whose stdout is a real pipe, so select() and os.read() run."""

    def __init__(self, output: bytes = b"", *, hang: bool = False):
        read_descriptor, self._write_descriptor = os.pipe()
        self.stdout = os.fdopen(read_descriptor, "rb", 0)
        self.returncode = None
        self.killed = False
        if not hang:
            os.write(self._write_descriptor, output)
            self._close_write()

    def _close_write(self):
        if self._write_descriptor is not None:
            try:
                os.close(self._write_descriptor)
            except OSError:
                pass
            self._write_descriptor = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._close_write()

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class FakePopen:
    """Records every spawn and hands back a canned child."""

    def __init__(self, *outputs: bytes, hang: bool = False):
        self.outputs = list(outputs)
        self.hang = hang
        self.calls = []
        self.processes = []
        self.live = 0
        self.max_live = 0

    def __call__(self, command, **kwargs):
        self.calls.append((tuple(command), kwargs))
        self.live += 1
        self.max_live = max(self.max_live, self.live)
        output = self.outputs.pop(0) if self.outputs else b""
        process = FakeProcess(output, hang=self.hang)
        original_kill = process.kill

        def kill():
            self.live -= 1
            original_kill()

        process.kill = kill
        self.processes.append(process)
        return process


def probe(*, popen=None, metrics=None, proc_root=None, clock=None,
          tls_connect=None, child_timeout=CHILD_TIMEOUT_SECONDS,
          config=None) -> NetProbeWorker:
    return NetProbeWorker(
        config or NetProbeConfig(enabled=True),
        popen=popen or FakePopen(HEALTHY_PING.encode()),
        clock=clock or time.monotonic,
        host_metrics=metrics or (lambda **_: {"soc_temp_c": 62.0, "load_1m": 0.4,
                                              "mem_available_kib": 6_000_000}),
        proc_root=proc_root or Path("/nonexistent-proc"),
        tls_connect=tls_connect or (lambda host: 220.0),
        child_timeout=child_timeout,
    )


class GovernorTests(unittest.TestCase):
    """The governor is what makes running this on a 6 C headroom board safe."""

    def run_with(self, metrics):
        popen = FakePopen(HEALTHY_PING.encode())
        worker = probe(popen=popen, metrics=lambda **_: metrics)
        worker.run_once()
        return worker.status(), popen

    def test_a_cycle_is_skipped_and_named_at_82_degrees(self):
        status, popen = self.run_with({"soc_temp_c": 82.0, "load_1m": 0.2,
                                       "mem_available_kib": 6_000_000})

        self.assertEqual("hot", status["skipped_reason"])
        self.assertFalse(status["probed"])
        self.assertEqual([], popen.calls)

    def test_the_ceiling_is_inclusive_so_exactly_80_degrees_still_skips(self):
        status, popen = self.run_with({"soc_temp_c": DEFAULT_MAX_TEMP_C,
                                       "load_1m": 0.2, "mem_available_kib": 6_000_000})

        self.assertEqual("hot", status["skipped_reason"])
        self.assertEqual([], popen.calls)

    def test_a_loaded_board_skips_rather_than_competing_with_the_relay_path(self):
        status, popen = self.run_with({"soc_temp_c": 60.0, "load_1m": DEFAULT_MAX_LOAD,
                                       "mem_available_kib": 6_000_000})

        self.assertEqual("loaded", status["skipped_reason"])
        self.assertEqual([], popen.calls)

    def test_under_300_mb_available_skips_rather_than_courting_the_oom_killer(self):
        available_kib = (DEFAULT_MIN_AVAILABLE_BYTES // 1024) - 1
        status, popen = self.run_with({"soc_temp_c": 60.0, "load_1m": 0.2,
                                       "mem_available_kib": available_kib})

        self.assertEqual("low_memory", status["skipped_reason"])
        self.assertEqual([], popen.calls)

    def test_a_healthy_board_runs_the_cycle(self):
        status, popen = self.run_with({"soc_temp_c": 71.4, "load_1m": 0.6,
                                       "mem_available_kib": 6_000_000})

        self.assertIsNone(status["skipped_reason"])
        self.assertTrue(status["probed"])

    def test_an_unreadable_governor_metric_does_not_disable_the_probe_forever(self):
        status, _ = self.run_with({})

        self.assertIsNone(status["skipped_reason"])

    def test_a_governor_that_raises_is_swallowed_like_every_other_failure(self):
        def explode(**_):
            raise OSError("thermal zone moved")

        worker = probe(metrics=explode)

        self.assertTrue(worker.run_once())


class ChildProcessBoundTests(unittest.TestCase):
    def test_the_single_child_is_address_space_limited_and_silenced(self):
        popen = FakePopen(HEALTHY_PING.encode())
        worker = probe(popen=popen, proc_root=self.route_root())

        worker.run_once()

        command, kwargs = popen.calls[0]
        self.assertEqual("ping", command[0])
        self.assertIn("-c", command)
        self.assertEqual("5", command[command.index("-c") + 1])
        self.assertIs(_limit_address_space, kwargs["preexec_fn"])
        self.assertEqual(subprocess.DEVNULL, kwargs["stderr"])
        self.assertEqual(subprocess.PIPE, kwargs["stdout"])
        self.assertEqual(subprocess.DEVNULL, kwargs["stdin"])
        self.assertTrue(kwargs["close_fds"])

    def test_the_address_space_ceiling_is_64_mib(self):
        self.assertEqual(64 * 1024 * 1024, CHILD_ADDRESS_SPACE_BYTES)

    def test_the_kill_timeout_is_three_seconds(self):
        self.assertEqual(3.0, CHILD_TIMEOUT_SECONDS)
        self.assertEqual(3.0, probe().child_timeout)

    def test_a_child_that_never_exits_is_killed_at_the_deadline(self):
        popen = FakePopen(hang=True)
        worker = probe(popen=popen, proc_root=self.route_root(), child_timeout=0.05)

        started = time.monotonic()
        worker.run_once()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.5)
        self.assertTrue(popen.processes[0].killed)
        self.assertEqual(1.0, worker.status()["router"]["loss"])

    def test_an_oversized_child_stdout_is_refused_rather_than_held(self):
        popen = FakePopen(b"x" * (MAX_CHILD_OUTPUT_BYTES + 1))
        worker = probe(popen=popen, proc_root=self.route_root())

        worker.run_once()

        self.assertEqual(1.0, worker.status()["router"]["loss"])
        self.assertTrue(popen.processes[0].killed)

    def test_a_spawn_failure_is_a_measurement_not_an_exception(self):
        def refuse(command, **kwargs):
            raise OSError("fork failed")

        worker = probe(popen=refuse, proc_root=self.route_root())

        self.assertTrue(worker.run_once())
        self.assertEqual(1.0, worker.status()["router"]["loss"])

    def test_close_kills_the_live_child_and_refuses_to_start_another(self):
        popen = FakePopen(HEALTHY_PING.encode())
        worker = probe(popen=popen, proc_root=self.route_root())
        worker.close()

        self.assertFalse(worker.run_once())
        self.assertEqual([], popen.calls)

    def route_root(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "net").mkdir()
        (root / "net" / "route").write_text(ROUTE, encoding="utf-8")
        return root


class SingleFlightTests(unittest.TestCase):
    def test_at_most_one_child_is_ever_alive_across_concurrent_cycles(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "net").mkdir()
        (root / "net" / "route").write_text(ROUTE, encoding="utf-8")
        popen = FakePopen(*[HEALTHY_PING.encode()] * 40)
        worker = probe(popen=popen, proc_root=root)

        threads = [Thread(target=worker.run_once) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        pings = [command for command, _ in popen.calls if command[0] == "ping"]
        self.assertEqual(1, popen.max_live)
        # Contended cycles are dropped, never queued behind the one in flight.
        self.assertLessEqual(len(pings), 8)


class MeasurementTests(unittest.TestCase):
    def test_the_healthy_powerline_baseline_is_reported_as_measured(self):
        parsed = parse_ping(HEALTHY_PING)

        self.assertEqual(0.0, parsed["loss"])
        self.assertEqual(3.41, parsed["rtt_ms"])

    def test_the_measured_2026_09_06_degradation_is_visible_as_loss_and_rtt(self):
        parsed = parse_ping(DEGRADED_PING)

        self.assertEqual(0.6, parsed["loss"])
        self.assertEqual(213.88, parsed["rtt_ms"])

    def test_total_loss_is_a_measurement_not_an_exception_or_a_hang(self):
        parsed = parse_ping(LOST_PING)

        self.assertEqual(1.0, parsed["loss"])
        self.assertIsNone(parsed["rtt_ms"])

    def test_unparseable_ping_output_reports_nothing_rather_than_guessing(self):
        parsed = parse_ping("ping: unknown host\n")

        self.assertIsNone(parsed["loss"])
        self.assertIsNone(parsed["rtt_ms"])

    def test_the_default_gateway_is_read_from_the_route_table_never_hard_coded(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "net").mkdir()
        (root / "net" / "route").write_text(ROUTE, encoding="utf-8")

        self.assertEqual("192.168.0.1", default_gateway(root))
        self.assertEqual("eth0", default_interface(root))
        self.assertNotIn("192.168.0.1", executable_source(Path(net_probe.__file__)))

    def test_an_unreadable_route_table_yields_no_gateway_and_no_exception(self):
        self.assertIsNone(default_gateway(Path("/nonexistent-proc")))
        self.assertIsNone(default_interface(Path("/nonexistent-proc")))

    def test_uplink_bytes_come_from_the_proc_counter_delta_not_a_speed_test(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "net").mkdir()
        (root / "net" / "route").write_text(ROUTE, encoding="utf-8")
        counters = {"value": 0}

        def write_counters(received, transmitted):
            (root / "net" / "dev").write_text(
                "Inter-|   Receive                        |  Transmit\n"
                " face |bytes packets errs drop fifo frame compressed multicast|"
                "bytes packets errs drop fifo colls carrier compressed\n"
                f"  eth0: {received} 10 0 0 0 0 0 0 {transmitted} 12 0 0 0 0 0 0\n",
                encoding="utf-8",
            )

        write_counters(1_000, 2_000)
        clock = lambda: counters["value"]
        worker = probe(popen=FakePopen(*[HEALTHY_PING.encode()] * 4),
                       proc_root=root, clock=clock)
        worker.run_once()
        self.assertNotIn("uplink", worker.status())

        counters["value"] = 60.0
        write_counters(61_000, 122_000)
        worker.run_once()

        uplink = worker.status()["uplink"]
        self.assertEqual("eth0", uplink["interface"])
        self.assertEqual(1000.0, uplink["receive_bytes_per_s"])
        self.assertEqual(2000.0, uplink["transmit_bytes_per_s"])

    def test_a_counter_reset_reports_nothing_rather_than_a_negative_rate(self):
        self.assertIsNone(interface_counters(Path("/nonexistent-proc"), "eth0"))
        self.assertIsNone(interface_counters(Path("/nonexistent-proc"), None))

    def test_the_tls_handshake_is_measured_on_its_own_slower_cadence(self):
        handshakes = []
        counters = {"value": 0.0}
        worker = probe(clock=lambda: counters["value"],
                       tls_connect=lambda host: handshakes.append(host) or 0.22)

        worker.run_once()
        counters["value"] = 60.0
        worker.run_once()
        self.assertEqual(1, len(handshakes))

        counters["value"] = 400.0
        worker.run_once()
        self.assertEqual(2, len(handshakes))
        self.assertTrue(worker.status()["tls"]["ok"])

    def test_a_refused_handshake_is_reported_rather_than_raised(self):
        def refuse(host):
            raise OSError("connection refused")

        worker = probe(tls_connect=refuse)
        worker.run_once()

        self.assertFalse(worker.status()["tls"]["ok"])
        self.assertIsNone(worker.status()["tls"]["handshake_ms"])

    def test_the_reported_status_carries_no_address_path_or_credential(self):
        worker = probe(proc_root=self.route_root())
        worker.run_once()

        rendered = str(worker.status())
        self.assertNotRegex(rendered, r"\b\d{1,3}(\.\d{1,3}){3}\b")
        self.assertNotIn("/", rendered)

    def test_only_a_completed_cycle_is_published(self):
        worker = probe()

        self.assertFalse(worker.status()["probed"])
        self.assertNotIn("router", worker.status())

    def route_root(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "net").mkdir()
        (root / "net" / "route").write_text(ROUTE, encoding="utf-8")
        return root


class ConfigurationTests(unittest.TestCase):
    def test_the_defaults_are_the_measured_thermal_and_load_ceilings(self):
        config = load_net_probe_config({})

        self.assertTrue(config.enabled)
        self.assertEqual(60.0, config.interval_seconds)
        self.assertEqual(300.0, config.tls_interval_seconds)
        self.assertEqual(80.0, config.max_temp_c)
        self.assertEqual(3.0, config.max_load)
        self.assertEqual(300 * 1024 * 1024, config.min_available_bytes)

    def test_the_probe_can_be_switched_off_entirely(self):
        self.assertFalse(load_net_probe_config({"GATE_NET_PROBE_ENABLED": "false"}).enabled)

    def test_a_disabled_probe_never_runs_a_cycle(self):
        popen = FakePopen(HEALTHY_PING.encode())
        worker = NetProbeWorker(NetProbeConfig(enabled=False), popen=popen)

        self.assertFalse(worker.run_once())
        self.assertEqual([], popen.calls)

    def test_settings_outside_the_safe_envelope_are_refused_at_start(self):
        for environment, message in (
            ({"GATE_NET_PROBE_ENABLED": "maybe"}, "true or false"),
            ({"GATE_NET_PROBE_INTERVAL_SECONDS": "1"}, "between"),
            ({"GATE_NET_PROBE_INTERVAL_SECONDS": "not-a-number"}, "must be a number"),
            ({"GATE_NET_PROBE_TLS_INTERVAL_SECONDS": "5"}, "between"),
            ({"GATE_NET_PROBE_MAX_TEMP_C": "95"}, "between"),
            ({"GATE_NET_PROBE_MAX_LOAD": "0"}, "between"),
            ({"GATE_NET_PROBE_TLS_HOST": ""}, "hostname"),
        ):
            with self.subTest(environment=environment):
                with self.assertRaisesRegex(ValueError, message):
                    load_net_probe_config(environment)

    def test_run_forever_stops_promptly_when_the_controller_shuts_down(self):
        worker = probe(config=NetProbeConfig(enabled=True, interval_seconds=30.0))
        stop_event = Event()
        thread = Thread(target=worker.run_forever, args=(stop_event,))

        thread.start()
        stop_event.set()
        thread.join(timeout=5)

        self.assertFalse(thread.is_alive())


class ResourceCeilingTests(unittest.TestCase):
    """The ceilings the design committed to, asserted rather than asserted-to."""

    def measure(self, cycles: int):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "net").mkdir()
        (root / "net" / "route").write_text(ROUTE, encoding="utf-8")
        (root / "net" / "dev").write_text(
            "Inter-|   Receive                        |  Transmit\n"
            " face |bytes packets errs drop fifo frame compressed multicast|"
            "bytes packets errs drop fifo colls carrier compressed\n"
            "  eth0: 1000 10 0 0 0 0 0 0 2000 12 0 0 0 0 0 0\n",
            encoding="utf-8",
        )
        worker = probe(popen=FakePopen(*[HEALTHY_PING.encode()] * (cycles + 4)),
                       proc_root=root)
        worker.run_once()  # warm the caches the steady state would already hold

        before_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        before_cpu = time.process_time()
        before_wall = time.monotonic()
        for _ in range(cycles):
            worker.run_once()
        wall = time.monotonic() - before_wall
        cpu = time.process_time() - before_cpu
        after_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # ru_maxrss is bytes on Darwin and kibibytes on Linux.
        scale = 1 if os.uname().sysname == "Darwin" else 1024
        return (after_rss - before_rss) * scale, cpu, wall

    def test_steady_state_growth_stays_under_five_megabytes(self):
        rss_delta, _, _ = self.measure(200)

        self.assertLess(rss_delta, 5 * 1024 * 1024, f"probe grew by {rss_delta} bytes")

    def test_cpu_stays_under_half_a_percent_of_one_core_at_the_60_second_poll(self):
        cycles = 200
        _, cpu, _ = self.measure(cycles)
        share_of_one_core = (cpu / cycles) / NetProbeConfig().interval_seconds

        self.assertLess(share_of_one_core, 0.005,
                        f"probe used {share_of_one_core * 100:.4f}% of one core")

    def test_a_cycle_finishes_well_inside_the_wall_time_budget(self):
        cycles = 50
        _, _, wall = self.measure(cycles)

        self.assertLess(wall / cycles, 1.5)


class ForbiddenDependencyTests(unittest.TestCase):
    """A sub-agent's 4K decode OOM-killed this board. Not from here, ever."""

    def test_the_probe_never_reaches_for_a_decoder_a_model_or_a_speed_test(self):
        body = executable_source(Path(net_probe.__file__))

        for forbidden in ("ffmpeg", "numpy", "onnxruntime", "journalctl",
                          "cv2", "PIL", "iperf", "speedtest"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, body)

    def test_the_probe_spawns_nothing_but_ping_and_vcgencmd(self):
        body = executable_source(Path(net_probe.__file__))
        host_body = executable_source(
            Path(net_probe.__file__).with_name("host_metrics.py")
        )

        self.assertIn('"ping"', body)
        self.assertIn('"vcgencmd"', host_body)
        self.assertNotIn("shell=True", body)
        self.assertNotIn("os.system", body)


if __name__ == "__main__":
    unittest.main()
