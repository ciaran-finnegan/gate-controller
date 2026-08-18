# Pi Cloudflare Performance Harness

Run this harness on the gate controller host to capture local command-server and
media-gateway latency alongside CPU load, memory, and root disk usage. It does
not require SSH access to the Pi when run locally.

## Safe Collection

The default command measures the local command server with a non-actuating GET
request and the MediaMTX API health URL. The command-server GET returns its
normal 404 response; that response is still a useful loopback availability and
latency measurement. The media URL defaults to the local MediaMTX paths API.

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

The JSON summary always records:

```json
"pi_ssh_tests": "skipped_until_tailscale_or_home_wifi"
```

This task deliberately does not execute Pi SSH, relay, or media load tests.

## Actuation Guardrail

No relay command is sent unless `--actuate` is explicitly provided. When it is,
the harness sends one `open_gate` command to `--command-url`; use it only while
physically present and prepared for the gate to open. `--controller-id` defaults
to `primary` and can be changed to match the local controller configuration.

```bash
.venv/bin/python scripts/pi-cloudflare-performance-harness.py \
  --actuate --controller-id primary --output /tmp/gate-pi-perf.json
```

Both endpoint URLs and the request timeout are configurable with
`--command-url`, `--media-health-url`, and `--timeout-seconds`.
