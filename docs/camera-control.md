# Gate Camera Control

`gate-camera-control` is the only process in the deployment that holds Reolink
camera API credentials. It exposes a small loopback HTTP surface so the Gate Mate
Worker can read the state of the camera's two lights, take a bounded lease on
either of them, and fetch one on-demand 4K still — without the browser, the
Worker, or the gate controller ever holding a camera credential.

The service holds **two bounded light leases**, and they behave identically:

| Lease | Camera light | Endpoint | Default |
| --- | --- | --- | --- |
| IR | the infrared illuminator, `Auto` or `Off` | `POST /camera/ir` | `GATE_CAMERA_IR_DEFAULT` |
| Spotlight | the RLC-811A's white lamp, `On` or `Off` | `POST /camera/spotlight` | `GATE_CAMERA_SPOTLIGHT_DEFAULT` |

Each has its own durable lease record, its own budget, and its own idempotency
keys; neither can revert or cancel the other. Both are `Off` by default, both
auto-revert, and both are restored on start.

Companion issues: gate-controller#92 (this service) and access-gate-ui#33 (the
Worker routes, the D1 audit, and the Live UI).

## Safety: these are recognition controls, not brightness controls

Read [Front Gate camera night configuration](reviews/2026-09-06-camera-night-configuration.md)
before changing anything here.

- IR has been **Off** by deliberate, measured configuration since 2026-09-06.
  With IR off the empty scene reads brightness 0.0180 with 99.6% of pixels below
  level 33: the AI trigger and the plate both depend on the vehicle's headlights.
- IR **on** re-creates the specular return off the near gate post about 1 m from
  the lens (left-third clipping 0.2248), which is what caused the 22:16 denial.
- `GetIrLights` `range` is exactly `["Auto", "Off"]`. There is no brightness and
  no zone control: it is all or nothing.
- The spotlight is a real white lamp with a 1–100 brightness, and it is the one
  light at the gate that a person on the road can see. It is **not** a night
  default: see the section below.

Therefore every change this service accepts is a **bounded lease** that reverts
to that light's configured default, and the revert survives a service restart
and a reboot: each lease record lives on durable storage, and a start that finds
no usable record reads the camera once and puts the light back if it disagrees.
The service exposes those two lights and nothing else. It never calls `SetIsp`:
the deployed Manual `s4 g16` exposure is a measured setting and stays a
reviewed, on-Pi operation. The camera command allowlist is exactly `Login`,
`GetIrLights`, `SetIrLights`, `GetWhiteLed`, `SetWhiteLed`, `Snap`, and no
request body can widen it.

## Why a spotlight and not IR

The fitted camera is an RLC-811A running in **forced colour night mode**
(`Isp.dayNight=Color`), and a camera in colour cannot use infrared. Measured on
the fitted unit: with IR `Auto` under colour mode the night scene reads
brightness **0.028** — identical to IR off. The IR lease is still here and still
works, but on this camera and in this mode it changes nothing anybody can see.
The white spotlight is the light that actually reaches the plate, so that is the
light the operator now needs. See
[Reolink RLC-811A](reolink-rlc-811a.md#night-light) and its
[fitted-unit record](reolink-rlc-811a.md#fitted-unit-2026-09-11).

**It stays a manual, time-limited action.** The PIR floodlight already lights
the stop position, and plates are retroreflective: a second light doubles the
return and washes the characters out — the 811A document's own instruction is to
run one light, not two. So the spotlight is `Off` by default, every change is a
lease that expires on its own, and nothing in this service will ever turn it on
by schedule or on detection. Every write also pins the camera's `mode` to `0`
(manual), so the camera's own "auto on at night" cannot re-arm the lamp behind
an expiring lease.

## Security model

| Property | How it is enforced |
| --- | --- |
| Camera credentials exist in exactly one file | `/etc/gate-camera-control.env`, root:root 0600, read through `gate_media_config._open_trusted_file` |
| The controller never gains camera credentials | `file-monitor.service` reads no camera env; it learns both lights' state only from the nonsecret `/run/gate-camera/state.json` |
| The media gateway secret is not widened | The camera env is a **separate** file. `validate_gateway_static_environment()` pins the gateway key set, so camera-API keys cannot be bolted onto it, and the service is not in group `gate-media` |
| Its own identity | System user and group `gate-camera-control`; the installer refuses to run if that account shares a group with the media or controller services, or with `gpio` |
| Its own network reach | `IPAddressDeny=any` plus `IPAddressAllow=localhost` in the unit, and `IPAddressAllow=<camera>/32` in the drop-in `gate-camera-control.service.d/10-camera-address.conf`, which the validator writes itself (`camera-control --write-address-dropin`) so the address never passes through the installer's shell. `GATE_CAMERA_HOST` must be one exact reachable IPv4 address so that pin is one
host route and is verifiable |
| Its own remote door | A separate Cloudflare Tunnel hostname (`gate-camera.*`) with its own Access application and its own service token — deliberately not the `gate-command` token |
| No camera payloads leak | Every response is a bounded JSON status (or JPEG bytes); no camera payload, URL, token, or credential appears in a response or in the journal |
| Loopback only | Binds `127.0.0.1:8767`; `--host` refuses anything else |

The camera presents a self-signed certificate, so TLS verification is disabled
for the camera connection exactly as `curl -k` does today. The trust boundary is
the network pin above: the service can open a connection to nothing but loopback
and that one address.

## HTTP contract

Base URL on the Pi: `http://127.0.0.1:8767`. Remotely: the `gate-camera` tunnel
hostname, behind its own Access service token, called from the Worker the same
way `worker/piCommandClient.ts` calls `POST /commands`.

Every response carries `Cache-Control: no-store`. Request bodies are JSON only,
at most 4096 bytes. Any query string or fragment is a `404`; this service takes
no parameters in the URL.

`HEAD` is answered on every path that answers `GET`, with the same status and
the same headers and no body; `HEAD /camera/snap` reports `image/jpeg` without
taking a picture, so it never spends the snapshot budget. A request the parser
could not read at all is answered `400 {"error":"invalid_request"}` with a real
status line and `Connection: close`.

Every socket read on a connection is bounded at **10 s**. A client that opens a
connection and then says nothing, stops mid-headers, or holds a keep-alive
connection open after its last request is dropped rather than parking one of the
service's few threads: `TasksMax=64` makes twenty such connections an outage.
The bound is on the socket, not on the handler, so a `POST /camera/ir` that has
to log in first — 10-20 s inside the service — is unaffected.

Each endpoint has its own budget, and exceeding one is `429` with `Retry-After`:

| Endpoint | Budget |
| --- | --- |
| `GET /camera/state` (and `GET /camera/ir`) | 10 at once, then 2 a second |
| `POST /camera/ir` | 6 at once, then 1 every 2 s |
| `POST /camera/spotlight` | 6 at once, then 1 every 2 s, in its **own** bucket |
| `GET /camera/snap` | 1 every 2 s, service-wide |

### `GET /camera/state` — also served at `GET /camera/ir`

```json
{
  "observed_at": "2026-09-07T21:04:11+00:00",
  "ir": {
    "state": "Auto",
    "default": "Off",
    "effective_until": "2026-09-07T21:14:11+00:00",
    "lease_seconds_remaining": 600,
    "revert_failed": false,
    "last_error": null
  },
  "spotlight": {
    "state": "Off",
    "default": "Off",
    "brightness": 100,
    "effective_until": null,
    "lease_seconds_remaining": null,
    "revert_failed": false,
    "last_error": null
  }
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `observed_at` | RFC 3339 UTC string | when this answer was produced |
| `ir.state` | `"Auto"` \| `"Off"` \| `"unknown"` | the camera's observed state; `"unknown"` whenever it could not be confirmed within 60 s. **Never** assume `"Off"` from `"unknown"` |
| `ir.default` | `"Auto"` \| `"Off"` | `GATE_CAMERA_IR_DEFAULT`, the state every lease reverts to |
| `ir.effective_until` | RFC 3339 UTC string \| `null` | when the current lease expires; `null` when no lease is outstanding |
| `ir.lease_seconds_remaining` | integer \| `null` | seconds left on the lease, for a countdown |
| `ir.revert_failed` | boolean | a revert has failed and is being retried with backoff |
| `ir.last_error` | `"camera_busy"` \| `"camera_unreachable"` \| `"camera_error"` \| `null` | the last camera failure seen |
| `spotlight.state` | `"On"` \| `"Off"` \| `"unknown"` | as `ir.state`, for the white lamp. **Never** assume `"Off"` from `"unknown"` |
| `spotlight.default` | `"On"` \| `"Off"` | `GATE_CAMERA_SPOTLIGHT_DEFAULT` |
| `spotlight.brightness` | integer 1–100 | `GATE_CAMERA_SPOTLIGHT_BRIGHTNESS`, how bright a lease burns it. Read-only: no request can change it |
| `spotlight.effective_until`, `.lease_seconds_remaining`, `.revert_failed`, `.last_error` | | exactly as the `ir` fields above, for the spotlight's own lease |

The two blocks are independent. A spotlight the camera would not answer about
reads `"unknown"` and leaves the `ir` block — and the still — entirely alone.

A read refreshes the observation from the camera when it is older than 5 s;
otherwise it answers from the cached observation. The refresh is subject to the
same breaker and login throttle as any other call, so polling cannot cause a
login storm.

Three rules keep that refresh from ever delaying a revert, which is the one
thing this service must not do:

- the camera call happens **outside** the state lock — a read publishes what it
  learned under the lock, and holds it only for that;
- refreshes are **single-flight**: a second concurrent reader answers from the
  first reader's result rather than putting a second call on the camera;
- a queued revert or lease change takes **priority** — a read that finds one
  waiting skips the camera entirely and answers from the last observation.

A read observes **both** lights, so a `GET /camera/state` costs one `GetIrLights`
and one `GetWhiteLed` on the cached token — both under the rules above.

Separately, the state publisher makes one bounded read of each light about every
30 s on the cached token, behind the breaker. Without it a service that has
served no request since a restart has never looked at the camera, and would
report an entirely healthy camera as unavailable.

### `POST /camera/ir`

Request body — unknown fields are rejected:

```json
{"state": "Auto", "lease_minutes": 10, "idempotency_key": "01J..."}
```

| Field | Required | Rules |
| --- | --- | --- |
| `state` | yes | exactly `"Auto"` or `"Off"` |
| `lease_minutes` | no | integer 1–`GATE_CAMERA_IR_LEASE_MAX_MINUTES` (default max 60). Omitted means `GATE_CAMERA_IR_LEASE_DEFAULT_MINUTES` (default 10) |
| `ttl_seconds` | no | compatibility alias for callers that speak seconds: a whole number of minutes, 60–`max × 60`. Sending both `lease_minutes` and `ttl_seconds` is a `400` |
| `idempotency_key` | no | 1–128 characters. A replay within 300 s does not call the camera again |

Response `200`: the `GET /camera/state` document plus `"status": "completed"`,
and `"idempotency_key"` echoed when one was supplied.

A replayed key is answered from the **live** lease, not from a stored copy of
the first response: `observed_at` and `lease_seconds_remaining` are recomputed,
so a client that retries a minute later is told how much of the lease is left
now, not how much was left when the lease was created. The key is reserved
before the camera is touched, so two concurrent posts of one key produce one
lease and one camera call; the second waits for the first and then answers from
what the first actually created.

A key records how its call ended, not merely that it happened:

- the call succeeded — a replay is `200 completed`, recomputed from the live
  lease as above;
- the camera refused it or did not confirm — a replay raises the **same**
  failure with the same status. Answering `completed` from a snapshot taken
  before the change told the caller a change had landed that the camera had
  refused;
- the service refused it before touching the camera (`429 rate_limited`) — the
  key stands for nothing and is released, so the caller may retry it;
- the first call is still in flight when a duplicate's 15 s wait runs out —
  `502 {"error":"camera_indeterminate"}`. Nothing is known yet, and this is the
  one status the app records as indeterminate rather than as a landed change
  (any `2xx`) or a definite refusal (`4xx`, `503 camera_busy`).

Setting `state` to the configured default cancels the lease immediately — that
is the "revert now" action.

### `POST /camera/spotlight`

The white lamp, under exactly the rules above. Request body — unknown fields are
rejected:

```json
{"state": "On", "lease_minutes": 5, "idempotency_key": "01J..."}
```

| Field | Required | Rules |
| --- | --- | --- |
| `state` | yes | exactly `"On"` or `"Off"` |
| `lease_minutes` | no | integer 1–`GATE_CAMERA_IR_LEASE_MAX_MINUTES`. Omitted means `GATE_CAMERA_IR_LEASE_DEFAULT_MINUTES`. The lease bounds are **shared with IR**; the spotlight has no minutes of its own |
| `ttl_seconds` | no | the same compatibility alias, with the same rules. Sending both is a `400` |
| `idempotency_key` | no | 1–128 characters, **scoped to this light**. The same key posted at `/camera/ir` and at `/camera/spotlight` is two independent calls with two independent outcomes; it can never be answered from the other light's record |

There is no `brightness` field, and sending one is a `400`. How bright the lamp
burns is `GATE_CAMERA_SPOTLIGHT_BRIGHTNESS`, set once in the environment file
where it is validated, rather than something whoever holds an app session can
raise.

Response `200`: the `GET /camera/state` document — **both** lights — plus
`"status": "completed"`, and `"idempotency_key"` echoed when one was supplied.
Setting `state` to `GATE_CAMERA_SPOTLIGHT_DEFAULT` cancels the lease
immediately, exactly as it does for IR. Every other rule in `POST /camera/ir`
above — the replay from the live lease, the four outcomes a key can record, the
`429` release, the `502 camera_indeterminate` — applies here unchanged.

The path is write-only: `GET`, `HEAD`, `PUT` and `DELETE` on `/camera/spotlight`
answer `405`. Both lights are read together on `GET /camera/state`.

### `GET /camera/snap` — also served at `POST /camera/snap`, and `GET`/`POST /camera/snapshot`

`POST` is accepted on both spellings for callers that cannot issue a `GET`;
it takes no body and behaves identically.

Returns `200` with `Content-Type: image/jpeg` and one bounded 4K JPEG from the
camera's `Snap` API (a few hundred KB, about 0.45 s). Rate limited to **one per
2 s** service-wide. The service stores nothing; retention, if any, is the
Worker's decision.

### Errors

| Status | Body | When |
| --- | --- | --- |
| `400` | `{"error":"invalid_request"}` | malformed JSON, unknown field, bad state, lease out of bounds |
| `404` | `{"error":"not_found"}` | unknown path, or any query string |
| `405` | `{"error":"method_not_allowed"}` | wrong method for a known path |
| `413` | `{"error":"request_too_large"}` | body over 4096 bytes |
| `429` | `{"error":"rate_limited","retry_after":2}` | any endpoint budget above; `Retry-After` header set |
| `502` | `{"error":"camera_error"}` | the camera answered, but not usably |
| `502` | `{"error":"camera_indeterminate"}` | a replay of an `idempotency_key` whose first call was still in flight after 15 s. Not a failure and not a success; do not infer an IR state from it |
| `503` | `{"error":"camera_busy","retry_after":60}` | circuit breaker open after a camera 502 or a camera that did not answer, or the 60 s login throttle is holding; `Retry-After` header set. **Do not retry** — surface the wait |
| `503` | `{"error":"camera_unreachable"}` | the camera did not answer at all. This is the *first* such failure; it also opens the breaker, so an immediate retry is `503 camera_busy` |
| `500` | `{"error":"internal_error"}` | unexpected; nothing about the camera is disclosed |

## Token cache, throttle, and circuit breaker

The RLC-810A firmware answers **HTTP 502 for about a minute** after repeated
`Login` calls. The client therefore:

- holds **one** token plus its `leaseTime` in memory and reuses it for every call,
  refreshing only within 60 s of expiry or after an authentication failure;
- persists it to `/run/gate-camera/token.json` (0600, owner-checked on read) so
  `Restart=always` cannot cause a login storm;
- persists `last_login_at` in that same file, **written before the login is
  attempted**, so a login that hangs or takes the process down with it still
  counts against the floor. A restart that finds no usable token therefore still
  knows how recently this service last hit `Login`, and a crash loop waits
  instead of logging in on every start;
- allows exactly **one in-flight login** — concurrent callers wait on the same
  lock and then reuse the resulting token;
- enforces a **60 s minimum re-login interval**; a login needed sooner answers
  `503 camera_busy` with the remaining seconds;
- on a camera `502`/`503`, **or a camera that does not answer at all**, opens a
  **circuit breaker for 60 s** and answers `503 camera_busy` without any retry.
  An unreachable camera has to open it too: otherwise every read pays a fresh
  5 s connect attempt and a due revert queues behind all of them.

## State published to the controller

The service atomically rewrites `/run/gate-camera/state.json` (0644) every 5 s
from its cached observation — it never calls the camera to publish:

```json
{
  "observed_at": 1757279051,
  "camera_control": {
    "available": true,
    "reason": "ready",
    "ir": {
      "state": "Off",
      "default": "Off",
      "effective_until": null,
      "revert_failed": false
    },
    "spotlight": {
      "state": "Off",
      "default": "Off",
      "effective_until": null,
      "revert_failed": false
    }
  }
}
```

`reason` is one of `ready`, `not_observed`, `camera_busy`, `camera_unreachable`,
`camera_error`. `available` is true exactly when `ir.state` is a real state and
`reason` is `ready` — **the illuminator decides both**, so a spotlight the
camera would not answer about never takes the camera control away from the app.
The file names no camera, no address, and no credential. `brightness` is
deliberately not here: it is a control-surface field the app reads from
`GET /camera/state`, and the heartbeat block's key set is frozen.

Both shapes of this file are readable, in both directions, because the service
and the controller are upgraded separately:

- a document **without** `spotlight` is an older service. The controller fills
  the block with `state: "unknown"` and **`supported: false`**, which is how the
  app tells "this deployment has no spotlight control" from "the spotlight is
  there and its state is not currently known";
- a document **with** `spotlight` gets `supported: true` and the light's real
  state.

A `spotlight` that is present but malformed fails the whole document closed to
`service_unhealthy`, exactly as a malformed `ir` block does: guessing which half
of a document to trust is how an `unknown` becomes a reported `Off`.

`not_observed` means the service is running and nothing has failed — it simply
has not called the camera yet. It is deliberately distinct from
`camera_unreachable`: the app hides the control on a failure, so reporting a
healthy but un-observed camera as unreachable hid the control after every
restart until somebody opened the page and forced a read. In practice the 30 s
background refresh clears `not_observed` within one publication interval.

`gate_controller/camera_control_state.py` reads it the way
`gate_controller/media_capabilities.py` reads the media snapshot: `O_NOFOLLOW`,
8 KiB size cap, 30 s freshness cap, exact key sets, and coherence checks. It
falls back to `not_configured` when the file is absent and `service_unhealthy`
for anything else, both with `ir.state: "unknown"`. That block appears as
`camera_control` in `_controller_status()` beside `media` and `recognition`, and
reaches the app through the 15 s heartbeat.

Worst-case heartbeat staleness is Pi heartbeat 15 s plus UI poll 15 s ≈ **30 s**,
so the app must confirm a toggle with a direct read rather than waiting for a
heartbeat.

## Environment

`/etc/gate-camera-control.env`, root:root 0600, validated by
`gate_media_config.validate_camera_control_environment()`. The key set is closed:
anything outside this table is rejected. Template:
[`deployment/gate-camera-control.env.example`](../deployment/gate-camera-control.env.example).

| Key | Required | Default | Rules |
| --- | --- | --- | --- |
| `GATE_CAMERA_HOST` | yes | — | exactly one reachable **IPv4** address; loopback, unspecified, multicast, link-local and reserved are rejected, and hostnames are rejected so the systemd `/32` pin stays verifiable. IPv6 is rejected outright: `http.client` splits a bare IPv6 literal at its last colon and would never reach the camera, and an IPv4-shaped `/32` on such an address would pin 2**96 of them |
| `GATE_CAMERA_USERNAME` | yes | — | 1–256 bytes |
| `GATE_CAMERA_PASSWORD` | yes | — | 1–256 bytes |
| `GATE_CAMERA_IR_DEFAULT` | no | `Off` | exactly `Auto` or `Off` |
| `GATE_CAMERA_IR_LEASE_DEFAULT_MINUTES` | no | `10` | 1–60, and not greater than the maximum. **Applies to both lights** |
| `GATE_CAMERA_IR_LEASE_MAX_MINUTES` | no | `60` | 1–60. **Applies to both lights** |
| `GATE_CAMERA_SPOTLIGHT_DEFAULT` | no | `Off` | exactly `On` or `Off`. Keep it `Off`: see "Why a spotlight and not IR" |
| `GATE_CAMERA_SPOTLIGHT_BRIGHTNESS` | no | `100` | integer 1–100. Configuration only; no request field can change it |

The lease minutes are deliberately shared. One operator asking "how long may a
light stay changed" gets one answer, and a second pair of knobs would only be a
second thing to get wrong.

Validate a file without starting the service:

```bash
sudo python3 gate_media_config.py camera-control --env /etc/gate-camera-control.env
```

## Journal

Every change, revert, camera error, and breaker event is one structured line on
stdout, which systemd captures. The prefix is always `gate_camera_control` and
values are stripped to `[A-Za-z0-9-_.:+]`, so no field can inject another:

```text
gate_camera_control stage=started ir_default=Off lease_default_minutes=10 lease_max_minutes=60 port=8767 spotlight_brightness=100 spotlight_default=Off
gate_camera_control stage=login lease_seconds=3600
gate_camera_control stage=login_throttled retry_after=41
gate_camera_control stage=ir_set lease_seconds=600 outcome=completed state=Auto
gate_camera_control stage=ir_revert outcome=completed state=Off
gate_camera_control stage=ir_revert attempt=2 outcome=camera_busy retry_after=10 state=Off
gate_camera_control stage=startup_revert state=Off
gate_camera_control stage=startup_reconcile observed=Auto state=Off
gate_camera_control stage=startup_reconcile outcome=not_observed
gate_camera_control stage=lease_corrupt state=Off
gate_camera_control stage=ir_idempotent_in_flight
gate_camera_control stage=spotlight_set brightness=100 lease_seconds=300 outcome=completed state=On
gate_camera_control stage=spotlight_revert brightness=100 outcome=completed state=Off
gate_camera_control stage=spotlight_revert attempt=2 brightness=100 outcome=camera_busy retry_after=10 state=Off
gate_camera_control stage=spotlight_startup_revert state=Off
gate_camera_control stage=spotlight_startup_reconcile observed=On state=Off
gate_camera_control stage=spotlight_lease_corrupt state=Off
gate_camera_control stage=spotlight_idempotent_in_flight
gate_camera_control stage=spotlight_rate_limited retry_after=2
gate_camera_control stage=breaker_open retry_after=60
gate_camera_control stage=camera_busy retry_after=60
gate_camera_control stage=camera_unreachable
gate_camera_control stage=camera_error
gate_camera_control stage=snapshot bytes=284119 outcome=completed
gate_camera_control stage=snapshot_rate_limited retry_after=2
gate_camera_control stage=state_rate_limited retry_after=1
gate_camera_control stage=ir_rate_limited retry_after=2
```

Each light names itself: the illuminator's stages are the ones the existing
runbooks already grep for, and every spotlight stage is prefixed `spotlight_`.

No credential, token, camera address, or camera payload ever appears — the same
rule as the webhook listener.

```bash
journalctl -u gate-camera-control -f
journalctl -u gate-camera-control --since -1h | grep 'stage=ir_'
journalctl -u gate-camera-control --since -1h | grep 'stage=spotlight_'
```

## Failure modes

| Failure | Behaviour |
| --- | --- |
| Camera 502 after a login storm | breaker 60 s, `503 camera_busy` with `retry_after`, no auto-retry |
| Camera unreachable | `503 camera_unreachable` and the breaker opens for 60 s, so the retries after it are `503 camera_busy`; `state.json` keeps `last_error` and its own `observed_at`, so the app says "unknown", never "Off" |
| Nothing has called the camera yet | `state.json` reads `not_observed`, not `camera_unreachable`; the 30 s background refresh clears it without an operator doing anything |
| A read flood during an expiring lease | reads are single-flighted, skipped while a revert is queued, and rate limited; the revert waits for at most one in-flight call |
| Service restart mid-lease | each lease is persisted **before** the camera is changed — IR to `/var/lib/gate-camera/lease.json`, the spotlight to `/var/lib/gate-camera/spotlight-lease.json`; on start the service restores **both** lights to their configured defaults before it serves a single request, then clears the records. A lost revert timer can never leave a light in the temporary state |
| One light's lease record lost, or one light refusing | the other light is unaffected: separate records, separate reverts, separate retry backoffs, and one `RevertWorker` that runs both even when one of them is failing. A record naming a state the light cannot hold — an `Auto` in the spotlight's file — is `*_lease_corrupt`, not a lease |
| Reboot or power cut mid-lease | the record is on durable storage (`StateDirectory=gate-camera`), not in `/run`, which the boot recreates empty — the camera is separately powered, so a lease whose record died with the tmpfs would have been held indefinitely. It is reverted on the next start exactly as a service restart is |
| The lease record is lost or unreadable anyway | on start, with no usable record, the service **reads** the camera once: a state that is not `GATE_CAMERA_IR_DEFAULT` is put back and journaled `startup_reconcile`; a state that matches it, or a camera that will not answer, is left alone. That read is one `GetIrLights` on the cached token, so a restart loop still cannot become a login storm, and nothing is ever written on the strength of a camera that could not be read. A record that exists but is unusable — bad JSON, or a timestamp outside a year either side of now — is journaled `lease_corrupt` and treated as an expired lease, so the default is restored through the ordinary revert path |
| A set call fails without answering | treated as indeterminate, because the camera may have applied it: the lease record is **kept**, so a later expiry or restart still reverts. A failed revert likewise keeps the outstanding lease rather than assuming success |
| Revert call itself fails | retried with 5/10/20/40/60 s backoff for the life of the process; `ir.revert_failed` stays true in `state.json`; every attempt is journaled |
| Two operators toggling at once | serialised under one lock, last write wins, both journaled |
| Heartbeat lag | heartbeat-derived state can be ~30 s stale; confirm with `GET /camera/state` after a toggle |
| Camera push interval | the firmware minimum webhook interval is **20 s**, so "did the change help the trigger?" cannot be answered faster than that. Do not imply instant confirmation |

## Install

The service is installed separately from the media stack; it shares no state, no
user, and no environment file with it. **Run these steps in this order.** The
ordering is not cosmetic: the Access application must exist before any DNS name
resolves to this service, or the window between the two is an unauthenticated
camera control on the public internet.

### 1. Write the environment file first

Nothing is published until this file validates, so write it before running the
installer.

```bash
sudo install -o root -g root -m 0600 /dev/null /etc/gate-camera-control.env
sudoedit /etc/gate-camera-control.env      # see the Environment table above
```

Format rules, all enforced: one `KEY=value` per line, **no whitespace** around
the `=` and none at the end of a line, **no quotes** around values (they become
part of the password), and a **trailing newline** on the last line.

### 2. Validate it before installing anything

```bash
sudo python3 gate_media_config.py camera-control --env /etc/gate-camera-control.env
```

Silence means valid. This is the same validator the installer runs, so a
failure here is a failure there — and the installer runs it *before* it
publishes anything, so a rejected file never replaces the running library.

### 3. Run the installer

Run it through `bash`, from the release tree the controller is running:

```bash
RELEASE=$(readlink -f /opt/gate-controller-deploy/current)
sudo bash "$RELEASE/deployment/install-camera-control.sh" --source "$RELEASE"
```

Always invoke it through `bash`, never as `sudo "$RELEASE/deployment/..."`.
The release tree keeps the mode each file has in git, and releases up to
1d98e6e carried this script without its execute bit, so on those releases the
direct form fails with "command not found". The `bash` form works on every
release, and it is the form the updater's own `bash -n` syntax check exercises.
From a git checkout, `--source "$PWD"` does the same thing.

It creates the `gate-camera-control` system user, refuses it if it is in `gpio`
or shares a group with the media or controller services, publishes
`/usr/local/lib/gate-camera-control`, installs the unit and the `/32` drop-in
derived from `GATE_CAMERA_HOST`, creates `/run/gate-camera` and the durable
`/var/lib/gate-camera` (0700) through `/etc/tmpfiles.d/gate-camera.conf` —
the unit's `StateDirectory=gate-camera` creates the latter too — then enables
and starts the service. If the
environment file is empty or invalid it publishes **nothing**, removes the
address drop-in, leaves the service **disabled**, and says so. A failure part way
through stops and disables the service rather than leaving it enabled against
half-published code.

### 4. Verify locally, including the egress pin

```bash
systemctl status gate-camera-control
curl -s http://127.0.0.1:8767/camera/state
curl -s -X POST http://127.0.0.1:8767/camera/ir \
  -H 'Content-Type: application/json' \
  -d '{"state":"Auto","lease_minutes":5}'
curl -s -X POST http://127.0.0.1:8767/camera/spotlight \
  -H 'Content-Type: application/json' \
  -d '{"state":"On","lease_minutes":1}'
curl -s -o /tmp/gate-snap.jpg -w '%{http_code} %{content_type}\n' \
  http://127.0.0.1:8767/camera/snap
```

Then confirm `IPAddressDeny=` actually installed its BPF program, rather than
being silently ignored on a kernel without cgroup BPF — in which case the
service can reach the whole LAN and the isolation claim is void:

```bash
systemctl show gate-camera-control -p IPAddressDeny -p IPAddressAllow
sudo journalctl -u gate-camera-control | grep -i 'ip firewalling\|bpf'
```

`IPAddressAllow` must list the camera's `/32` and loopback, and there must be
**no** "IP firewalling not supported" or "Failed to install BPF" line. If there
is, stop: the address filter is not in force.

### 5. Create the Access application and its service token — before any DNS

In Cloudflare Zero Trust, create a **separate** Access application for the
`gate-camera` hostname and a **separate** service token for it. Do not reuse the
`gate-command` token: the blast radius is different. Both must exist and the
policy must be saved before the hostname resolves anywhere.

### 6. Only then: ingress and DNS — in the Cloudflare account, not on the Pi

The live tunnel is **remotely managed**. The Pi runs
`cloudflared --no-autoupdate tunnel run --token-file /etc/cloudflared/token`;
the tunnel's configuration source is `cloudflare`, and there is no
`/etc/cloudflared/config.yml` and no `cert.pem` anywhere on the Pi. Its ingress
rules and DNS records live in the Cloudflare account, so nothing in this step
touches the Pi and nothing on it needs a reload. In particular
`cloudflared tunnel route dns` **cannot** run on the Pi — it fails with "Error
locating origin cert" — and `deployment/cloudflared/gate-controller-tunnel.yml`
is only a reference for the shape of the ingress list, **not** the live
configuration; editing it changes nothing.

Use either of the two routes below. Both must leave the `http_status:404`
catch-all as the last rule.

**Zero Trust dashboard.** Networks → Tunnels → the gate tunnel → Public
Hostname → Add a public hostname: hostname `gate-camera.example.com`, type
`HTTP`, URL `127.0.0.1:8767`. Saving creates the ingress rule *and* the proxied
CNAME to `<tunnel-id>.cfargotunnel.com` in one step, which is exactly why step
5 has to be complete first: the hostname resolves the moment you save.

**API.** The configurations endpoint replaces the *whole* ingress list, so read
it first and send it back with the new rule inserted ahead of the catch-all and
every existing rule kept exactly as returned. Run this from a workstation with
an API token holding *Account → Cloudflare Tunnel → Edit* and *Zone → DNS →
Edit*; the Pi holds only the connector token, which cannot change the tunnel's
configuration.

```bash
ACCOUNT_ID=<account-id>; TUNNEL_ID=<tunnel-id>; ZONE_ID=<zone-id>
CONFIG="https://api.cloudflare.com/client/v4/accounts/$ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/configurations"
curl -s "$CONFIG" -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" | jq .result.config.ingress
curl -s -X PUT "$CONFIG" -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" --json '{
  "config": {"ingress": [
    {"hostname": "gate-command.example.com", "service": "http://127.0.0.1:8765"},
    {"hostname": "gate-media.example.com",   "service": "http://127.0.0.1:8891"},
    {"hostname": "gate-camera.example.com",  "service": "http://127.0.0.1:8767"},
    {"service": "http_status:404"}
  ]}
}'
curl -s -X POST "https://api.cloudflare.com/client/v4/zones/$ZONE_ID/dns_records" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" --json '{
  "type": "CNAME", "proxied": true,
  "name": "gate-camera.example.com", "content": "'"$TUNNEL_ID"'.cfargotunnel.com"
}'
```

The connector picks the new configuration up over its existing connection, so
there is no `systemctl reload` or restart on the Pi. Confirm it arrived, then
confirm from anywhere *off* the Pi that Access is in front of the hostname:

```bash
sudo journalctl -u cloudflared -n 50 --no-pager | grep -i 'configuration'
curl -sI https://gate-camera.example.com/camera/state | head -1
```

The journal must show a configuration update line. The `curl` must get an
Access response — a `302` to the login page or a `403` — and never the
tunnel's `404` (the rule did not apply) or a `200` (nothing is in front of the
service: remove the DNS record immediately and go back to step 5).

### 7. Store the service token in the Worker

Put the token's client ID and secret in the Worker's secrets, exactly as the
`gate-command` token is stored. The token never goes near the Pi.

### Re-run the installer on every controller release

The controller's own activation does **not** republish
`/usr/local/lib/gate-camera-control` — that path is outside the managed release
tree on purpose, so an auto-update cannot silently change the one process that
holds camera credentials. The cost is that a release carrying a change to
`gate_camera_control/` or to `gate_media_config.py` does not reach the running
service until the installer is re-run:

```bash
RELEASE=/opt/gate-controller-deploy/releases/<sha>   # or $(readlink -f /opt/gate-controller-deploy/current)
sudo bash "$RELEASE/deployment/install-camera-control.sh" --source "$RELEASE"
```

As in step 3, invoke it through `bash`: releases up to 1d98e6e publish the
script mode 0644, so the direct `sudo "$RELEASE/deployment/..."` form fails
there with "command not found".

## Rollback

The service is additive: nothing else in the pipeline depends on it. Removing it
returns the deployment to exactly today's behaviour, except that the app's
`camera_control` heartbeat block reads `not_configured`.

```bash
# 1. Make sure BOTH lights are back at their configured defaults before stopping
#    the service, because a stopped service cannot run its reverts. Setting a
#    state to that light's configured default *is* the cancel, so read each
#    default rather than assuming it: GATE_CAMERA_IR_DEFAULT may be Auto, and
#    posting a hard-coded "Off" at such a deployment creates a lease instead of
#    ending one. The spotlight is the light a stopped service would leave
#    burning where the road can see it, so do not skip it.
state=$(curl -s http://127.0.0.1:8767/camera/state)
default=$(printf '%s' "$state" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["ir"]["default"])')
spotlight_default=$(printf '%s' "$state" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["spotlight"]["default"])')
curl -s -X POST http://127.0.0.1:8767/camera/ir \
  -H 'Content-Type: application/json' -d "{\"state\":\"$default\"}"
curl -s -X POST http://127.0.0.1:8767/camera/spotlight \
  -H 'Content-Type: application/json' -d "{\"state\":\"$spotlight_default\"}"

# 2. Stop and disable.
sudo systemctl disable --now gate-camera-control.service

# 3. Optional: remove the artifacts and the credentials.
sudo rm -f /etc/systemd/system/gate-camera-control.service
sudo rm -rf /etc/systemd/system/gate-camera-control.service.d
sudo rm -f /etc/tmpfiles.d/gate-camera.conf
sudo rm -rf /usr/local/lib/gate-camera-control /run/gate-camera /var/lib/gate-camera
sudo rm -f /etc/gate-camera-control.env
sudo systemctl daemon-reload
```

If a light was left on and the service is already gone, set it back from the
camera's web interface, or for IR from the Pi with the documented rollback in
[Front Gate camera night configuration](reviews/2026-09-06-camera-night-configuration.md)
§ rollback.

Finally, remove the `gate-camera` ingress hostname from the tunnel config and
delete its Access application and service token.

## Not implemented: the "clearer" live stream

Issue #92 §5 proposes a second, sharper live path. It is **not** implemented here,
and it should not be attempted until one measurement is taken, because 4K live is
not deliverable to a phone over this link:

- clear/main is 3840x2160 H.265 at 6144 Kbit/s while the property uplink measures
  about 4.5 Mbit/s — the stream alone exceeds the uplink, before TURN overhead;
- browsers have effectively no H.265 WebRTC support, so `-c:v copy` cannot serve
  the clear path;
- the Pi 5 has no hardware H.264 encoder, and 4K software decode alone already
  measures 8.2 s of CPU per second of work next to the recognition workload.

So the only honest sharp option today is the **4K still** above, which this
service does implement.

**The open question is whether this firmware exposes a third (`ext`/balanced)
encoder profile** — typically 1280x720 H.264 at about 1 Mbit/s, which would fit
the uplink and could be copied rather than transcoded. Check it on the Pi:

```bash
python3 /root/camtool.py raw '[{"cmd":"GetEnc","action":1,"param":{"channel":0}}]'
```

If `GetEnc` reports an `extStream` block with an H.264 profile at roughly 1 Mbit/s
or less, the work is a `-c:v copy` path and is worth doing:

- `deployment/media/mediamtx.yml`: add a `clear_view` source path and a
  `gate_clear` publisher path;
- `gate_media_transcoder/__main__.py`: select between **two fixed** pipelines by
  `argv[1]` (`gate` | `gate_clear`) — never accept a URL, and keep it
  credential-free;
- `deployment/systemd/gate-media-transcoder.service` becomes the template unit
  `gate-media-transcoder@.service`; each instance costs the current `CPUQuota=20%`
  Opus encode again;
- `gate_media_auth/__main__.py`: extend `_allows_local_rtsp` with
  `("publish","gate_clear")` and `("read","clear_view")`, and `_allows_viewer_read`
  to accept path `gate_clear`;
- `gate_media_auth/token.py`: `_validate_claims` currently pins `claims["path"]`
  to `"gate"`; accept the exact set `{"gate","gate_clear"}` and keep every other
  claim pinned;
- `deployment/media/nginx-whep-locations.conf.template`: add the `/gate_clear/whep`
  create and teardown locations.

If `GetEnc` shows no such profile, live selection reduces to "Fluent live plus the
on-demand 4K still", and the app should not render a `Clearer` option at all
(access-gate-ui#33 §4 hides it unless the controller reports the second path
ready).
