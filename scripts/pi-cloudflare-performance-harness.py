#!/usr/bin/env python3
"""Capture local gate-controller performance and host metrics on a Pi."""

import argparse
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


MAX_PROC_NET_DEV_BYTES = 64 * 1024
MAX_NETWORK_INTERFACES = 32
PATHS_URL = "http://127.0.0.1:9997/v3/paths/list"
METRICS_URL = "http://127.0.0.1:9998/metrics"
_READ_ONLY_ENDPOINTS = frozenset((PATHS_URL, METRICS_URL))


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        raise HTTPError(
            request.full_url, code, "redirect response rejected", headers, file_pointer
        )


_URL_OPENER = build_opener(ProxyHandler({}), _RejectRedirects())


def parse_args(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("gate-pi-performance.json"))
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--skip-network", action="store_true")
    return parser.parse_args(arguments)


def measure_request(url, *, timeout_seconds=5.0):
    if url not in _READ_ONLY_ENDPOINTS:
        raise ValueError("performance harness endpoint is not allowlisted")
    request = Request(url, method="GET")
    started = time.perf_counter()
    sample = {"url": url, "method": "GET"}
    try:
        with _URL_OPENER.open(request, timeout=timeout_seconds) as response:
            response.read()
            sample["status_code"] = response.status
    except HTTPError as error:
        error.read()
        sample["status_code"] = error.code
    except (OSError, URLError) as error:
        sample["error"] = str(error)
    sample["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return sample


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


def build_summary(*, samples, run_mode, host_metrics=None):
    if run_mode not in {"host_metrics_only", "passive_endpoint_probe"}:
        raise ValueError("invalid performance harness run mode")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "samples": samples,
        "host_metrics": host_metrics if host_metrics is not None else collect_host_metrics(),
        "run_mode": run_mode,
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
            PATHS_URL, timeout_seconds=args.timeout_seconds,
        ))
        samples.append(measure_request(
            METRICS_URL, timeout_seconds=args.timeout_seconds,
        ))
    run_mode = "host_metrics_only" if args.skip_network else "passive_endpoint_probe"
    summary = build_summary(
        samples=samples,
        run_mode=run_mode,
        host_metrics=collect_host_metrics(),
    )
    write_json(args.output, summary)
    return summary


if __name__ == "__main__":
    main()
