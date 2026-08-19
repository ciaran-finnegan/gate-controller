# Gate Controller Cloudflare Replatform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the controller's Supabase/AWS-facing cloud integrations with Cloudflare Worker, Access service-token, and Tunnel integrations while preserving local-first relay safety.

**Architecture:** Keep Plate Recognizer, local SQLite, `GateProcessor`, `ActuationCoordinator`, evidence spool, and relay control as the safety core. Add a Cloudflare HTTP client for plate/status/outbox calls and a loopback-only command server exposed by Cloudflare Tunnel; remove Supabase polling and legacy AWS/S3 runtime paths after tests prove parity.

**Tech Stack:** Python 3.10+, unittest, requests, Pillow, watchdog, SQLite, systemd, Cloudflare Access service-token headers, cloudflared config/docs, existing media gateway.

**Spec:** `docs/superpowers/specs/2026-08-17-cloudflare-replatform-design.md`

## Global Constraints

- Recognition and relay decisions never wait for optional cloud services after Plate Recognizer succeeds.
- Cloud-managed plate snapshots fail closed after a bounded staleness window.
- Remote commands have durable idempotency and expiry checks under the relay lock.
- Media gateway code remains isolated from controller and relay imports.
- Replace Supabase plate/status/command/outbox clients with Cloudflare Worker clients.
- Remove legacy AWS/S3 upload paths.
- Add a minimal loopback-only command endpoint for Cloudflare Tunnel.
- Authenticate controller-to-Worker requests with Cloudflare Access service credentials.
- Authenticate Worker-to-Pi direct commands through an Access-protected Tunnel service token.
- Keep Tailscale only for break-glass administration.
- Add a committed performance-test harness and runbook, but do not run Pi performance tests until the Pi is reachable.
- TDD is mandatory for production behavior changes.
- Baseline: 356 unittest tests pass with 2 expected skips; compileall and shell syntax checks pass.

---

## File Structure

- Create `gate_controller/cloudflare_client.py`: bounded HTTPS/loopback HTTP helper, Access service-token headers, plate fetcher, status client, outbox sender.
- Create `gate_controller/command_server.py`: loopback HTTP server for direct Tunnel commands.
- Modify `gate_controller/control_plane.py`: keep shared `GateCommand` and `CommandWorker` behavior where useful; remove Supabase-specific RPC client after replacement.
- Modify `gate_controller/authorisation.py`: replace or alias `SupabasePlateFetcher` with `CloudflarePlateFetcher`.
- Modify `gate_controller/outbox.py`: use Cloudflare Access headers through new sender, keep evidence semantics.
- Modify `gate_controller/__main__.py`: environment validation and background worker wiring.
- Modify `gate_controller/runtime.py`: allow HTTPS service URLs and explicit loopback local test URLs.
- Modify `deployment/install.sh`: validate Cloudflare env pairs and reject mixed Supabase/Cloudflare config.
- Create `deployment/cloudflared/gate-controller-tunnel.yml`: locally managed tunnel example with command/media routes and catch-all.
- Create `deployment/systemd/gate-command-server.service` and update install docs if the command server is separate from the main process.
- Create `scripts/pi-cloudflare-performance-harness.py`: records local endpoint latency and host metrics without requiring remote SSH.
- Modify `README.md`, `docs/deployment.md`, `docs/reolink-rlc-811a.md`: Cloudflare cutover docs.
- Remove `s3_utils.py` and `test_upload_image_to_s3.py` after tests prove no runtime import.

---

### Task 1: Add Cloudflare Service URL And Access Header Client

**Files:**
- Create: `gate_controller/cloudflare_client.py`
- Modify: `gate_controller/runtime.py`
- Test: `tests/test_cloudflare_client.py`
- Test: `tests/test_runtime.py`

**Interfaces:**
- Produces: `CloudflareServiceClient(base_url, client_id, client_secret, session=None, timeout=(2, 4))`
- Produces: `require_https_or_loopback_service_url(url, name) -> str`
- Consumes: `requests.Session`

- [ ] **Step 1: Write failing client tests**

```python
def test_cloudflare_client_uses_access_service_headers_and_bounded_timeout(self):
    session = RecordingSession()
    client = CloudflareServiceClient(
        "https://gate.example.com", "client-id", "client-secret",
        session=session, timeout=(1, 2),
    )

    client.get_json("/api/controller/plates")

    self.assertEqual(session.requests[0].headers["CF-Access-Client-Id"], "client-id")
    self.assertEqual(session.requests[0].headers["CF-Access-Client-Secret"], "client-secret")
    self.assertEqual(session.requests[0].timeout, (1, 2))

def test_cloudflare_client_rejects_plain_http_except_loopback(self):
    with self.assertRaisesRegex(ValueError, "GATE_CLOUDFLARE_API_URL"):
        CloudflareServiceClient("http://gate.example.com", "id", "secret")
    CloudflareServiceClient("http://127.0.0.1:8787", "id", "secret")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_cloudflare_client -v`

Expected: FAIL because `gate_controller.cloudflare_client` does not exist.

- [ ] **Step 3: Implement minimal client**

Implement `get_json()` and `post_json()` with status-code checks, JSON parsing, bounded timeout, absolute path enforcement, and no logging of secrets.

- [ ] **Step 4: Verify**

Run:

```bash
.venv/bin/python -m unittest tests.test_cloudflare_client tests.test_runtime -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add gate_controller/cloudflare_client.py gate_controller/runtime.py tests/test_cloudflare_client.py tests/test_runtime.py
git commit -m "feat: add Cloudflare Access service client"
```

---

### Task 2: Replace Supabase Plate, Heartbeat, And Outbox Clients

**Files:**
- Modify: `gate_controller/cloudflare_client.py`
- Modify: `gate_controller/authorisation.py`
- Modify: `gate_controller/outbox.py`
- Test: `tests/test_authorisation.py`
- Test: `tests/test_outbox.py`
- Test: `tests/test_control_plane.py`

**Interfaces:**
- Produces: `CloudflarePlateFetcher(client, controller_id) -> list[dict]`
- Produces: `CloudflareStatusReporter(client, controller_id).heartbeat(status) -> None`
- Produces: `CloudflareOutboxSender(client, controller_id).__call__(payload, evidence_bytes=None) -> None`
- Consumes: `AuthorisationRefreshWorker`, `HeartbeatWorker`, `OutboxWorker`

- [ ] **Step 1: Write failing fetcher/status/outbox tests**

Add tests:

```python
def test_cloudflare_plate_fetcher_reads_worker_snapshot_with_controller_id(self):
    client = FakeClient(json_response={"plates": [{"plate": "241D123"}], "controller_id": "primary"})
    rows = CloudflarePlateFetcher(client, "primary")()
    self.assertEqual(rows, [{"plate": "241D123"}])
    self.assertEqual(client.requests[0].path, "/api/controller/plates?controller_id=primary")

def test_cloudflare_status_reporter_forwards_existing_heartbeat_contract(self):
    reporter = CloudflareStatusReporter(FakeClient(), "primary")
    reporter.heartbeat({"queue_depth": 2, "media": {"video": {"configured": False, "ready": False, "verified": False, "reason": "not_reported"}}})
    self.assertEqual(reporter.client.requests[0].path, "/api/controller/status")

def test_cloudflare_outbox_sender_preserves_idempotency_key_and_evidence_digest(self):
    sender = CloudflareOutboxSender(FakeClient(), "primary")
    sender({"event_id": 7, "controller_id": "primary", "image_sha256": sha}, evidence_bytes=image)
    self.assertEqual(sender.client.requests[0].headers["Idempotency-Key"], expected_digest)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_authorisation tests.test_outbox tests.test_control_plane -v`

Expected: FAIL because Cloudflare classes are absent.

- [ ] **Step 3: Implement classes**

Keep existing response validation and error types. `CloudflarePlateFetcher` accepts either `[{ plate }]` or `{ plates: [{ plate }] }` only when controller ID matches. `CloudflareOutboxSender` reuses the existing image object shape and idempotency digest.

- [ ] **Step 4: Verify**

Run:

```bash
.venv/bin/python -m unittest tests.test_authorisation tests.test_outbox tests.test_control_plane -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add gate_controller/cloudflare_client.py gate_controller/authorisation.py gate_controller/outbox.py tests
git commit -m "feat: add Cloudflare plate status and outbox clients"
```

---

### Task 3: Wire Cloudflare Background Workers And Environment Validation

**Files:**
- Modify: `gate_controller/__main__.py`
- Modify: `deployment/install.sh`
- Modify: `.gitignore` if generated runtime files appear during tests
- Test: `tests/test_main.py`
- Test: `tests/test_deployment.py`

**Interfaces:**
- Produces: `_cloudflare_configured(environment) -> bool`
- Produces: Cloudflare config creates `OutboxWorker`, `AuthorisationRefreshWorker`, and `HeartbeatWorker`
- Consumes: Task 2 Cloudflare classes

- [ ] **Step 1: Write failing config tests**

Add tests proving:

```python
def test_partial_cloudflare_configuration_fails_closed(self):
    for environment in [
        {"GATE_CLOUDFLARE_API_URL": "https://gate.example.com"},
        {"GATE_CLOUDFLARE_ACCESS_CLIENT_ID": "id", "GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET": "secret"},
    ]:
        with self.subTest(environment=environment):
            with self.assertRaisesRegex(ValueError, "GATE_CLOUDFLARE"):
                build_background_workers(store, relay=object(), environment=environment)

def test_cloudflare_configuration_builds_authorisation_status_and_outbox_workers(self):
    workers, _, status = build_background_workers(store, relay=object(), environment={
        "GATE_CLOUDFLARE_API_URL": "https://gate.example.com",
        "GATE_CLOUDFLARE_ACCESS_CLIENT_ID": "client-id",
        "GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET": "client-secret",
        "GATE_CONTROLLER_ID": "primary",
    }, authorised=authorised)
    self.assertEqual([type(worker).__name__ for worker in workers], ["OutboxWorker", "AuthorisationRefreshWorker", "HeartbeatWorker"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_main tests.test_deployment -v`

Expected: FAIL because Cloudflare env is not recognized.

- [ ] **Step 3: Implement environment wiring**

Reject partial Cloudflare config. Reject simultaneous Supabase and Cloudflare config. Use Cloudflare clients when Cloudflare config exists. Keep local-only mode when neither cloud config exists.

- [ ] **Step 4: Update installer validation**

In `deployment/install.sh`, validate these variables as one group:

- `GATE_CLOUDFLARE_API_URL`
- `GATE_CLOUDFLARE_ACCESS_CLIENT_ID`
- `GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET`

Reject mixed Supabase and Cloudflare credential groups.

- [ ] **Step 5: Verify**

Run:

```bash
.venv/bin/python -m unittest tests.test_main tests.test_deployment -v
bash -n deployment/install.sh
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add gate_controller/__main__.py deployment/install.sh tests
git commit -m "feat: wire controller to Cloudflare API"
```

---

### Task 4: Add Loopback Direct Command Server

**Files:**
- Create: `gate_controller/command_server.py`
- Modify: `gate_controller/control_plane.py` if command execution is extracted for reuse
- Test: `tests/test_command_server.py`
- Test: `tests/test_control_plane.py`

**Interfaces:**
- Produces: `build_command_server(host, port, executor) -> ThreadingHTTPServer`
- Produces: `CommandRequestHandler`, `run_command_server(host, port, executor, stop_event)`
- Produces: `DirectCommandExecutor.execute(payload) -> dict`
- Consumes: `ActuationCoordinator`, `PromptPlayer`, `LocalStore`

- [ ] **Step 1: Write failing command server tests**

```python
def test_command_server_rejects_non_loopback_bind(self):
    with self.assertRaisesRegex(ValueError, "loopback"):
        build_command_server("0.0.0.0", 8765, executor=object())

def test_expired_direct_command_never_pulses_relay(self):
    response = executor.execute({
        "controller_id": "primary",
        "command": "open_gate",
        "idempotency_key": "request-1",
        "expires_at": "2026-08-17T10:00:00Z",
    }, now=datetime(2026, 8, 17, 10, 0, 1, tzinfo=timezone.utc))
    self.assertEqual(response["status"], "expired")
    self.assertEqual(relay.pulses, 0)

def test_repeated_direct_command_idempotency_does_not_repulse(self):
    first = executor.execute(valid_payload)
    second = executor.execute(valid_payload)
    self.assertEqual(first["status"], "completed")
    self.assertEqual(second["status"], "completed")
    self.assertEqual(relay.pulses, 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_command_server -v`

Expected: FAIL because command server module is absent.

- [ ] **Step 3: Implement direct executor and HTTP server**

Use `http.server.ThreadingHTTPServer` bound only to `127.0.0.1` or `::1`. Accept `POST /commands` with a body limit of 4096 bytes. Return JSON with `status` and optional `detail`. Reuse existing actuation idempotency by mapping to `command:<idempotency_key>`.

- [ ] **Step 4: Verify**

Run:

```bash
.venv/bin/python -m unittest tests.test_command_server tests.test_control_plane -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add gate_controller/command_server.py gate_controller/control_plane.py tests/test_command_server.py tests/test_control_plane.py
git commit -m "feat: add loopback gate command endpoint"
```

---

### Task 5: Install And Document cloudflared Command/Media Tunnel

**Files:**
- Create: `deployment/cloudflared/gate-controller-tunnel.yml`
- Create: `deployment/systemd/gate-command-server.service`
- Modify: `deployment/install.sh`
- Modify: `docs/deployment.md`
- Test: `tests/test_deployment.py`

**Interfaces:**
- Produces: validated local tunnel example and systemd unit wiring
- Consumes: command server from Task 4 and existing media gateway services

- [ ] **Step 1: Write failing deployment tests**

```python
def test_cloudflared_config_has_command_media_and_catch_all_rules(self):
    config = Path("deployment/cloudflared/gate-controller-tunnel.yml").read_text()
    self.assertIn("service: http://127.0.0.1:8765", config)
    self.assertIn("service: http://127.0.0.1:8889", config)
    self.assertRegex(config, r"- service: http_status:404\\s*$")

def test_command_server_unit_runs_as_gate_controller_without_gpio_capabilities(self):
    unit = Path("deployment/systemd/gate-command-server.service").read_text()
    self.assertIn("User=gate-controller", unit)
    self.assertNotIn("CAP_SYS_RAWIO", unit)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_deployment -v`

Expected: FAIL because tunnel config/unit are absent.

- [ ] **Step 3: Add config and install docs**

`deployment/cloudflared/gate-controller-tunnel.yml` must include:

```yaml
tunnel: 11111111-1111-4111-8111-111111111111
credentials-file: /etc/cloudflared/11111111-1111-4111-8111-111111111111.json
ingress:
  - hostname: gate-command.example.com
    service: http://127.0.0.1:8765
  - hostname: gate-media.example.com
    service: http://127.0.0.1:8889
  - service: http_status:404
```

Docs must instruct `cloudflared tunnel ingress validate` and `cloudflared tunnel ingress rule https://gate-command.example.com`.

- [ ] **Step 4: Verify**

Run:

```bash
.venv/bin/python -m unittest tests.test_deployment -v
bash -n deployment/install.sh
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add deployment docs tests/test_deployment.py
git commit -m "deploy: add Cloudflare Tunnel command wiring"
```

---

### Task 6: Remove Supabase Polling And Legacy AWS/S3 Runtime Surface

**Files:**
- Modify: `gate_controller/control_plane.py`
- Modify: `gate_controller/authorisation.py`
- Modify: `gate_controller/__main__.py`
- Remove: `s3_utils.py`
- Remove: `test_upload_image_to_s3.py`
- Modify: `README.md`
- Modify: `docs/reolink-rlc-811a.md`
- Test: `tests/test_runtime.py`
- Test: `tests/test_main.py`
- Test: `tests/test_authorisation.py`
- Test: `tests/test_control_plane.py`

**Interfaces:**
- Produces: no runtime Supabase/AWS imports in the Cloudflare branch
- Consumes: Cloudflare clients and direct command server

- [ ] **Step 1: Write failing removal test**

```python
def test_runtime_import_graph_has_no_supabase_or_s3_clients(self):
    runtime_files = [path for path in Path("gate_controller").glob("*.py")]
    forbidden = []
    for path in runtime_files:
        text = path.read_text()
        if "Supabase" in text or "boto3" in text or "s3_utils" in text:
            forbidden.append(str(path))
    self.assertEqual(forbidden, [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest tests.test_runtime -v`

Expected: FAIL while Supabase classes remain.

- [ ] **Step 3: Remove or rename Supabase-specific code**

Replace Supabase class names and messages with Cloudflare equivalents. Delete S3 helpers. Update tests to assert Cloudflare behavior and preserve existing command worker safety tests through the direct command executor.

- [ ] **Step 4: Verify**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime tests.test_main tests.test_authorisation tests.test_control_plane -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add gate_controller tests README.md docs
git rm s3_utils.py test_upload_image_to_s3.py
git commit -m "refactor: remove Supabase and S3 controller runtime"
```

---

### Task 7: Add Pi Performance Harness Without Running Pi Tests

> Historical note: the summary schema below was updated after on-device
> validation; the original task sequence and checklist are otherwise preserved.

**Files:**
- Create: `scripts/pi-cloudflare-performance-harness.py`
- Create: `docs/pi-cloudflare-performance.md`
- Test: `tests/test_performance_harness.py`

**Interfaces:**
- Produces: local harness command that can run on the Pi when reachable
- Consumes: command server local endpoint and media gateway health endpoint

- [ ] **Step 1: Write failing harness tests**

```python
def test_performance_harness_refuses_to_actuate_without_explicit_flag(self):
    result = parse_args(["--command-url", "http://127.0.0.1:8765/commands"])
    self.assertFalse(result.actuate)

def test_performance_harness_outputs_json_summary(self):
    summary = build_summary(
        samples=[{"latency_ms": 12.5}],
        run_mode="host_metrics_only",
        actuation_requested=False,
    )
    self.assertEqual(summary["run_mode"], "host_metrics_only")
    self.assertFalse(summary["actuation_requested"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_performance_harness -v`

Expected: FAIL because harness is absent.

- [ ] **Step 3: Implement harness**

Harness records local command endpoint latency with a non-actuating health request by default, media gateway health URL latency, CPU load from `/proc/loadavg` when present, memory from `/proc/meminfo` when present, disk usage via `shutil.disk_usage("/")`, and JSON output. Real relay actuation requires `--actuate` and is not run in this environment.

- [ ] **Step 4: Verify**

Run:

```bash
.venv/bin/python -m unittest tests.test_performance_harness -v
.venv/bin/python scripts/pi-cloudflare-performance-harness.py --skip-network --output /tmp/gate-pi-perf.json
```

Expected: unit tests pass and the local command writes JSON with
`"run_mode": "host_metrics_only"` and `"actuation_requested": false`.

- [ ] **Step 5: Commit**

```bash
git add scripts/pi-cloudflare-performance-harness.py docs/pi-cloudflare-performance.md tests/test_performance_harness.py
git commit -m "test: add deferred Pi performance harness"
```

---

### Task 8: Final Controller Docs And Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/deployment.md`
- Modify: `docs/reolink-rlc-811a.md`
- Modify: `.github/workflows/ci.yml` if new tests need CI inclusion
- Test: complete repo verification

**Interfaces:**
- Produces: controller repo ready for PR and deferred Pi validation
- Consumes: Tasks 1-7

- [ ] **Step 1: Write failing docs contract test**

Add a docs test that asserts README describes Cloudflare variables and does not instruct new Supabase remote command setup.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest tests.test_deployment -v`

Expected: FAIL until docs are updated.

- [ ] **Step 3: Update docs**

Document:

- `GATE_CLOUDFLARE_API_URL`
- `GATE_CLOUDFLARE_ACCESS_CLIENT_ID`
- `GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET`
- direct command endpoint
- cloudflared config validation
- event ingest and R2 retention
- rollback to previous release before decommission
- Pi performance test deferral

- [ ] **Step 4: Final verification**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q gate_controller deployment tests scripts
bash -n deployment/install.sh
sh -n file_monitor.sh
```

Expected: all pass, with the existing Linux flock skip accepted on macOS.

- [ ] **Step 5: Commit**

```bash
git add README.md docs .github tests
git commit -m "docs: finalize Cloudflare controller cutover"
```
