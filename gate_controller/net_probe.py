"""A governed, single-flight network probe for the controller heartbeat.

On 2026-09-06 the powerline bridge degraded to ~210 ms RTT, OCR round trips
went to 3.1-3.2 s and a vehicle was denied; the heartbeat showed nothing. This
worker measures that link, and it is deliberately built so it can never repeat
the failure it exists to catch.

The bounds, all of them load-bearing:

* one extra thread on a 60 s poll inside the existing controller process, with
  no new systemd unit;
* a ``BoundedSemaphore(1)`` so at most one child process is ever alive;
* a governor checked before every cycle -- skip entirely at 80 C or hotter, at
  a one-minute load average of 3.0 or higher, or under 300 MB available -- and
  the reason is recorded so the gap in the series is explicit, not silent;
* every child gets ``RLIMIT_AS`` of 64 MiB, a 3 s deadline, a capped stdout,
  ``stderr`` to ``/dev/null``, and a kill-and-reap on the way out; and
* **permanently forbidden in this module**: ffmpeg, any video or image decode,
  numpy, onnxruntime, any model load, ``journalctl``, any throughput test, and
  any unbounded read.

There is deliberately no speed test. The uplink is about 4.5 Mbit/s and OCR
uploads already saturate it, so a throughput probe would compete with the
thing it is meant to measure; effective throughput is derived instead from the
``/proc/net/dev`` delta and the ``upload_bytes`` the OCR client already logs.

Nothing here may block, lock or delay the relay path, and every failure is
swallowed and reported as a field value.
"""
import logging
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
CHILD_TIMEOUT_SECONDS = 3.0
CHILD_ADDRESS_SPACE_BYTES = 64 * 1024 * 1024
MAX_CHILD_OUTPUT_BYTES = 8 * 1024
MAX_NETWORK_INTERFACES = 32
PING_COUNT = 5
INTERFACE_NAME_MAX = 16
HOSTNAME_MAX = 253


@dataclass(frozen=True)
class NetProbeConfig:
    enabled: bool = False
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    tls_interval_seconds: float = DEFAULT_TLS_INTERVAL_SECONDS
    tls_host: str = DEFAULT_TLS_HOST
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


def load_net_probe_config(environment=None) -> NetProbeConfig:
    """Read the probe configuration, rejecting anything outside safe bounds.

    The default is enabled: the governor is what makes "on" defensible on a
    board with roughly six degrees of thermal headroom, and a probe that is
    off by default would not have caught the powerline degradation.
    """
    environment = os.environ if environment is None else environment
    enabled = str(environment.get("GATE_NET_PROBE_ENABLED", "true")).strip().lower()
    if enabled not in {"true", "false", "1", "0", "yes", "no", ""}:
        raise ValueError("GATE_NET_PROBE_ENABLED must be true or false")
    host = str(environment.get("GATE_NET_PROBE_TLS_HOST", DEFAULT_TLS_HOST)).strip()
    if not host or len(host) > HOSTNAME_MAX or any(character.isspace() for character in host):
        raise ValueError("GATE_NET_PROBE_TLS_HOST must be a hostname")
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


def interface_counters(proc_root=Path("/proc"), interface: str | None = None) -> tuple[int, int] | None:
    """(receive_bytes, transmit_bytes) for one interface, or None."""
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
            return int(values[0]), int(values[8])
        except ValueError:
            return None
    return None


def parse_ping(output: str) -> dict:
    """Loss and round-trip time from ``ping -q`` statistics.

    100 % loss is a measurement, not an error: the probe must report
    ``loss = 1.0`` rather than raising when the powerline bridge is unplugged.
    """
    result: dict = {"probed": True, "loss": None, "rtt_ms": None}
    for line in output.splitlines():
        if "packets transmitted" in line:
            fields = line.replace(",", " ").split()
            try:
                transmitted = int(fields[0])
                received = int(fields[fields.index("received") - 1])
            except (IndexError, ValueError):
                continue
            if transmitted > 0:
                result["loss"] = round(max(0.0, (transmitted - received) / transmitted), 3)
        elif line.strip().startswith(("rtt ", "round-trip ")) and "=" in line:
            numbers = line.split("=", 1)[1].strip().split()[0].split("/")
            if len(numbers) >= 2:
                try:
                    result["rtt_ms"] = round(float(numbers[1]), 2)
                except ValueError:
                    continue
    return result


class NetProbeWorker:
    """One thread, one child at a time, and a governor in front of both."""

    def __init__(self, config: NetProbeConfig, *, popen=subprocess.Popen,
                 clock=time.monotonic, host_metrics=read_host_metrics,
                 proc_root=Path("/proc"), tls_connect=None,
                 child_timeout: float = CHILD_TIMEOUT_SECONDS):
        self.config = config
        self.child_timeout = child_timeout
        self._popen = popen
        self._clock = clock
        self._host_metrics = host_metrics
        self._proc_root = Path(proc_root)
        self._tls_connect = tls_connect or _tls_handshake_ms
        self._slot = BoundedSemaphore(1)
        self._lock = Lock()
        self._closed = False
        self._process = None
        self._gateway: str | None = None
        self._interface: str | None = None
        self._counters: tuple[float, int, int] | None = None
        self._tls: dict | None = None
        self._tls_measured_at: float | None = None
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
        """The firmware throttle word from the last cycle, or None."""
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
        skipped = self._governor_verdict()
        if skipped is not None:
            LOGGER.info("gate_net_probe outcome=skipped reason=%s", skipped)
            self._publish({"enabled": True, "probed": False, "skipped_reason": skipped})
            return False
        router = self._probe_router()
        if router.get("rtt_ms") is None and router.get("loss") in (None, 1.0):
            # The route may have moved; re-read it so the next cycle recovers.
            self._gateway = None
            self._interface = None
        tls = self._probe_tls()
        uplink = self._probe_uplink()
        self._throttled = read_throttled_flags(run_command=self._run_child) or self._throttled
        measured: dict = {"enabled": True, "probed": True, "skipped_reason": None,
                          "router": router}
        if tls is not None:
            measured["tls"] = tls
        if uplink is not None:
            measured["uplink"] = uplink
        self._publish(measured)
        return True

    def _publish(self, measured: dict) -> None:
        with self._lock:
            self._status = measured
            self._status_at = self._clock()

    def _governor_verdict(self) -> str | None:
        """Why this cycle must not run, or None.

        A metric that could not be read is permissive: a probe that silently
        stopped forever because a sysfs path moved would be worse than one
        that ran. A metric that *was* read and breaches always skips.
        """
        try:
            metrics = self._host_metrics()
        except Exception:
            return None
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
        if self._gateway is None:
            return {"probed": False, "loss": None, "rtt_ms": None}
        output = self._run_child((
            "ping", "-n", "-q", "-c", str(PING_COUNT), "-i", "0.2", "-W", "1",
            self._gateway,
        ))
        if output is None:
            return {"probed": True, "loss": 1.0, "rtt_ms": None}
        return parse_ping(output)

    def _probe_tls(self) -> dict | None:
        now = self._clock()
        if (self._tls_measured_at is not None
                and now - self._tls_measured_at < self.config.tls_interval_seconds):
            return self._tls
        try:
            handshake_ms = self._tls_connect(self.config.tls_host)
        except Exception:
            handshake_ms = None
        self._tls = {"handshake_ms": handshake_ms, "ok": handshake_ms is not None}
        self._tls_measured_at = now
        return self._tls

    def _probe_uplink(self) -> dict | None:
        if self._interface is None:
            self._interface = default_interface(self._proc_root)
        counters = interface_counters(self._proc_root, self._interface)
        now = self._clock()
        if counters is None:
            self._counters = None
            return None
        previous = self._counters
        self._counters = (now, counters[0], counters[1])
        if previous is None:
            return None
        elapsed = now - previous[0]
        if elapsed <= 0:
            return None
        received = counters[0] - previous[1]
        transmitted = counters[1] - previous[2]
        if received < 0 or transmitted < 0:
            # A counter wrap or an interface reset; report nothing rather than
            # a nonsense rate.
            return None
        return {
            "interface": self._interface,
            "receive_bytes_per_s": round(received / elapsed, 1),
            "transmit_bytes_per_s": round(transmitted / elapsed, 1),
        }

    # -- the one child process --------------------------------------------

    def _run_child(self, command) -> str | None:
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
            output = self._read_bounded(process)
        finally:
            _terminate(process)
            with self._lock:
                if self._process is process:
                    self._process = None
        return output

    def _read_bounded(self, process) -> str | None:
        """Read at most MAX_CHILD_OUTPUT_BYTES, never past the 3 s deadline."""
        deadline = self._clock() + self.child_timeout
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


def _tls_handshake_ms(host: str, *, port: int = 443,
                      timeout: float = CHILD_TIMEOUT_SECONDS) -> float | None:
    """Time one TLS handshake and close. No HTTP request, no lookup billed."""
    context = tls_context()
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host):
                pass
    except Exception:
        return None
    return round((time.perf_counter() - started) * 1000, 2)
