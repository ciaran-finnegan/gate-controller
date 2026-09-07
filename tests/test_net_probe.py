import ast
import logging
import os
import resource
import ssl
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event, Thread

from gate_controller import net_probe
from gate_controller.net_probe import (
    CHILD_ADDRESS_SPACE_BYTES, CHILD_TIMEOUT_SECONDS, CRITICAL_MIN_AVAILABLE_BYTES,
    CRITICAL_TEMP_C, DEFAULT_MAX_LOAD, DEFAULT_MAX_TEMP_C,
    DEFAULT_MIN_AVAILABLE_BYTES, DEFAULT_PING_COUNT, MAX_CHILD_OUTPUT_BYTES,
    MAX_CHILD_TIMEOUT_SECONDS, NetProbeConfig, NetProbeWorker,
    _limit_address_space, default_gateway, default_interface, interface_counters,
    journal_line, link_speed_mbps, load_net_probe_config, parse_ping,
    ping_command, ping_timeout_seconds, tls_context,
)


def ping_output(target: str, sent: int, samples, *, mdev: float = 0.0) -> str:
    """Real ``ping`` output: one line per reply, then the summary block."""
    lines = [f"PING {target} ({target}) 56(84) bytes of data."]
    for sequence, sample in enumerate(samples, start=1):
        lines.append(
            f"64 bytes from {target}: icmp_seq={sequence} ttl=64 time={sample} ms"
        )
    received = len(samples)
    loss = round(100.0 * (sent - received) / sent, 0)
    lines.append("")
    lines.append(f"--- {target} ping statistics ---")
    lines.append(
        f"{sent} packets transmitted, {received} received, "
        f"{loss:g}% packet loss, time {sent * 200}ms"
    )
    if samples:
        ordered = sorted(samples)
        mean = sum(ordered) / len(ordered)
        lines.append(
            "rtt min/avg/max/mdev = "
            f"{ordered[0]:.3f}/{mean:.3f}/{ordered[-1]:.3f}/{mdev:.3f} ms"
        )
    return "\n".join(lines) + "\n"


# The 2026-09-07 measurement: the camera never leaves the gate switch.
CAMERA_SAMPLES = [0.312, 0.401, 0.355, 0.821, 0.334, 0.377, 0.362, 0.398, 0.344, 0.389]
CAMERA_PING = ping_output("192.168.0.54", 10, CAMERA_SAMPLES)
# The same measurement to the router, which crosses the powerline bridge.
ROUTER_SAMPLES = [70.1, 154.2, 88.7, 245.0, 131.4, 167.9, 203.6, 96.3, 178.5]
ROUTER_PING = ping_output("192.168.0.1", 10, ROUTER_SAMPLES)
HEALTHY_PING = ping_output("192.168.0.1", 5, [2.914, 3.102, 3.412, 3.731, 4.881])
QUIET_PING = (
    "--- gateway ping statistics ---\n"
    "5 packets transmitted, 5 received, 0% packet loss, time 803ms\n"
    "rtt min/avg/max/mdev = 2.914/3.412/4.881/0.702 ms\n"
)
LOST_PING = (
    "PING 192.168.0.1 (192.168.0.1) 56(84) bytes of data.\n"
    "\n--- 192.168.0.1 ping statistics ---\n"
    "5 packets transmitted, 0 received, 100% packet loss, time 4082ms\n"
)
ROUTE = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "eth0\t00000000\t0100A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
    "eth0\t0000A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n"
)
HEALTHY_INTERNET = {"dns_ms": 12.4, "connect_ms": 38.1, "tls_ms": 96.2,
                    "total_ms": 146.7}
HEALTHY_METRICS = {"soc_temp_c": 68.0, "load_1m": 0.5, "mem_available_kib": 6_000_000}


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

    def commands(self, name: str) -> list:
        return [command for command, _ in self.calls if command[0] == name]


def route_root(test) -> Path:
    """A /proc root carrying the default route and the interface counters."""
    directory = tempfile.TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    root = Path(directory.name)
    (root / "net").mkdir()
    (root / "net" / "route").write_text(ROUTE, encoding="utf-8")
    return root


def write_counters(root: Path, values) -> None:
    (root / "net" / "dev").write_text(
        "Inter-|   Receive                        |  Transmit\n"
        " face |bytes packets errs drop fifo frame compressed multicast|"
        "bytes packets errs drop fifo colls carrier compressed\n"
        "  eth0: " + " ".join(str(value) for value in values) + "\n",
        encoding="utf-8",
    )


def counter_row(*, rx_bytes=0, rx_packets=0, rx_errors=0, rx_dropped=0,
                tx_bytes=0, tx_packets=0, tx_errors=0) -> list:
    return [rx_bytes, rx_packets, rx_errors, rx_dropped, 0, 0, 0, 0,
            tx_bytes, tx_packets, tx_errors, 0, 0, 0, 0, 0]


def probe(*, popen=None, metrics=None, proc_root=None, clock=None,
          internet_connect=None, child_timeout=CHILD_TIMEOUT_SECONDS,
          ping_timeout=None, sys_class_net=None, config=None,
          lan_host="") -> NetProbeWorker:
    return NetProbeWorker(
        config or NetProbeConfig(enabled=True, lan_host=lan_host),
        popen=popen or FakePopen(CAMERA_PING.encode(), ROUTER_PING.encode()),
        clock=clock or time.monotonic,
        host_metrics=metrics or (lambda **_: dict(HEALTHY_METRICS)),
        proc_root=proc_root or Path("/nonexistent-proc"),
        sys_class_net=sys_class_net or Path("/nonexistent-sys"),
        internet_connect=internet_connect or (lambda host: dict(HEALTHY_INTERNET)),
        child_timeout=child_timeout,
        ping_timeout=ping_timeout,
    )


class HopComparisonTests(unittest.TestCase):
    """The comparison is the diagnostic; a single number is not."""

    def cycle(self):
        popen = FakePopen(CAMERA_PING.encode(), ROUTER_PING.encode())
        worker = probe(popen=popen, proc_root=route_root(self),
                       lan_host="camera.invalid")
        worker.run_once()
        return worker.status(), popen

    def test_the_same_switch_and_across_powerline_hops_are_measured_separately(self):
        status, popen = self.cycle()

        self.assertEqual({"lan", "router", "internet"}, set(status["hops"]))
        self.assertEqual(2, len(popen.commands("ping")))
        self.assertIn("camera.invalid", popen.commands("ping")[0])
        self.assertIn("192.168.0.1", popen.commands("ping")[1])

    def test_the_clean_switch_hop_sits_beside_the_lossy_powerline_hop(self):
        status, _ = self.cycle()

        lan, router = status["hops"]["lan"], status["hops"]["router"]
        self.assertEqual(0.0, lan["loss"])
        self.assertEqual(0.1, router["loss"])
        self.assertLess(lan["p95_ms"], 1.0)
        self.assertGreater(router["p95_ms"], 200.0)

    def test_an_unnamed_lan_host_is_reported_unconfigured_not_healthy(self):
        worker = probe(popen=FakePopen(ROUTER_PING.encode()),
                       proc_root=route_root(self))
        worker.run_once()

        lan = worker.status()["hops"]["lan"]
        self.assertEqual("unconfigured", lan["state"])
        self.assertNotIn("loss", lan)
        self.assertNotIn("p95_ms", lan)

    def test_a_missing_default_route_leaves_the_router_hop_unconfigured(self):
        worker = probe(proc_root=Path("/nonexistent-proc"))
        worker.run_once()

        self.assertEqual("unconfigured", worker.status()["hops"]["router"]["state"])


class DistributionTests(unittest.TestCase):
    """A mean hides exactly the tail that breaks a recognition upload."""

    def test_the_powerline_hop_reports_a_distribution_not_a_single_number(self):
        parsed = parse_ping(ROUTER_PING)

        self.assertEqual("ok", parsed["state"])
        self.assertEqual(0.1, parsed["loss"])
        self.assertEqual(9, parsed["samples"])
        self.assertEqual(70.1, parsed["min_ms"])
        self.assertEqual(154.2, parsed["p50_ms"])
        self.assertEqual(228.44, parsed["p95_ms"])
        self.assertEqual(245.0, parsed["max_ms"])
        self.assertEqual(148.41, parsed["mean_ms"])

    def test_the_ninety_fifth_percentile_is_not_silently_the_maximum(self):
        parsed = parse_ping(ROUTER_PING)

        self.assertLess(parsed["p95_ms"], parsed["max_ms"])
        self.assertGreater(parsed["p95_ms"], parsed["p50_ms"])

    def test_the_camera_hop_distribution_matches_the_measured_baseline(self):
        parsed = parse_ping(CAMERA_PING)

        self.assertEqual(0.0, parsed["loss"])
        self.assertEqual(10, parsed["samples"])
        self.assertEqual(0.31, parsed["min_ms"])
        self.assertEqual(0.37, parsed["p50_ms"])
        self.assertEqual(0.82, parsed["max_ms"])
        self.assertEqual(0.41, parsed["mean_ms"])

    def test_jitter_is_the_variation_between_consecutive_round_trips(self):
        parsed = parse_ping(ROUTER_PING)

        self.assertEqual(85.15, parsed["jitter_ms"])
        self.assertLess(parse_ping(CAMERA_PING)["jitter_ms"], 1.0)

    def test_a_single_reply_reports_no_jitter_rather_than_zero(self):
        parsed = parse_ping(ping_output("192.168.0.1", 5, [12.5]))

        self.assertEqual(1, parsed["samples"])
        self.assertNotIn("jitter_ms", parsed)
        self.assertEqual(0.8, parsed["loss"])

    def test_quiet_ping_output_yields_a_mean_without_inventing_percentiles(self):
        parsed = parse_ping(QUIET_PING)

        self.assertEqual(3.41, parsed["mean_ms"])
        self.assertEqual(2.91, parsed["min_ms"])
        self.assertNotIn("p95_ms", parsed)

    def test_total_loss_is_a_measurement_not_an_exception_or_a_hang(self):
        parsed = parse_ping(LOST_PING)

        self.assertEqual("lost", parsed["state"])
        self.assertEqual(1.0, parsed["loss"])
        self.assertNotIn("max_ms", parsed)

    def test_unparseable_ping_output_reports_nothing_rather_than_guessing(self):
        parsed = parse_ping("ping: unknown host\n")

        self.assertEqual("failed", parsed["state"])
        self.assertNotIn("loss", parsed)

    def test_a_broken_child_is_not_reported_as_a_hundred_percent_loss(self):
        def refuse(command, **kwargs):
            raise OSError("fork failed")

        worker = probe(popen=refuse, proc_root=route_root(self))
        worker.run_once()

        router = worker.status()["hops"]["router"]
        self.assertEqual("failed", router["state"])
        self.assertNotIn("loss", router)


class JournalLineTests(unittest.TestCase):
    """The device has to be diagnosable over SSH with no cloud involved."""

    def journal(self, **kwargs):
        with self.assertLogs("gate_controller.net_probe", level=logging.INFO) as logs:
            worker = kwargs.pop("worker", None) or probe(**kwargs)
            worker.run_once()
        return logs.output, worker

    def test_every_successful_cycle_writes_exactly_one_structured_line(self):
        popen = FakePopen(CAMERA_PING.encode(), ROUTER_PING.encode())
        output, _ = self.journal(popen=popen, proc_root=route_root(self),
                                 lan_host="camera.invalid")

        self.assertEqual(1, len(output))
        self.assertIn("gate_net_probe outcome=ok mode=full", output[0])

    def test_the_line_carries_both_hops_distributions_side_by_side(self):
        popen = FakePopen(CAMERA_PING.encode(), ROUTER_PING.encode())
        output, _ = self.journal(popen=popen, proc_root=route_root(self),
                                 lan_host="camera.invalid")

        line = output[0]
        for field in ("lan=ok", "lan_loss_pct=0", "lan_n=10", "lan_p95_ms=0.63",
                      "router=ok", "router_loss_pct=10", "router_n=9",
                      "router_min_ms=70.1", "router_p50_ms=154.2",
                      "router_p95_ms=228.44", "router_max_ms=245",
                      "router_jitter_ms=85.15", "internet=ok",
                      "internet_dns_ms=12.4", "internet_total_ms=146.7"):
            with self.subTest(field=field):
                self.assertIn(field, line)

    def test_a_skipped_cycle_still_writes_a_line_naming_the_reason(self):
        output, _ = self.journal(
            metrics=lambda **_: {"soc_temp_c": 86.0, "mem_available_kib": 6_000_000},
        )

        self.assertEqual(
            ["INFO:gate_controller.net_probe:"
             "gate_net_probe outcome=skipped reason=critical_temp"],
            output,
        )

    def test_an_absent_measurement_is_an_absent_key_never_a_confident_zero(self):
        line = journal_line({
            "outcome": "ok", "mode": "ping",
            "hops": {"lan": {"state": "unconfigured"},
                     "router": {"state": "failed"}},
        })

        self.assertEqual("outcome=ok mode=ping lan=unconfigured router=failed", line)
        self.assertNotIn("loss_pct", line)

    def test_the_line_is_parseable_key_value_pairs_with_no_spaces_in_values(self):
        popen = FakePopen(CAMERA_PING.encode(), ROUTER_PING.encode())
        output, _ = self.journal(popen=popen, proc_root=route_root(self),
                                 lan_host="camera.invalid")

        body = output[0].split("gate_net_probe ", 1)[1]
        pairs = [field.split("=", 1) for field in body.split(" ")]
        self.assertTrue(all(len(pair) == 2 and pair[0] and pair[1] for pair in pairs))
        self.assertEqual(len(pairs), len({key for key, _ in pairs}))


class InterfaceCounterTests(unittest.TestCase):
    """Lifetime totals are meaningless after a month of uptime; rates are not."""

    def rates(self, before, after, *, seconds=60.0, speed=None):
        root = route_root(self)
        counters = {"value": 0.0}
        sys_root = Path(tempfile.mkdtemp(dir=root))
        if speed is not None:
            (sys_root / "eth0").mkdir()
            (sys_root / "eth0" / "speed").write_text(f"{speed}\n", encoding="utf-8")
        worker = probe(popen=FakePopen(*[ROUTER_PING.encode()] * 8),
                       proc_root=root, sys_class_net=sys_root,
                       clock=lambda: counters["value"])
        write_counters(root, before)
        worker.run_once()
        first = worker.status()
        counters["value"] = seconds
        write_counters(root, after)
        worker.run_once()
        return first, worker.status()

    def test_the_first_cycle_reports_no_rate_rather_than_a_lifetime_total(self):
        first, _ = self.rates(
            counter_row(rx_bytes=783_827_097_918, rx_packets=646_734_326,
                        rx_dropped=2_949_738),
            counter_row(rx_bytes=783_827_097_918, rx_packets=646_734_326,
                        rx_dropped=2_949_738),
        )

        self.assertNotIn("interface", first)

    def test_drops_errors_and_packets_are_reported_as_rates_over_the_interval(self):
        _, second = self.rates(
            counter_row(rx_bytes=1_000, rx_packets=646_734_326, rx_dropped=2_949_738,
                        rx_errors=4, tx_bytes=2_000, tx_packets=100, tx_errors=1),
            counter_row(rx_bytes=61_000, rx_packets=646_754_326, rx_dropped=2_949_762,
                        rx_errors=4, tx_bytes=122_000, tx_packets=1_300, tx_errors=1),
        )

        interface = second["interface"]
        self.assertEqual("eth0", interface["name"])
        self.assertEqual(1000.0, interface["receive_bytes_per_s"])
        self.assertEqual(2000.0, interface["transmit_bytes_per_s"])
        self.assertEqual(333.3, interface["receive_packets_per_s"])
        self.assertEqual(20.0, interface["transmit_packets_per_s"])
        self.assertEqual(0.4, interface["receive_dropped_per_s"])
        self.assertEqual(0.0, interface["receive_errors_per_s"])
        self.assertEqual(0.0, interface["transmit_errors_per_s"])

    def test_the_drop_share_is_the_measured_zero_point_one_two_percent(self):
        _, second = self.rates(
            counter_row(rx_packets=0, rx_dropped=0),
            counter_row(rx_packets=19_976, rx_dropped=24),
        )

        self.assertEqual(0.12, second["interface"]["receive_dropped_pct"])

    def test_the_negotiated_link_speed_is_reported_when_the_driver_says(self):
        _, second = self.rates(counter_row(), counter_row(rx_packets=10), speed=100)

        self.assertEqual(100, second["interface"]["link_mbps"])

    def test_a_down_link_reports_no_speed_rather_than_minus_one(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "eth0").mkdir()
        (root / "eth0" / "speed").write_text("-1\n", encoding="utf-8")

        self.assertIsNone(link_speed_mbps("eth0", root))
        self.assertIsNone(link_speed_mbps("eth0", Path("/nonexistent-sys")))
        self.assertIsNone(link_speed_mbps(None, root))

    def test_a_counter_reset_reports_nothing_rather_than_a_negative_rate(self):
        _, second = self.rates(
            counter_row(rx_bytes=5_000_000, rx_packets=90_000),
            counter_row(rx_bytes=1_000, rx_packets=10),
        )

        self.assertNotIn("interface", second)

    def test_a_default_route_that_moves_interface_restarts_the_baseline(self):
        # Differencing eth1's totals against eth0's would manufacture a rate.
        root = route_root(self)
        counters = {"value": 0.0}
        worker = probe(
            popen=FakePopen(ROUTER_PING.encode(), b"", ROUTER_PING.encode(), b"",
                            b"not ping output"),
            proc_root=root, clock=lambda: counters["value"],
        )
        write_counters(root, counter_row(rx_bytes=1_000, rx_packets=10))
        worker.run_once()
        counters["value"] = 60.0
        write_counters(root, counter_row(rx_bytes=61_000, rx_packets=1_010))
        worker.run_once()
        self.assertEqual(1000.0, worker.status()["interface"]["receive_bytes_per_s"])

        (root / "net" / "route").write_text(
            ROUTE.replace("eth0", "eth1"), encoding="utf-8"
        )
        (root / "net" / "dev").write_text(
            (root / "net" / "dev").read_text(encoding="utf-8").replace("eth0", "eth1"),
            encoding="utf-8",
        )
        counters["value"] = 120.0
        worker.run_once()

        self.assertEqual("failed", worker.status()["hops"]["router"]["state"])
        self.assertNotIn("interface", worker.status())

    def test_the_counters_come_from_proc_and_omit_an_unknown_interface(self):
        root = route_root(self)
        write_counters(root, counter_row(rx_bytes=7, rx_packets=8, rx_errors=1,
                                         rx_dropped=2, tx_bytes=9, tx_packets=10,
                                         tx_errors=3))

        self.assertEqual(
            {"receive_bytes": 7, "receive_packets": 8, "receive_errors": 1,
             "receive_dropped": 2, "transmit_bytes": 9, "transmit_packets": 10,
             "transmit_errors": 3},
            interface_counters(root, "eth0"),
        )
        self.assertIsNone(interface_counters(root, "wlan0"))
        self.assertIsNone(interface_counters(Path("/nonexistent-proc"), "eth0"))
        self.assertIsNone(interface_counters(root, None))


class InternetHopTests(unittest.TestCase):
    def test_the_end_to_end_open_is_split_into_dns_connect_and_tls(self):
        worker = probe()
        worker.run_once()

        internet = worker.status()["hops"]["internet"]
        self.assertEqual("ok", internet["state"])
        self.assertEqual(12.4, internet["dns_ms"])
        self.assertEqual(38.1, internet["connect_ms"])
        self.assertEqual(96.2, internet["tls_ms"])
        self.assertEqual(146.7, internet["total_ms"])

    def test_the_internet_hop_keeps_its_slower_cadence_and_reports_its_age(self):
        opens = []
        counters = {"value": 0.0}
        worker = probe(clock=lambda: counters["value"],
                       internet_connect=lambda host: opens.append(host) or dict(
                           HEALTHY_INTERNET))

        worker.run_once()
        counters["value"] = 60.0
        worker.run_once()
        self.assertEqual(1, len(opens))
        self.assertEqual(60.0, worker.status()["hops"]["internet"]["age_seconds"])

        counters["value"] = 400.0
        worker.run_once()
        self.assertEqual(2, len(opens))
        self.assertEqual(0.0, worker.status()["hops"]["internet"]["age_seconds"])

    def test_a_refused_open_is_reported_rather_than_raised(self):
        def refuse(host):
            raise OSError("connection refused")

        worker = probe(internet_connect=refuse)
        worker.run_once()

        internet = worker.status()["hops"]["internet"]
        self.assertEqual("failed", internet["state"])
        self.assertNotIn("total_ms", internet)

    def test_the_handshake_is_measured_over_a_protocol_we_would_actually_use(self):
        # A handshake timed over TLS 1.0 would be a number for a connection the
        # OCR client would refuse, which is worse than reporting none.
        context = tls_context()

        self.assertEqual(ssl.TLSVersion.TLSv1_2, context.minimum_version)
        self.assertTrue(context.check_hostname)
        self.assertEqual(ssl.CERT_REQUIRED, context.verify_mode)


class GovernorTests(unittest.TestCase):
    """Two tiers: a hard floor under the pings, a governor over the extras."""

    def run_with(self, metrics, **kwargs):
        popen = FakePopen(CAMERA_PING.encode(), ROUTER_PING.encode())
        worker = probe(popen=popen, metrics=lambda **_: metrics,
                       proc_root=route_root(self), lan_host="camera.invalid",
                       **kwargs)
        worker.run_once()
        return worker.status(), popen

    def test_a_healthy_board_runs_the_whole_cycle(self):
        status, popen = self.run_with({"soc_temp_c": 71.4, "load_1m": 0.6,
                                       "mem_available_kib": 6_000_000})

        self.assertEqual("full", status["mode"])
        self.assertIsNone(status["skipped_reason"])
        self.assertIn("internet", status["hops"])
        self.assertEqual(2, len(popen.commands("ping")))

    def test_the_measured_idle_board_is_nowhere_near_either_ceiling(self):
        # 66-75 C and a 4-core load average around 0.5 is what this board
        # actually idles at; the probe must run there, every cycle.
        for temperature in (66.7, 71.4, 75.0):
            with self.subTest(temperature=temperature):
                status, _ = self.run_with({"soc_temp_c": temperature, "load_1m": 0.5,
                                           "mem_available_kib": 6_000_000})

                self.assertEqual("full", status["mode"])

    def test_a_hot_board_still_measures_the_hops_and_names_what_was_withheld(self):
        status, popen = self.run_with({"soc_temp_c": 82.0, "load_1m": 0.2,
                                       "mem_available_kib": 6_000_000})

        self.assertEqual("ping", status["mode"])
        self.assertEqual("hot", status["skipped_reason"])
        self.assertTrue(status["probed"])
        self.assertEqual(0.1, status["hops"]["router"]["loss"])
        self.assertNotIn("internet", status["hops"])

    def test_a_loaded_board_still_measures_because_that_is_when_it_matters(self):
        # A recognition burst raises the load average, which is precisely the
        # moment a lost sample would be worth most.
        status, popen = self.run_with({"soc_temp_c": 70.0, "load_1m": DEFAULT_MAX_LOAD,
                                       "mem_available_kib": 6_000_000})

        self.assertEqual("ping", status["mode"])
        self.assertEqual("loaded", status["skipped_reason"])
        self.assertEqual(2, len(popen.commands("ping")))

    def test_the_expensive_tier_is_withheld_under_three_hundred_megabytes(self):
        available_kib = (DEFAULT_MIN_AVAILABLE_BYTES // 1024) - 1
        status, _ = self.run_with({"soc_temp_c": 60.0, "load_1m": 0.2,
                                   "mem_available_kib": available_kib})

        self.assertEqual("low_memory", status["skipped_reason"])
        self.assertEqual("ping", status["mode"])

    def test_the_ceiling_is_inclusive_so_exactly_eighty_degrees_withholds(self):
        status, _ = self.run_with({"soc_temp_c": DEFAULT_MAX_TEMP_C, "load_1m": 0.2,
                                   "mem_available_kib": 6_000_000})

        self.assertEqual("hot", status["skipped_reason"])

    def test_at_the_firmware_throttle_point_nothing_runs_at_all(self):
        status, popen = self.run_with({"soc_temp_c": CRITICAL_TEMP_C, "load_1m": 0.2,
                                       "mem_available_kib": 6_000_000})

        self.assertEqual("critical_temp", status["skipped_reason"])
        self.assertFalse(status["probed"])
        self.assertEqual([], popen.calls)

    def test_a_board_out_of_memory_adds_no_work_of_any_kind(self):
        available_kib = (CRITICAL_MIN_AVAILABLE_BYTES // 1024) - 1
        status, popen = self.run_with({"soc_temp_c": 60.0, "load_1m": 0.2,
                                       "mem_available_kib": available_kib})

        self.assertEqual("critical_memory", status["skipped_reason"])
        self.assertEqual([], popen.calls)

    def test_an_unreadable_governor_metric_does_not_disable_the_probe_forever(self):
        status, _ = self.run_with({})

        self.assertIsNone(status["skipped_reason"])
        self.assertEqual("full", status["mode"])

    def test_a_governor_that_raises_is_swallowed_like_every_other_failure(self):
        def explode(**_):
            raise OSError("thermal zone moved")

        worker = probe(metrics=explode)

        self.assertTrue(worker.run_once())


class ChildProcessBoundTests(unittest.TestCase):
    def test_the_single_child_is_address_space_limited_and_silenced(self):
        popen = FakePopen(ROUTER_PING.encode())
        worker = probe(popen=popen, proc_root=route_root(self))

        worker.run_once()

        command, kwargs = popen.calls[0]
        self.assertEqual("ping", command[0])
        self.assertIn("-c", command)
        self.assertEqual(str(DEFAULT_PING_COUNT), command[command.index("-c") + 1])
        self.assertIs(_limit_address_space, kwargs["preexec_fn"])
        self.assertEqual(subprocess.DEVNULL, kwargs["stderr"])
        self.assertEqual(subprocess.PIPE, kwargs["stdout"])
        self.assertEqual(subprocess.DEVNULL, kwargs["stdin"])
        self.assertTrue(kwargs["close_fds"])

    def test_ping_carries_its_own_deadline_so_the_kill_is_never_the_normal_path(self):
        command = ping_command("192.168.0.1", 10)

        self.assertEqual("3", command[command.index("-w") + 1])
        self.assertLess(ping_timeout_seconds(10), MAX_CHILD_TIMEOUT_SECONDS)
        self.assertGreater(ping_timeout_seconds(10), 3.0)

    def test_even_the_longest_ping_plan_stays_inside_the_hard_kill_cap(self):
        self.assertLessEqual(ping_timeout_seconds(30), MAX_CHILD_TIMEOUT_SECONDS)

    def test_the_address_space_ceiling_is_64_mib(self):
        self.assertEqual(64 * 1024 * 1024, CHILD_ADDRESS_SPACE_BYTES)

    def test_the_default_kill_timeout_is_three_seconds(self):
        self.assertEqual(3.0, CHILD_TIMEOUT_SECONDS)
        self.assertEqual(3.0, probe().child_timeout)

    def test_a_child_that_never_exits_is_killed_at_the_deadline(self):
        popen = FakePopen(hang=True)
        worker = probe(popen=popen, proc_root=route_root(self), ping_timeout=0.05,
                       child_timeout=0.05)

        started = time.monotonic()
        worker.run_once()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.5)
        self.assertTrue(popen.processes[0].killed)
        self.assertEqual("failed", worker.status()["hops"]["router"]["state"])

    def test_an_oversized_child_stdout_is_refused_rather_than_held(self):
        popen = FakePopen(b"x" * (MAX_CHILD_OUTPUT_BYTES + 1))
        worker = probe(popen=popen, proc_root=route_root(self))

        worker.run_once()

        self.assertEqual("failed", worker.status()["hops"]["router"]["state"])
        self.assertTrue(popen.processes[0].killed)

    def test_close_kills_the_live_child_and_refuses_to_start_another(self):
        popen = FakePopen(ROUTER_PING.encode())
        worker = probe(popen=popen, proc_root=route_root(self))
        worker.close()

        self.assertFalse(worker.run_once())
        self.assertEqual([], popen.calls)


class SingleFlightTests(unittest.TestCase):
    def test_at_most_one_child_is_ever_alive_across_concurrent_cycles(self):
        root = route_root(self)
        popen = FakePopen(*[ROUTER_PING.encode()] * 60)
        worker = probe(popen=popen, proc_root=root, lan_host="camera.invalid")

        threads = [Thread(target=worker.run_once) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(1, popen.max_live)
        # Contended cycles are dropped, never queued behind the one in flight.
        self.assertLessEqual(len(popen.commands("ping")), 16)


class PrivacyTests(unittest.TestCase):
    def test_the_reported_status_carries_no_address_path_or_credential(self):
        for lan_host in ("192.168.0.54", "front-gate-camera.lan"):
            with self.subTest(lan_host=lan_host):
                worker = probe(
                    popen=FakePopen(CAMERA_PING.encode(), ROUTER_PING.encode()),
                    proc_root=route_root(self), lan_host=lan_host,
                )
                worker.run_once()

                rendered = str(worker.status())
                self.assertNotRegex(rendered, r"\b\d{1,3}(\.\d{1,3}){3}\b")
                self.assertNotIn("/", rendered)
                # The configured hop target is a ping destination, never a
                # field: the heartbeat leaves this device carrying labels only.
                self.assertNotIn(lan_host, rendered)

    def test_the_default_gateway_is_read_from_the_route_table_never_hard_coded(self):
        root = route_root(self)

        self.assertEqual("192.168.0.1", default_gateway(root))
        self.assertEqual("eth0", default_interface(root))
        self.assertNotIn("192.168.0.1", executable_source(Path(net_probe.__file__)))

    def test_an_unreadable_route_table_yields_no_gateway_and_no_exception(self):
        self.assertIsNone(default_gateway(Path("/nonexistent-proc")))
        self.assertIsNone(default_interface(Path("/nonexistent-proc")))

    def test_only_a_completed_cycle_is_published(self):
        worker = probe()

        self.assertFalse(worker.status()["probed"])
        self.assertNotIn("hops", worker.status())


class ConfigurationTests(unittest.TestCase):
    def test_the_defaults_are_the_measured_thermal_and_load_ceilings(self):
        config = load_net_probe_config({})

        self.assertTrue(config.enabled)
        self.assertEqual(60.0, config.interval_seconds)
        self.assertEqual(300.0, config.tls_interval_seconds)
        self.assertEqual(80.0, config.max_temp_c)
        self.assertEqual(3.0, config.max_load)
        self.assertEqual(300 * 1024 * 1024, config.min_available_bytes)
        self.assertEqual(10, config.ping_count)
        self.assertEqual("", config.lan_host)

    def test_the_same_switch_hop_is_named_by_configuration_not_by_discovery(self):
        config = load_net_probe_config({"GATE_NET_PROBE_LAN_HOST": "192.168.0.54"})

        self.assertEqual("192.168.0.54", config.lan_host)

    def test_the_probe_can_be_switched_off_entirely(self):
        self.assertFalse(load_net_probe_config({"GATE_NET_PROBE_ENABLED": "false"}).enabled)

    def test_a_disabled_probe_never_runs_a_cycle(self):
        popen = FakePopen(ROUTER_PING.encode())
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
            ({"GATE_NET_PROBE_PING_COUNT": "1"}, "between"),
            ({"GATE_NET_PROBE_PING_COUNT": "60"}, "between"),
            ({"GATE_NET_PROBE_TLS_HOST": ""}, "hostname"),
            ({"GATE_NET_PROBE_LAN_HOST": "http://camera/live"}, "hostname"),
            ({"GATE_NET_PROBE_LAN_HOST": "user@camera"}, "hostname"),
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
        root = route_root(self)
        write_counters(root, counter_row(rx_bytes=1000, rx_packets=10, tx_bytes=2000,
                                         tx_packets=12))
        worker = probe(
            popen=FakePopen(*[ROUTER_PING.encode()] * (cycles * 4 + 8)),
            proc_root=root, lan_host="camera.invalid",
        )
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
