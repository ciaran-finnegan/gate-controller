# Pi Cloudflare Performance Harness

Run this harness on the gate controller host to capture local command-server and
media-gateway latency alongside CPU load, memory, and root disk usage. It does
not require SSH access to the Pi when run locally.

## Safe Collection

The default command measures the local command server and MediaMTX API health
URL using only passive `GET` requests. The command-server GET returns its
normal 404 response; that response is still a useful loopback availability and
latency measurement. The media URL defaults to the local MediaMTX paths API.
The harness never sends a command.

```bash
.venv/bin/python scripts/pi-cloudflare-performance-harness.py \
  --output /tmp/gate-pi-perf.json
```

Use `--skip-network` to collect only host metrics. This is the intended command
for local development and any environment where local services are unavailable:

```bash
.venv/bin/python scripts/pi-cloudflare-performance-harness.py \
  --skip-network --output /tmp/gate-pi-perf.json
```

The JSON summary records how it was collected. The safe default is:

```json
{"run_mode": "passive_endpoint_probe"}
```

`--skip-network` reports `host_metrics_only`. Running the harness through SSH
does not change its schema; the probes themselves execute on the Pi.

Both endpoint URLs and the request timeout are configurable with
`--command-url`, `--media-health-url`, and `--timeout-seconds`.
