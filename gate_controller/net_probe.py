"""A governed, single-flight network probe for the journal and the heartbeat.

The question this exists to settle is not "is the network bad" but "which
hop". Measured from the Pi on 2026-09-07:

* Pi to camera, which stays on the gate switch and never crosses the powerline
  bridge -- mean 0.389 ms, max 0.821 ms, 0 % loss over 30 packets;
* Pi to router, which does cross it -- min 70 ms, mean 167 ms, max 245 ms,
  4 % loss over 100 packets;
* Pi to the plate service, end to end -- 0.58 s to 1.05 s, healthy.

Only the *comparison* is diagnostic. A flawless same-switch hop beside a lossy
across-powerline hop isolates the bridge; two equally clean hops exonerate it.
Either answer is a good outcome, so the probe measures all three separately and
labels them, and reports a distribution rather than a mean: a small web-console
request is imperceptible at 200 ms, while a recognition upload is several round
trips plus a bulk transfer, so it pays the *tail*, not the average.

The bounds, all of them load-bearing:

* one extra thread on a 60 s poll inside the existing controller process, with
  no new systemd unit;
* a ``BoundedSemaphore(1)`` so at most one child process is ever alive;
* every child gets ``RLIMIT_AS`` of 64 MiB, a deadline derived from the ping
  plan and hard-capped at 10 s, a capped stdout, ``stderr`` to ``/dev/null``,
  and a kill-and-reap on the way out; and
* **permanently forbidden in this module**: ffmpeg, any video or image decode,
  numpy, onnxruntime, any model load, ``journalctl``, any throughput test, and
  any unbounded read.

There is deliberately no speed test. The uplink is about 4.5 Mbit/s and OCR
uploads already saturate it, so a throughput probe would compete with the
thing it is meant to measure; the ``/proc/net/dev`` delta carries the load.

Two tiers, because a probe that rarely runs proves nothing either way. The
board idles at 66-75 C against an 80 C ceiling and a 4-core load average
around 0.5, so the old single governor withheld the *whole* cycle exactly when
a recognition burst made the network interesting. The ping tier now always
runs -- it is two ``ping`` children under a 64 MiB address-space limit, which
is nothing like the 4K decode that OOM-killed this board -- behind a hard
floor at the 85 C hardware-throttle point and 96 MB available. The more
expensive extras (the DNS/TCP/TLS timing to the plate service, and
``vcgencmd``) stay behind the original 80 C / 3.0 load / 300 MB governor, and
the withheld reason is always recorded so the gap is explicit, not silent.

Every cycle writes one ``key=value`` journal line, successes included, so the
device is diagnosable over SSH with no cloud involvement. Nothing here may
block, lock or delay the relay path, and every failure is swallowed and
reported as an absent field -- never a confident zero.
"""
import logging
import math
import os
import resource
import select
import socket
import ssl
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import BoundedSemaphore, Event, Lock

from .host_metrics import MAX_PROC_BYTES, read_host_metrics, read_throttled_flags, read_proc_value

LOGGER = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 60.0
DEFAULT_TLS_INTERVAL_SECONDS = 300.0
DEFAULT_TLS_HOST = "api.platerecognizer.com"
DEFAULT_MAX_TEMP_C = 80.0
DEFAULT_MAX_LOAD = 3.0
DEFAULT_MIN_AVAILABLE_BYTES = 300 * 1024 * 1024
# The hard floor under the always-on ping tier. 85 C is where this board's
# firmware throttles, and 96 MB is well under the 300 MB the expensive tier
# demands: below either, add no work of any kind.
CRITICAL_TEMP_C = 85.0
CRITICAL_MIN_AVAILABLE_BYTES = 96 * 1024 * 1024
CHILD_TIMEOUT_SECONDS = 3.0
MAX_CHILD_TIMEOUT_SECONDS = 10.0
CHILD_ADDRESS_SPACE_BYTES = 64 * 1024 * 1024
MAX_CHILD_OUTPUT_BYTES = 8 * 1024
MAX_NETWORK_INTERFACES = 32
DEFAULT_PING_COUNT = 10
MIN_PING_COUNT = 3
MAX_PING_COUNT = 30
PING_INTERVAL_SECONDS = 0.2
PING_REPLY_GRACE_SECONDS = 1.0
MAX_PING_LINES = 128
INTERFACE_NAME_MAX = 16
HOSTNAME_MAX = 253
SYS_CLASS_NET = Path("/sys/class/net")

HOP_LAN = "lan"
HOP_ROUTER = "router"
HOP_INTERNET = "internet"

STATE_OK = "ok"
STATE_LOST = "lost"
STATE_FAILED = "failed"
STATE_UNCONFIGURED = "unconfigured"

MODE_FULL = "full"
MODE_PING = "ping"


@dataclass(frozen=True)
class NetProbeConfig:
    enabled: bool = False
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    tls_interval_seconds: float = DEFAULT_TLS_INTERVAL_SECONDS
    tls_host: str = DEFAULT_TLS_HOST
    lan_host: str = ""
    ping_count: int = DEFAULT_PING_COUNT
    max_temp_c: float = DEFAULT_MAX_TEMP_C
    max_load: float = DEFAULT_MAX_LOAD
    min_available_bytes: int = DEFAULT_MIN_AVAILABLE_BYTES


def _float_setting(environment, name, default, *, minimum, maximum) -> float:
    raw = str(environment.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a number") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _hostname_setting(environment, name, default) -> str:
    """A hostname or address, or the default. Never a URL, never a path."""
    raw = str(environment.get(name, default)).strip()
    if not raw:
        return ""
    if (len(raw) > HOSTNAME_MAX or any(character.isspace() for character in raw)
            or "/" in raw or "@" in raw):
        raise ValueError(f"{name} must be a hostname or address")
    return raw


def load_net_probe_config(environment=None) -> NetProbeConfig:
    """Read the probe configuration, rejecting anything outside safe bounds.

    The default is enabled: the governor and the hard floor are what make "on"
    defensible on a board with roughly six degrees of thermal headroom, and a
    probe that is off by default would not have caught the powerline
    degradation.
    """
    environment = os.environ if environment is None else environment
    enabled = str(environment.get("GATE_NET_PROBE_ENABLED", "true")).strip().lower()
    if enabled not in {"true", "false", "1", "0", "yes", "no", ""}:
        raise ValueError("GATE_NET_PROBE_ENABLED must be true or false")
    host = _hostname_setting(environment, "GATE_NET_PROBE_TLS_HOST", DEFAULT_TLS_HOST)
    if not host:
        raise ValueError("GATE_NET_PROBE_TLS_HOST must be a hostname or address")
    return NetProbeConfig(
        enabled=enabled in {"true", "1", "yes", ""},
        interval_seconds=_float_setting(
            environment, "GATE_NET_PROBE_INTERVAL_SECONDS",
            DEFAULT_INTERVAL_SECONDS, minimum=15.0, maximum=3600.0,
        ),
        tls_interval_seconds=_float_setting(
            environment, "GATE_NET_PROBE_TLS_INTERVAL_SECONDS",
            DEFAULT_TLS_INTERVAL_SECONDS, minimum=60.0, maximum=86400.0,
        ),
        tls_host=host,
        # Deliberately unset by default: the same-switch hop is the whole
        # comparison, but guessing an address on someone's LAN is worse than
        # reporting the hop as unconfigured until it is named.
        lan_host=_hostname_setting(environment, "GATE_NET_PROBE_LAN_HOST", ""),
        ping_count=int(_float_setting(
            environment, "GATE_NET_PROBE_PING_COUNT",
            float(DEFAULT_PING_COUNT),
            minimum=float(MIN_PING_COUNT), maximum=float(MAX_PING_COUNT),
        )),
        max_temp_c=_float_setting(
            environment, "GATE_NET_PROBE_MAX_TEMP_C",
            DEFAULT_MAX_TEMP_C, minimum=40.0, maximum=85.0,
        ),
        max_load=_float_setting(
            environment, "GATE_NET_PROBE_MAX_LOAD",
            DEFAULT_MAX_LOAD, minimum=0.5, maximum=16.0,
        ),
    )


def _limit_address_space() -> None:  # pragma: no cover - runs in the child
    resource.setrlimit(
        resource.RLIMIT_AS, (CHILD_ADDRESS_SPACE_BYTES, CHILD_ADDRESS_SPACE_BYTES),
    )


def _terminate(process) -> None:
    """Kill and reap a probe child, tolerating one that has already gone."""
    try:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
    except OSError:
        pass
    stdout = getattr(process, "stdout", None)
    if stdout is not None:
        try:
            stdout.close()
        except OSError:
            pass


def default_gateway(proc_root=Path("/proc")) -> str | None:
    """The default gateway from /proc/net/route. Never hard-coded."""
    raw = read_proc_value(Path(proc_root) / "net" / "route", max_bytes=MAX_PROC_BYTES)
    if raw is None:
        return None
    for line in raw.splitlines()[1:1 + MAX_NETWORK_INTERFACES]:
        fields = line.split()
        if len(fields) < 3 or fields[1] != "00000000":
            continue
        try:
            packed = int(fields[2], 16).to_bytes(4, "little")
        except (ValueError, OverflowError):
            continue
        return socket.inet_ntoa(packed)
    return None


def default_interface(proc_root=Path("/proc")) -> str | None:
    """The interface carrying the default route, for the uplink counters."""
    raw = read_proc_value(Path(proc_root) / "net" / "route", max_bytes=MAX_PROC_BYTES)
    if raw is None:
        return None
    for line in raw.splitlines()[1:1 + MAX_NETWORK_INTERFACES]:
        fields = line.split()
        if len(fields) < 3 or fields[1] != "00000000":
            continue
        name = fields[0]
        if name and len(name) <= INTERFACE_NAME_MAX and name.replace(".", "").replace("-", "").replace("_", "").isalnum():
            return name
    return None


# /proc/net/dev column offsets after the interface name, in the kernel's order:
# receive bytes packets errs drop fifo frame compressed multicast, then
# transmit bytes packets errs drop fifo colls carrier compressed.
COUNTER_COLUMNS = {
    "receive_bytes": 0,
    "receive_packets": 1,
    "receive_errors": 2,
    "receive_dropped": 3,
    "transmit_bytes": 8,
    "transmit_packets": 9,
    "transmit_errors": 10,
}


def interface_counters(proc_root=Path("/proc"), interface: str | None = None) -> dict | None:
    """The lifetime counters for one interface, or None.

    Lifetime totals are meaningless on a board with a month of uptime -- 2.9 M
    dropped frames says nothing about today -- so nothing reports these
    directly. They exist to be differenced into rates over one cycle.
    """
    if not interface:
        return None
    raw = read_proc_value(Path(proc_root) / "net" / "dev", max_bytes=MAX_PROC_BYTES)
    if raw is None:
        return None
    for line in raw.splitlines()[2:2 + MAX_NETWORK_INTERFACES]:
        if ":" not in line:
            continue
        name, values_text = line.split(":", 1)
        if name.strip() != interface:
            continue
        values = values_text.split()
        if len(values) != 16:
            return None
        try:
            return {field: int(values[column])
                    for field, column in COUNTER_COLUMNS.items()}
        except ValueError:
            return None
    return None


def link_speed_mbps(interface: str | None, sys_class_net=SYS_CLASS_NET) -> int | None:
    """The negotiated link speed, or None when the driver will not say.

    A 100 Mbit/s negotiation where 1000 was expected is a cabling or bridge
    fault visible nowhere else in the heartbeat.
    """
    if not interface:
        return None
    raw = read_proc_value(Path(sys_class_net) / interface / "speed", max_bytes=64)
    if raw is None:
        return None
    try:
        speed = int(raw.strip())
    except ValueError:
        return None
    # Drivers report -1 for a down or virtual link; that is not a measurement.
    return speed if speed > 0 else None


def _percentile(ordered: list, fraction: float) -> float:
    """Linear-interpolated percentile of an already sorted list.

    Nearest-rank would make p95 identical to the maximum at these sample
    sizes, which would report a distribution while hiding one.
    """
    if not ordered:
        raise ValueError("no samples")
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _jitter_ms(samples: list) -> float | None:
    """Mean absolute difference between consecutive round trips.

    In arrival order, not sorted: jitter is the variation a stream actually
    experiences, which is what a multi-round-trip upload pays for.
    """
    if len(samples) < 2:
        return None
    gaps = [abs(samples[index] - samples[index - 1])
            for index in range(1, len(samples))]
    return math.fsum(gaps) / len(gaps)


def _distribution(samples: list) -> dict:
    ordered = sorted(samples)
    measured = {
        "samples": len(ordered),
        "min_ms": round(ordered[0], 2),
        "mean_ms": round(math.fsum(ordered) / len(ordered), 2),
        "p50_ms": round(_percentile(ordered, 0.50), 2),
        "p95_ms": round(_percentile(ordered, 0.95), 2),
        "max_ms": round(ordered[-1], 2),
    }
    jitter = _jitter_ms(samples)
    if jitter is not None:
        measured["jitter_ms"] = round(jitter, 2)
    return measured


def _summary_distribution(numbers: list) -> dict:
    """min/avg/max from ``ping -q``'s summary, with no percentiles invented."""
    measured = {"min_ms": round(numbers[0], 2), "mean_ms": round(numbers[1], 2)}
    if len(numbers) >= 3:
        measured["max_ms"] = round(numbers[2], 2)
    return measured


def _reply_time_ms(line: str) -> float | None:
    marker = line.rfind("time=")
    if marker == -1:
        return None
    tail = line[marker + len("time="):].split()
    if not tail:
        return None
    try:
        return float(tail[0])
    except ValueError:
        return None


def parse_ping(output: str) -> dict:
    """Loss and the round-trip distribution from ``ping`` output.

    100 % loss is a measurement, not an error: the probe reports
    ``state=lost`` with ``loss=1.0`` when the powerline bridge is unplugged,
    and keeps ``state=failed`` for the different case where ``ping`` itself
    never produced usable output. Conflating the two would let a broken child
    masquerade as a broken network.
    """
    samples: list = []
    loss = None
    summary: list = []
    for line in output.splitlines()[:MAX_PING_LINES]:
        stripped = line.strip()
        if "packets transmitted" in stripped:
            fields = stripped.replace(",", " ").split()
            try:
                transmitted = int(fields[0])
                received = int(fields[fields.index("received") - 1])
            except (IndexError, ValueError):
                continue
            if transmitted > 0:
                loss = round(max(0.0, (transmitted - received) / transmitted), 3)
        elif stripped.startswith(("rtt ", "round-trip ")) and "=" in stripped:
            numbers = stripped.split("=", 1)[1].strip().split()[0].split("/")
            try:
                summary = [float(number) for number in numbers[:3]]
            except ValueError:
                summary = []
        elif "icmp_seq=" in stripped:
            sample = _reply_time_ms(stripped)
            if sample is not None and sample >= 0.0:
                samples.append(sample)
    if samples:
        return {"state": STATE_OK, "loss": loss, **_distribution(samples)}
    if len(summary) >= 2:
        # ``ping -q`` output: a mean without a tail, reported as exactly that.
        return {"state": STATE_OK, "loss": loss, **_summary_distribution(summary)}
    if loss is not None and loss >= 1.0:
        return {"state": STATE_LOST, "loss": loss}
    return {"state": STATE_FAILED}


def ping_command(target: str, count: int) -> tuple:
    """A bounded ping: fixed count, fixed interval, its own overall deadline.

    ``-w`` makes ``ping`` end itself before the parent's kill deadline, so the
    normal path never depends on the kill at all.
    """
    deadline = max(2, math.ceil(count * PING_INTERVAL_SECONDS + PING_REPLY_GRACE_SECONDS))
    return (
        "ping", "-n", "-c", str(count), "-i", str(PING_INTERVAL_SECONDS),
        "-W", "1", "-w", str(deadline), target,
    )


def ping_timeout_seconds(count: int) -> float:
    """The parent's kill deadline: the ping plan plus slack, hard-capped."""
    planned = count * PING_INTERVAL_SECONDS + PING_REPLY_GRACE_SECONDS + 1.5
    return min(MAX_CHILD_TIMEOUT_SECONDS, planned)


def _format(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


PING_JOURNAL_FIELDS = (
    ("loss_pct", "loss", 100.0),
    ("n", "samples", None),
    ("min_ms", "min_ms", None),
    ("p50_ms", "p50_ms", None),
    ("p95_ms", "p95_ms", None),
    ("max_ms", "max_ms", None),
    ("jitter_ms", "jitter_ms", None),
)
INTERNET_JOURNAL_FIELDS = (
    ("dns_ms", "dns_ms"),
    ("connect_ms", "connect_ms"),
    ("tls_ms", "tls_ms"),
    ("total_ms", "total_ms"),
    ("age_s", "age_seconds"),
)
INTERFACE_JOURNAL_FIELDS = (
    ("iface", "name"),
    ("link_mbps", "link_mbps"),
    ("rx_bytes_per_s", "receive_bytes_per_s"),
    ("tx_bytes_per_s", "transmit_bytes_per_s"),
    ("rx_pkt_per_s", "receive_packets_per_s"),
    ("tx_pkt_per_s", "transmit_packets_per_s"),
    ("rx_drop_per_s", "receive_dropped_per_s"),
    ("rx_drop_pct", "receive_dropped_pct"),
    ("rx_err_per_s", "receive_errors_per_s"),
    ("tx_err_per_s", "transmit_errors_per_s"),
)


def journal_fields(measured: dict) -> list:
    """One cycle rendered as ordered ``key=value`` pairs.

    A measurement that could not be taken is an absent key, never a zero: the
    reader must be able to tell "0 % loss" from "no answer".
    """
    fields = [("outcome", measured.get("outcome", "ok"))]
    if measured.get("mode"):
        fields.append(("mode", measured["mode"]))
    if measured.get("skipped_reason"):
        fields.append(("reason", measured["skipped_reason"]))
    hops = measured.get("hops") or {}
    for label in (HOP_LAN, HOP_ROUTER):
        hop = hops.get(label)
        if not hop:
            continue
        fields.append((label, hop.get("state", STATE_FAILED)))
        for suffix, key, scale in PING_JOURNAL_FIELDS:
            value = hop.get(key)
            if value is None:
                continue
            fields.append((f"{label}_{suffix}",
                           round(value * scale, 2) if scale else value))
    internet = hops.get(HOP_INTERNET)
    if internet:
        fields.append((HOP_INTERNET, internet.get("state", STATE_FAILED)))
        for suffix, key in INTERNET_JOURNAL_FIELDS:
            value = internet.get(key)
            if value is not None:
                fields.append((f"{HOP_INTERNET}_{suffix}", value))
    interface = measured.get("interface")
    if interface:
        for suffix, key in INTERFACE_JOURNAL_FIELDS:
            value = interface.get(key)
            if value is not None:
                fields.append((suffix, value))
    return fields


def journal_line(measured: dict) -> str:
    return " ".join(f"{key}={_format(value)}" for key, value in journal_fields(measured))


class NetProbeWorker:
    """One thread, one child at a time, a hard floor and a governor in front."""

    def __init__(self, config: NetProbeConfig, *, popen=subprocess.Popen,
                 clock=time.monotonic, host_metrics=read_host_metrics,
                 proc_root=Path("/proc"), sys_class_net=SYS_CLASS_NET,
                 internet_connect=None,
                 child_timeout: float = CHILD_TIMEOUT_SECONDS,
                 ping_timeout: float | None = None):
        self.config = config
        self.child_timeout = child_timeout
        self.ping_timeout = (ping_timeout if ping_timeout is not None
                             else ping_timeout_seconds(config.ping_count))
        self._popen = popen
        self._clock = clock
        self._host_metrics = host_metrics
        self._proc_root = Path(proc_root)
        self._sys_class_net = Path(sys_class_net)
        self._internet_connect = internet_connect or _internet_timings
        self._slot = BoundedSemaphore(1)
        self._lock = Lock()
        self._closed = False
        self._process = None
        self._gateway: str | None = None
        self._interface: str | None = None
        self._counters: tuple | None = None
        self._internet: dict | None = None
        self._internet_at: float | None = None
        self._throttled: dict | None = None
        self._status: dict = {"enabled": config.enabled, "probed": False}
        self._status_at: float | None = None

    # -- public surface ---------------------------------------------------

    def status(self) -> dict:
        """The last completed probe cycle only, never a partial one."""
        with self._lock:
            snapshot = dict(self._status)
            measured_at = self._status_at
        if measured_at is not None:
            snapshot["age_seconds"] = round(max(0.0, self._clock() - measured_at), 1)
        return snapshot

    def throttled_flags(self) -> dict | None:
        """The firmware throttle word from the last cycle that read it, or None.

        It is read only in the governed tier, so under sustained load it can be
        older than the last cycle. It is a latched word, so a stale reading
        under-reports rather than inventing a clean board.
        """
        with self._lock:
            return dict(self._throttled) if self._throttled else None

    def run_once(self) -> bool:
        if not self.config.enabled or self._closed:
            return False
        if not self._slot.acquire(blocking=False):
            # A cycle is already in flight; never run two, never queue one.
            return False
        try:
            return self._cycle()
        except Exception:
            LOGGER.warning("gate_net_probe outcome=failed", exc_info=True)
            return False
        finally:
            self._slot.release()

    def run_forever(self, stop_event: Event) -> None:
        while not stop_event.is_set():
            self.run_once()
            stop_event.wait(self.config.interval_seconds)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            process = self._process
            self._process = None
        if process is not None:
            _terminate(process)

    # -- the cycle --------------------------------------------------------

    def _cycle(self) -> bool:
        metrics = self._read_governor_metrics()
        blocked = self._critical_verdict(metrics)
        if blocked is not None:
            measured = {"enabled": True, "probed": False, "outcome": "skipped",
                        "mode": None, "skipped_reason": blocked}
            LOGGER.info("gate_net_probe %s", journal_line(measured))
            self._publish(measured)
            return False
        withheld = self._governor_verdict(metrics)
        hops = {
            HOP_LAN: self._probe_ping(self.config.lan_host),
            HOP_ROUTER: self._probe_router(),
        }
        if withheld is None:
            internet = self._probe_internet()
            if internet is not None:
                hops[HOP_INTERNET] = internet
            self._throttled = read_throttled_flags(run_command=self._run_child) or self._throttled
        measured: dict = {
            "enabled": True,
            "probed": True,
            "outcome": "ok",
            "mode": MODE_PING if withheld else MODE_FULL,
            "skipped_reason": withheld,
            "hops": hops,
        }
        interface = self._probe_interface()
        if interface is not None:
            measured["interface"] = interface
        LOGGER.info("gate_net_probe %s", journal_line(measured))
        self._publish(measured)
        return True

    def _publish(self, measured: dict) -> None:
        with self._lock:
            self._status = measured
            self._status_at = self._clock()

    def _read_governor_metrics(self) -> dict:
        try:
            metrics = self._host_metrics()
        except Exception:
            return {}
        return metrics if isinstance(metrics, dict) else {}

    def _critical_verdict(self, metrics: dict) -> str | None:
        """Why no work at all may happen this cycle, or None.

        This is the floor, not the governor: two ``ping`` children under a
        64 MiB address-space limit are not what OOM-killed this board, so the
        ping tier is withheld only when the board is already at the firmware
        throttle point or genuinely out of memory.
        """
        temperature = metrics.get("soc_temp_c")
        if isinstance(temperature, (int, float)) and temperature >= CRITICAL_TEMP_C:
            return "critical_temp"
        available = metrics.get("mem_available_kib")
        if (isinstance(available, int)
                and available * 1024 < CRITICAL_MIN_AVAILABLE_BYTES):
            return "critical_memory"
        return None

    def _governor_verdict(self, metrics: dict) -> str | None:
        """Why the expensive extras must not run this cycle, or None.

        A metric that could not be read is permissive: a probe that silently
        stopped forever because a sysfs path moved would be worse than one
        that ran. A metric that *was* read and breaches always withholds.
        """
        temperature = metrics.get("soc_temp_c")
        if isinstance(temperature, (int, float)) and temperature >= self.config.max_temp_c:
            return "hot"
        load = metrics.get("load_1m")
        if isinstance(load, (int, float)) and load >= self.config.max_load:
            return "loaded"
        available = metrics.get("mem_available_kib")
        if (isinstance(available, int)
                and available * 1024 < self.config.min_available_bytes):
            return "low_memory"
        return None

    def _probe_router(self) -> dict:
        if self._gateway is None:
            self._gateway = default_gateway(self._proc_root)
        measured = self._probe_ping(self._gateway)
        if measured["state"] in {STATE_FAILED, STATE_LOST}:
            # The route may have moved; re-read it so the next cycle recovers.
            self._gateway = None
            self._interface = None
        return measured

    def _probe_ping(self, target: str | None) -> dict:
        if not target:
            return {"state": STATE_UNCONFIGURED}
        output = self._run_child(ping_command(target, self.config.ping_count),
                                 timeout=self.ping_timeout)
        if output is None:
            # The child failed, timed out or overran its output cap. That is
            # not 100 % loss; reporting it as loss would invent evidence.
            return {"state": STATE_FAILED}
        return parse_ping(output)

    def _probe_internet(self) -> dict | None:
        """DNS, TCP connect and TLS to the plate service, timed separately.

        No HTTP request, so no lookup is billed and no quota is consumed
        against the one-request-per-second throttle.
        """
        now = self._clock()
        if (self._internet_at is not None and self._internet is not None
                and now - self._internet_at < self.config.tls_interval_seconds):
            return {**self._internet,
                    "age_seconds": round(max(0.0, now - self._internet_at), 1)}
        try:
            timings = self._internet_connect(self.config.tls_host)
        except Exception:
            timings = None
        if isinstance(timings, dict) and timings.get("total_ms") is not None:
            self._internet = {"state": STATE_OK, **timings}
        else:
            self._internet = {"state": STATE_FAILED}
        self._internet_at = now
        return {**self._internet, "age_seconds": 0.0}

    def _probe_interface(self) -> dict | None:
        """Counter *rates* over this cycle. Lifetime totals are never reported.

        ``rx_dropped`` rising while ``rx_errors`` stays at zero is usually the
        kernel discarding frames no socket wanted, not a damaged link, so both
        are reported and neither is summarised into a verdict here.
        """
        if self._interface is None:
            self._interface = default_interface(self._proc_root)
        counters = interface_counters(self._proc_root, self._interface)
        now = self._clock()
        if counters is None:
            self._counters = None
            return None
        previous = self._counters
        self._counters = (now, self._interface, counters)
        if previous is None or previous[1] != self._interface:
            # No baseline, or the default route moved to another interface, so
            # there is no interval to difference over yet.
            return None
        elapsed = now - previous[0]
        if elapsed <= 0:
            return None
        deltas = {field: counters[field] - previous[2].get(field, 0)
                  for field in counters}
        if any(delta < 0 for delta in deltas.values()):
            # A counter wrap or an interface reset; report nothing rather than
            # a nonsense rate.
            return None
        measured = {
            "name": self._interface,
            "receive_bytes_per_s": round(deltas["receive_bytes"] / elapsed, 1),
            "transmit_bytes_per_s": round(deltas["transmit_bytes"] / elapsed, 1),
            "receive_packets_per_s": round(deltas["receive_packets"] / elapsed, 1),
            "transmit_packets_per_s": round(deltas["transmit_packets"] / elapsed, 1),
            "receive_dropped_per_s": round(deltas["receive_dropped"] / elapsed, 2),
            "receive_errors_per_s": round(deltas["receive_errors"] / elapsed, 2),
            "transmit_errors_per_s": round(deltas["transmit_errors"] / elapsed, 2),
        }
        arriving = deltas["receive_packets"] + deltas["receive_dropped"]
        if arriving > 0:
            measured["receive_dropped_pct"] = round(
                100.0 * deltas["receive_dropped"] / arriving, 3
            )
        speed = link_speed_mbps(self._interface, self._sys_class_net)
        if speed is not None:
            measured["link_mbps"] = speed
        return measured

    # -- the one child process --------------------------------------------

    def _run_child(self, command, *, timeout: float | None = None) -> str | None:
        """Run one bounded, address-space-limited child, or return None.

        The caller already holds the single-flight slot, so at most one child
        exists across the whole worker.
        """
        try:
            process = self._popen(
                tuple(command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                preexec_fn=_limit_address_space,
            )
        except (OSError, ValueError):
            return None
        with self._lock:
            if self._closed:
                stopping = True
            else:
                stopping = False
                self._process = process
        if stopping:
            _terminate(process)
            return None
        try:
            output = self._read_bounded(process, timeout or self.child_timeout)
        finally:
            _terminate(process)
            with self._lock:
                if self._process is process:
                    self._process = None
        return output

    def _read_bounded(self, process, timeout: float) -> str | None:
        """Read at most MAX_CHILD_OUTPUT_BYTES, never past the deadline."""
        deadline = self._clock() + min(timeout, MAX_CHILD_TIMEOUT_SECONDS)
        buffer = bytearray()
        try:
            descriptor = process.stdout.fileno()
        except (AttributeError, OSError, ValueError):
            return None
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return None
            try:
                ready, _, _ = select.select([descriptor], [], [], remaining)
            except (OSError, ValueError):
                return None
            if not ready:
                continue
            capacity = MAX_CHILD_OUTPUT_BYTES - len(buffer)
            try:
                chunk = os.read(descriptor, min(4096, capacity + 1))
            except OSError:
                return None
            if not chunk:
                break
            if len(chunk) > capacity:
                return None
            buffer.extend(chunk)
        return buffer.decode("utf-8", "replace")


def tls_context() -> ssl.SSLContext:
    """A verifying context pinned to TLS 1.2 or better.

    `ssl.create_default_context()` alone leaves TLS 1.0 and 1.1 reachable on
    some builds. This probe exists to measure a handshake, so the handshake it
    measures must be one the OCR client would actually be willing to make;
    a number obtained over a protocol we would refuse is worse than no number.
    """
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _internet_timings(host: str, *, port: int = 443,
                      timeout: float = CHILD_TIMEOUT_SECONDS) -> dict | None:
    """Time DNS, TCP connect and TLS separately for one end-to-end open.

    Split because the phases fail differently: a slow resolver, a lossy path
    that stretches the connect, and a saturated uplink that stretches the
    handshake all show up as one number otherwise. No HTTP request is made,
    so nothing is billed and no quota is consumed.
    """
    context = tls_context()
    started = time.perf_counter()
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return None
    if not addresses:
        return None
    resolved_at = time.perf_counter()
    family, kind, protocol, _, address = addresses[0]
    connection = None
    try:
        connection = socket.socket(family, kind, protocol)
        connection.settimeout(timeout)
        connection.connect(address)
        connected_at = time.perf_counter()
        with context.wrap_socket(connection, server_hostname=host):
            finished_at = time.perf_counter()
    except Exception:
        return None
    finally:
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
    return {
        "dns_ms": round((resolved_at - started) * 1000, 2),
        "connect_ms": round((connected_at - resolved_at) * 1000, 2),
        "tls_ms": round((finished_at - connected_at) * 1000, 2),
        "total_ms": round((finished_at - started) * 1000, 2),
    }
