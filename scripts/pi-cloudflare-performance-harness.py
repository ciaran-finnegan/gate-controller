#!/usr/bin/env python3
"""Capture local gate-controller performance and host metrics on a Pi."""

import argparse
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


DEFERRED_PI_STATUS = "skipped_until_tailscale_or_home_wifi"
MAX_PROC_NET_DEV_BYTES = 64 * 1024
MAX_NETWORK_INTERFACES = 32


def parse_args(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command-url", default="http://127.0.0.1:8765/commands")
    parser.add_argument("--media-health-url", default="http://127.0.0.1:9997/v3/paths/list")
    parser.add_argument("--output", type=Path, default=Path("gate-pi-performance.json"))
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--skip-network", action="store_true")
    parser.add_argument(
        "--actuate",
        action="store_true",
        help="send one real open_gate command; omitted by default",
    )
    parser.add_argument("--controller-id", default="primary")
    return parser.parse_args(arguments)


def measure_request(url, *, method="GET", body=None, timeout_seconds=5.0):
    request = Request(url, data=body, method=method)
    started = time.perf_counter()
    sample = {"url": url, "method": method}
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            response.read()
            sample["status_code"] = response.status
    except HTTPError as error:
        error.read()
        sample["status_code"] = error.code
    except (OSError, URLError) as error:
        sample["error"] = str(error)
    sample["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return sample


def _actuation_body(controller_id):
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=5)
    return json.dumps({
        "controller_id": controller_id,
        "command": "open_gate",
        "idempotency_key": f"performance-harness-{uuid4()}",
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
    }).encode("utf-8")


def _read_proc_value(path, *, max_bytes=1024 * 1024):
    try:
        with Path(path).open("r", encoding="utf-8") as source:
            content = source.read(max_bytes + 1)
    except (OSError, UnicodeDecodeError):
        return None
    return content if len(content) <= max_bytes else None


def _network_counters(content):
    counters = {}
    for line in content.splitlines()[2:2 + MAX_NETWORK_INTERFACES]:
        if ":" not in line:
            continue
        interface, values_text = line.split(":", 1)
        interface = interface.strip()
        values = values_text.split()
        if not interface or len(interface) > 64 or len(values) != 16:
            continue
        try:
            numbers = [int(value) for value in values]
        except ValueError:
            continue
        counters[interface] = {
            "receive_bytes": numbers[0],
            "receive_packets": numbers[1],
            "transmit_bytes": numbers[8],
            "transmit_packets": numbers[9],
        }
    return counters


def collect_host_metrics():
    metrics = {"disk": shutil.disk_usage("/")._asdict()}
    loadavg = _read_proc_value("/proc/loadavg")
    if loadavg is not None:
        values = loadavg.split()
        metrics["loadavg"] = {
            "one_minute": float(values[0]),
            "five_minutes": float(values[1]),
            "fifteen_minutes": float(values[2]),
        }
    meminfo = _read_proc_value("/proc/meminfo")
    if meminfo is not None:
        metrics["memory_kib"] = {
            key.rstrip(":"): int(value.split()[0])
            for line in meminfo.splitlines()
            if ":" in line
            for key, value in [line.split(":", 1)]
            if value.split() and value.split()[0].isdigit()
        }
    network = _read_proc_value("/proc/net/dev", max_bytes=MAX_PROC_NET_DEV_BYTES)
    if network is not None:
        metrics["network"] = _network_counters(network)
    return metrics


def build_summary(*, samples, skipped_pi, host_metrics=None):
    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "samples": samples,
        "host_metrics": host_metrics if host_metrics is not None else collect_host_metrics(),
        "pi_ssh_tests": DEFERRED_PI_STATUS if skipped_pi else "not_requested",
    }


def write_json(output, summary):
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=output.parent, prefix=f".{output.name}.", delete=False
    ) as temporary:
        json.dump(summary, temporary, indent=2, sort_keys=True)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, output)


def main(arguments=None):
    args = parse_args(arguments)
    samples = []
    if not args.skip_network:
        samples.append(measure_request(
            args.command_url, timeout_seconds=args.timeout_seconds,
        ))
        samples.append(measure_request(
            args.media_health_url, timeout_seconds=args.timeout_seconds,
        ))
        if args.actuate:
            samples.append(measure_request(
                args.command_url,
                method="POST",
                body=_actuation_body(args.controller_id),
                timeout_seconds=args.timeout_seconds,
            ))
    summary = build_summary(
        samples=samples,
        skipped_pi=True,
        host_metrics=collect_host_metrics(),
    )
    write_json(args.output, summary)
    return summary


if __name__ == "__main__":
    main()
