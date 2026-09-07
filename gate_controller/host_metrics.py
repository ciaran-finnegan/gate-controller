"""Bounded ``/proc`` and ``/sys`` reads describing the controller host.

The gate controller runs on a fanless Pi 5 that idles at 71-74 C, works to a
ceiling of 80 C and hardware-throttles at 85 C. On 2026-09-07 a software 4K
decode drove the board into the OOM killer and took the controller off the
network for about twenty-five minutes, and the heartbeat reported nothing at
all. This module is the instrument pointed at that headroom, so it is built
to be incapable of causing the failure it measures:

* every read is a bounded read of a virtual file, and
* nothing here decodes video or images, imports numpy or onnxruntime, loads a
  model, shells out to ``journalctl``, or runs a throughput test.

The single subprocess in the host path is ``vcgencmd get_throttled``, and it
lives behind :func:`read_throttled_flags`, which the governed network probe
calls at most once a cycle. Every failure is swallowed and reported as an
absent field: a heartbeat that loses a metric must still go out.
"""
import os
import shutil
from pathlib import Path

# /proc/meminfo and /proc/vmstat are the largest files read here and are a few
# kilobytes on this kernel. The cap exists so a pathological /proc cannot be
# read into memory on a board with 300 MB of headroom.
MAX_PROC_BYTES = 64 * 1024
DEFAULT_THERMAL_ZONE = Path("/sys/class/thermal/thermal_zone0/temp")
DEFAULT_STORAGE_DEVICE = "mmcblk0"
# Present on some Raspberry Pi kernels; when it is, the vcgencmd subprocess is
# never spawned at all.
FIRMWARE_THROTTLED = Path("/sys/devices/platform/soc/soc:firmware/get_throttled")
THROTTLED_BITS = {
    "under_voltage": 0x1,
    "arm_capped": 0x2,
    "currently_throttled": 0x4,
    "soft_temp_limit": 0x8,
}


def read_proc_value(path, *, max_bytes: int = MAX_PROC_BYTES) -> str | None:
    """Read a virtual file, or return None on any failure or overrun.

    Lifted from ``scripts/pi-cloudflare-performance-harness.py`` so the
    heartbeat and the harness agree about what a bounded read is.
    """
    try:
        with Path(path).open("r", encoding="utf-8") as source:
            content = source.read(max_bytes + 1)
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return content if len(content) <= max_bytes else None


def _soc_temp_c(path) -> float | None:
    raw = read_proc_value(path, max_bytes=64)
    if raw is None:
        return None
    try:
        return round(int(raw.strip()) / 1000.0, 1)
    except ValueError:
        return None


def _load_average(proc_root: Path) -> dict:
    raw = read_proc_value(proc_root / "loadavg", max_bytes=256)
    if raw is None:
        return {}
    values = raw.split()
    if len(values) < 3:
        return {}
    try:
        return {
            "load_1m": float(values[0]),
            "load_5m": float(values[1]),
            "load_15m": float(values[2]),
        }
    except ValueError:
        return {}


def _memory(proc_root: Path) -> dict:
    raw = read_proc_value(proc_root / "meminfo")
    if raw is None:
        return {}
    wanted = {
        "MemTotal": "mem_total_kib",
        "MemAvailable": "mem_available_kib",
        "SwapFree": "swap_free_kib",
    }
    metrics = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        field = wanted.get(key.strip())
        if field is None:
            continue
        parts = value.split()
        if parts and parts[0].isdigit():
            metrics[field] = int(parts[0])
    return metrics


def _disk(mount_point) -> dict:
    try:
        usage = shutil.disk_usage(mount_point)
    except (OSError, ValueError):
        return {}
    return {"disk_free_bytes": usage.free, "disk_total_bytes": usage.total}


def _oom_kill_total(proc_root: Path) -> int | None:
    """The kernel's monotonic OOM-kill counter.

    This is the exact signal for the 2026-09-07 failure and needs no threshold:
    any increase means the kernel killed something on this board.
    """
    raw = read_proc_value(proc_root / "vmstat")
    if raw is None:
        return None
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "oom_kill" and parts[1].isdigit():
            return int(parts[1])
    return None


def _uptime_seconds(proc_root: Path) -> float | None:
    raw = read_proc_value(proc_root / "uptime", max_bytes=256)
    if raw is None:
        return None
    parts = raw.split()
    if not parts:
        return None
    try:
        return round(float(parts[0]), 1)
    except ValueError:
        return None


def _process_uptime_seconds(proc_root: Path, uptime: float | None) -> float | None:
    """How long this controller process has been running.

    A decrease means ``file-monitor.service`` restarted, which the 15 s
    heartbeat cannot otherwise distinguish from a board that never rebooted.
    """
    if uptime is None:
        return None
    raw = read_proc_value(proc_root / "self" / "stat", max_bytes=4096)
    if raw is None:
        return None
    # The comm field is parenthesised and may contain spaces, so fields are
    # counted from after the final ')'. Field 22 (starttime) is index 19 there.
    closing = raw.rfind(")")
    if closing == -1:
        return None
    fields = raw[closing + 1:].split()
    if len(fields) < 20:
        return None
    try:
        ticks_per_second = os.sysconf("SC_CLK_TCK")
        started_at = int(fields[19]) / float(ticks_per_second)
    except (OSError, ValueError, ZeroDivisionError, AttributeError):
        return None
    age = uptime - started_at
    return round(age, 1) if age >= 0 else None


def _disk_sectors_written(proc_root: Path, device: str) -> int | None:
    """A cumulative-write proxy for SD wear.

    Consumer SD cards expose no JEDEC life-time percentage, so there is no
    honest "card health" number to report; this counter is the closest real
    measurement and is only meaningful as a trend.
    """
    raw = read_proc_value(proc_root / "diskstats")
    if raw is None:
        return None
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 10 or parts[2] != device:
            continue
        return int(parts[9]) if parts[9].isdigit() else None
    return None


def decode_throttled(raw: str | None) -> dict | None:
    """Decode a firmware throttled word into the raw hex plus booleans."""
    if not raw:
        return None
    text = raw.strip().split("=")[-1].strip()
    if not text or len(text) > 32:
        return None
    try:
        value = int(text, 16 if text.lower().startswith("0x") else 10)
    except ValueError:
        return None
    if value < 0:
        return None
    flags = {"raw": f"0x{value:x}"}
    for name, bit in THROTTLED_BITS.items():
        flags[name] = bool(value & bit)
    return flags


def read_throttled_flags(*, run_command=None,
                         firmware_path=FIRMWARE_THROTTLED) -> dict | None:
    """The firmware throttle word, from sysfs when the kernel exposes it.

    ``run_command`` is the governed bounded child runner supplied by the
    network probe. Without it only sysfs is consulted, so importing this
    module can never spawn a process.
    """
    decoded = decode_throttled(read_proc_value(firmware_path, max_bytes=64))
    if decoded is not None:
        return decoded
    if run_command is None:
        return None
    try:
        output = run_command(("vcgencmd", "get_throttled"))
    except Exception:
        return None
    return decode_throttled(output)


def read_host_metrics(*, proc_root=Path("/proc"), thermal_zone=DEFAULT_THERMAL_ZONE,
                      mount_point="/", storage_device: str = DEFAULT_STORAGE_DEVICE,
                      throttled: dict | None = None) -> dict:
    """Every host metric that could be read, and nothing for the rest.

    An absent field means the read failed. Callers must render that as
    "unknown" rather than substituting a healthy-looking default.
    """
    proc_root = Path(proc_root)
    metrics: dict = {}
    temperature = _soc_temp_c(thermal_zone)
    if temperature is not None:
        metrics["soc_temp_c"] = temperature
    metrics.update(_load_average(proc_root))
    metrics.update(_memory(proc_root))
    metrics.update(_disk(mount_point))
    oom_kills = _oom_kill_total(proc_root)
    if oom_kills is not None:
        metrics["oom_kill_total"] = oom_kills
    uptime = _uptime_seconds(proc_root)
    if uptime is not None:
        metrics["uptime_seconds"] = uptime
    process_uptime = _process_uptime_seconds(proc_root, uptime)
    if process_uptime is not None:
        metrics["process_uptime_seconds"] = process_uptime
    sectors = _disk_sectors_written(proc_root, storage_device)
    if sectors is not None:
        metrics["disk_sectors_written"] = sectors
    if throttled:
        metrics["throttled"] = dict(throttled)
    return metrics
