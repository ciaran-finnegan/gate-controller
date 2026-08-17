# Gate Controller Cloudflare Replatform Design

Date: 2026-08-17

Companion UI/Worker spec: `access-gate-ui/docs/superpowers/specs/2026-08-17-cloudflare-replatform-design.md`

## Classification

This is the controller side of the architectural Cloudflare replatform. It replaces Supabase/AWS-facing cloud clients with Worker/Tunnel clients while preserving the current local-first safety model. Raspberry Pi performance tests are deferred until the Pi is reachable again on the user's home network or Tailscale.

## Current State

The controller watches completed JPEG uploads, sends bounded requests to Plate Recognizer, matches authorized plates locally, and actuates the relay through a durable local coordinator. Optional Supabase workers currently refresh plates, poll remote commands, update controller status, and deliver queued outbox events. Event evidence is normalized and stored beside SQLite before asynchronous delivery. Legacy AWS/S3 helper files remain in the repo.

The existing safety boundaries stay:

- Recognition and relay decisions never wait for optional cloud services after Plate Recognizer succeeds.
- Cloud-managed plate snapshots fail closed after a bounded staleness window.
- Remote commands have durable idempotency and expiry checks under the relay lock.
- Media gateway code remains isolated from controller and relay imports.

## Goals

- Replace Supabase plate/status/command/outbox clients with Cloudflare Worker clients.
- Remove legacy AWS/S3 upload paths.
- Add a minimal loopback-only command endpoint for Cloudflare Tunnel.
- Authenticate controller-to-Worker requests with Cloudflare Access service credentials.
- Authenticate Worker-to-Pi direct commands through an Access-protected Tunnel service token.
- Keep Tailscale only for break-glass administration.
- Add a committed performance-test harness and runbook, but do not run Pi performance tests until the Pi is reachable.

## Non-Goals

- No local OCR replacement.
- No relay actuation via browser, public Worker route, or persistent cloud queue.
- No camera credential exposure to the browser or Worker.
- No transcoding or new media server behavior until measured on the Pi.

## Controller Cloud Clients

Introduce a Cloudflare client layer with bounded HTTP behavior matching the existing Supabase fail-closed posture.

Environment:

- `GATE_CLOUDFLARE_API_URL`
- `GATE_CLOUDFLARE_ACCESS_CLIENT_ID`
- `GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET`
- `GATE_CONTROLLER_ID`
- Existing local safety, OCR, relay, outbox, and media settings remain.

Routes called by the Pi:

- `GET /api/controller/plates`: returns the authorized plate snapshot for this controller.
- `POST /api/controller/status`: updates heartbeat, camera/upload status, relay readiness, queue depth, and media capabilities.
- `POST /api/controller/events`: receives versioned event/outbox payloads and optional evidence bytes.

The client rejects non-HTTPS Cloudflare API URLs except loopback/local test URLs. It adds `CF-Access-Client-Id` and `CF-Access-Client-Secret` headers for service-token authentication.

## Plate Snapshot Flow

Replace `SupabasePlateFetcher` with a Worker-backed fetcher:

1. Worker returns normalized plate rows, revision metadata, and server timestamp.
2. Controller validates the response shape and maximum size.
3. `AuthorisedPlateCache` keeps atomic local replacement and staleness behavior.
4. Recognition reads only the in-memory snapshot.
5. Worker/API outages keep the last known good snapshot until its configured staleness deadline, then fail closed.

## Status And Outbox Flow

Status:

- Heartbeat worker posts the same measured status and media capability vocabulary to the Worker.
- Failure to post status is logged and retried; it never affects OCR or relay state.

Events:

- Existing local SQLite outbox remains the durability boundary.
- Existing evidence normalization and SHA-256 identity remain.
- Worker ingest must be idempotent using the existing `<controller_id>:<local_event_id>` idempotency digest.
- Successful 2xx delivery allows local spool cleanup as it does today.
- Missing/corrupt expected evidence remains pending; do not substitute camera paths.

## Direct Remote Command Endpoint

Supabase command polling is retired for the Cloudflare cutover. A new local endpoint serves only loopback and is exposed by Cloudflare Tunnel.

Endpoint shape:

- Local bind: `127.0.0.1:<configured-port>`.
- Tunnel hostname/path: controlled by cloudflared config, Access-protected.
- Method: `POST /commands`.
- Payload: `controller_id`, `command`, `idempotency_key`, `expires_at`, optional `prompt_key`, and Worker audit/request ID.

Behavior:

1. Reject malformed requests, unknown commands, unknown prompt keys, wrong controller ID, or expired requests.
2. Enforce a maximum command lifetime of 10 seconds.
3. Route through the existing `ActuationCoordinator` and `PromptPlayer`.
4. Recheck expiry under the relay lock immediately before GPIO activation.
5. Persist idempotency so retries cannot repulse the relay.
6. Return `completed`, `failed`, or `expired` synchronously when known.

The endpoint does not accept browser credentials, does not queue work for later, and does not expose arbitrary shell/audio/file behavior.

## Cloudflare Tunnel Deployment

Add deployment docs/config examples for a Pi `cloudflared` service:

- One public hostname/path for the command endpoint.
- One public hostname/path for the existing media gateway.
- Required catch-all ingress rule.
- `cloudflared tunnel ingress validate` and rule checks in the runbook.
- Access policies: service-token-only for direct command path; human/media policy according to the UI Worker spec.

Production should prefer a remotely managed/token-based tunnel unless local config is needed for the Pi service layout.

## Migration

Controller cutover steps:

1. Deploy Worker API, D1, R2, Access policies, service tokens, and Tunnel routes.
2. Verify the Worker plate snapshot against Supabase export.
3. Install updated controller release but keep Supabase env available for rollback until acceptance.
4. Switch `/etc/gate-controller.env` from Supabase variables to Cloudflare variables.
5. Start/verify `cloudflared` and the loopback command endpoint.
6. Validate heartbeat and event ingest without touching relay.
7. Validate a non-actuating mocked command path locally.
8. Defer real Pi latency/performance/media validation until on-site network access is restored.

Rollback before decommission:

- Restore Supabase env vars and previous release.
- Stop cloudflared command exposure.
- Switch UI/DNS back to Netlify/Supabase if needed.

## Testing

TDD applies before production changes.

Controller test-first targets:

- Worker plate fetcher uses Access headers, bounded timeouts, HTTPS validation, response validation, and fails closed on malformed payloads.
- Status client sends the existing status/media capability contract without mutating relay state.
- Outbox client posts existing event payloads and preserves retry semantics on non-2xx/network failures.
- Direct command endpoint rejects expired/stale commands before relay activation.
- Direct command endpoint persists idempotency across retries/restarts.
- Prompt command accepts only fixed configured prompt keys.
- Deployment env generation rejects partial Supabase/Cloudflare configuration.
- Legacy AWS/S3 modules are removed or made unreachable with tests proving no runtime import path.
- cloudflared config examples validate syntactically where tooling is available.
- Performance harness records local endpoint latency, media health, CPU/memory/disk/network snapshots, but the real Pi run is skipped for now.

Baseline from isolated worktree:

- `.venv/bin/python -m unittest discover -s tests -v`: 356 passed, 2 skipped.
- `.venv/bin/python -m compileall -q gate_controller deployment tests`: passed.
- `bash -n deployment/install.sh && sh -n file_monitor.sh`: passed.

## Open Decisions

- Final Cloudflare hostnames and Access application split.
- Whether the command endpoint and media gateway share a tunnel hostname with path-based routing or use separate hostnames.
- Pi command endpoint port.
- Final service token names and secret storage path in `/etc/gate-controller.env`.
- Pi performance test date once SSH/Tailscale/home network access is restored.
