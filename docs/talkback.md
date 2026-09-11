# Push-to-talk (talkback) to the gate camera

The RLC-811A fitted on 2026-09-11 has a speaker. This document is the design,
the security model, the contract, and the acceptance procedure for the
push-to-talk path that drives it. Read
[Gate camera control](camera-control.md) first: talkback is an extension of
that service, not a new one, and every rule there still applies.

Until the physical acceptance test at the end of this document has been done
on the fitted camera, `GATE_MEDIA_TALKBACK_VERIFIED` stays `false` and the app
does not show the button. Nothing in this document changes that order.

## Why Baichuan, and why not reolink_aio

Reolink cameras take talk-back audio only over their proprietary Baichuan
protocol on TCP 9000. There is no RTSP backchannel on Reolink firmware and no
`api.cgi` command for it, so the media gateway's existing RTSP source cannot
carry audio *to* the camera.

Two implementations were evaluated on 2026-09-11:

| Candidate | Finding |
| --- | --- |
| `reolink_aio` 0.21.16 | Implements the Baichuan transport, nonce login, and AES-CFB encryption, but **no talk messages** — the only "talk" it knows is the `talkAndReplyVolume` setting. It also pulls in `aiohttp`, `aiortsp`, `orjson`, `pycryptodomex` and `typing_extensions`, none of which belong in the one process that holds camera credentials. |
| neolink (Rust) | Implements talk end to end: `TalkAbility` (10), `TalkConfig` (201), `Talk` (202) carrying `BcMedia` ADPCM frames, `TalkReset` (11). Its wire format is the reference for what follows. |

The result is [`gate_camera_control/baichuan.py`](../gate_camera_control/baichuan.py):
a client written against `socket` and `hashlib` alone, with a pure-Python
AES-128-CFB in [`aes.py`](../gate_camera_control/aes.py) (a few kilobytes of
control XML per session; the audio itself is unencrypted, as the camera
expects) and the DVI ADPCM encoder in [`adpcm.py`](../gate_camera_control/adpcm.py).
Its message set is closed to exactly the six messages above plus `Login` and
`Logout`; there is no generic send. Every primitive was cross-checked against
`reolink_aio` and `pycryptodomex` byte for byte, and the ADPCM blocks decode
with ffmpeg's stock `adpcm_ima_wav` decoder (`tests/test_talkback_ffmpeg.py`).

## The path

```text
browser microphone
  │  Opus over WebRTC (WHIP), on a 60 s `publish`/`talk` token from the Worker
  ▼
Worker /api/media/whip  ──►  Cloudflare Tunnel  ──►  nginx /talk/whip  ──►  MediaMTX path `talk`
                                                                                │  loopback RTSP
                                                                                ▼
                                            gate-camera-control: ffmpeg → PCM16 → DVI ADPCM → Baichuan :9000 → camera speaker
```

1. The operator presses **Hold to talk**. The app asks the Worker for a talk
   session (`POST /api/media/talk-session`).
2. The Worker requires role `operator` or `admin` and a heartbeat that reports
   `media.talkback` configured, ready and verified, then **arms** the Pi:
   `POST /camera/talk` on the camera-control service through its own tunnel
   hostname and service token. Only if that succeeds does it sign a
   `publish`/`talk` token and return the WHIP URL.
3. The browser captures the microphone, offers a send-only Opus track, and
   `POST`s the offer to `/api/media/whip`, which the Worker proxies to the
   Pi's `/talk/whip`. The auth sidecar accepts the publish only for a token
   whose claims are exactly `path: talk, actions: ["publish"]`.
4. The camera-control service, armed, polls the gateway's loopback API until
   the `talk` path has a publisher, then runs one fixed ffmpeg pipeline
   (`rtsp://127.0.0.1:8554/talk` → PCM16 mono at the camera's sample rate on
   stdout), encodes DVI ADPCM blocks of the camera's `lengthPerEncoder`, and
   sends each as one `Talk` message, waiting for its acknowledgement.
5. The session ends at the first of: the operator releasing the button (the
   browser deletes its WHIP resource and MediaMTX drops the path, so ffmpeg
   sees EOF); the Worker's `DELETE /camera/talk`; the **hard limit**; a camera
   error. At every exit the service sends `TalkReset`, logs out, and kicks the
   WHIP publisher from the gateway so the app sees the end.

## Security model

The rules in [camera-control.md § Security model](camera-control.md#security-model)
hold unchanged. Talkback adds these:

| Property | How it is enforced |
| --- | --- |
| Camera credentials stay in one process | The Baichuan client lives in `gate-camera-control` and reads the same `/etc/gate-camera-control.env`. The media gateway, the auth sidecar, the Worker and the browser never see them |
| No new network exposure | The camera-control service still binds loopback and still reaches nothing but loopback and the camera's `/32`; port 9000 is on that same address. The only new public surface is `/talk/whip` on the media hostname, behind the same nginx, Access and token check as `/gate/whep` |
| Off by default, and nothing until enabled | `GATE_CAMERA_TALK_ENABLED=false` means the service never opens port 9000. With it `true`, the only unsolicited traffic is one `TalkAbility` probe at start, hourly once ready, every 15 min while not |
| Bounded sessions with a hard time limit | One session at a time. `GATE_CAMERA_TALK_MAX_SECONDS` (5–60, default 30) is a **kill** of ffmpeg at the deadline from a timer thread, not a cooperative check a stalled read could miss. A publisher that never arrives ends the session at 10 s. The token is 60 s. Nothing here can be extended |
| The browser cannot pick the camera or the format | The WHIP path is fixed to `talk`; ffmpeg's command is fixed and credential-free; the ADPCM format comes from the camera's own `TalkAbility` and its tokens are ASCII-checked before they are echoed back in `TalkConfig` |
| Only operators and admins | Enforced twice: the Worker refuses the talk session and the WHIP proxy for viewers, and the media-session contract only ever reports `talkback: true` to those roles |
| No payload leaks | Errors are typed with no camera body attached; journal lines carry stage, outcome, block count and a truncated session id; the state file names no camera, address or credential |
| The gateway cannot be talked into anything else | The auth sidecar allows exactly `("read","talk")` over loopback RTSP (the forwarder) and `("publish","talk")` over WebRTC on a talk token. A gate token cannot publish; a talk token cannot read |

## Capability flow

`media.talkback` in the controller heartbeat is now canonical like `video` and
`listen`:

| Field | Source |
| --- | --- |
| `configured` | `GATE_MEDIA_TALKBACK_CONFIGURED=true` in `/etc/gate-media-auth.env` |
| `ready` | configured **and** the gateway's `gate` path is up (MediaMTX is running to carry the WHIP publish) **and** `/run/gate-camera/state.json` is fresh and says `talkback.available: true` — the camera answered a `TalkAbility` probe and ffmpeg is present |
| `verified` | ready **and** `GATE_MEDIA_TALKBACK_VERIFIED=true`, which is set only after the acceptance test below |

The camera-control state file gains one block, read by
`gate_controller/camera_control_state.py` with the same strictness as the IR
block and defaulted to not-enabled when a pre-talkback service publishes
nothing:

```json
"talkback": {"available": true, "reason": "ready", "active": false}
```

`reason` is one of `ready`, `not_enabled`, `not_probed`, `ffmpeg_missing`,
`unsupported`, `camera_auth`, `camera_busy`, `camera_unreachable`,
`camera_error`.

## HTTP contract (camera-control service)

Same base URL, budgets and error shapes as [camera-control.md](camera-control.md#http-contract).
`POST /camera/talk` has its own budget: 6 at once, then 1 every 2 s. `GET`
shares the state budget.

### `POST /camera/talk`

Body optional; `{"max_seconds": 20}` bounds this session (5–`GATE_CAMERA_TALK_MAX_SECONDS`).
Unknown fields are a `400`.

```json
{
  "observed_at": "2026-09-12T10:04:11+00:00",
  "status": "armed",
  "talk": {
    "available": true, "reason": "ready", "active": true, "max_seconds": 30,
    "state": "armed", "session_id": "…32 hex…",
    "armed_at": "2026-09-12T10:04:11+00:00", "expires_at": "2026-09-12T10:04:41+00:00",
    "seconds_remaining": 30, "last_outcome": null, "last_ended_at": null
  }
}
```

`state` moves through `armed` → `waiting_for_publisher` → `streaming` → `ended`.
`last_outcome` on an ended session is one of `time_limit`, `publisher_gone`,
`released`, `no_publisher`, `talk_busy`, `camera_unreachable`, `camera_error`,
`camera_auth`, `unsupported`, `ffmpeg_failed`.

| Status | Body | When |
| --- | --- | --- |
| `409` | `{"error":"talk_busy"}` | a session is already armed or streaming |
| `503` | `{"error":"talk_unavailable","reason":"…"}` | talk is not enabled, not yet probed, or the last probe failed; `reason` is the state-file reason |

### `GET /camera/talk`, `DELETE /camera/talk`

`GET` returns the `talk` document above without `status`. `DELETE` ends the
current session if there is one and answers `200 {"status":"released", …}`
either way.

## Environment

Two optional keys in `/etc/gate-camera-control.env` (root:root 0600):

| Key | Default | Rules |
| --- | --- | --- |
| `GATE_CAMERA_TALK_ENABLED` | `false` | exactly `true` or `false` |
| `GATE_CAMERA_TALK_MAX_SECONDS` | `30` | whole seconds, 5–60, no leading zero |

One optional key in `/etc/gate-media-auth.env`:

| Key | Default | Rules |
| --- | --- | --- |
| `GATE_MEDIA_TALKBACK_VERIFIED` | `false` | exactly `true` or `false`; `true` requires `GATE_MEDIA_TALKBACK_CONFIGURED=true` |

Both files keep validating without the new keys, so nothing changes on a Pi
that has not opted in.

## Journal

```text
gate_camera_control stage=talk_probe outcome=ready
gate_camera_control stage=talk_armed max_seconds=30 session=3f2a9c1d0b7e
gate_camera_control stage=talk_publisher outcome=ready session=3f2a9c1d0b7e
gate_camera_control stage=talk_stream format=adpcm16000x1024 outcome=started session=3f2a9c1d0b7e
gate_camera_control stage=talk_ended blocks=214 reason=publisher_gone seconds=14.1 session=3f2a9c1d0b7e
gate_camera_control stage=talk_busy
gate_camera_control stage=talk_unavailable reason=camera_unreachable
```

## Resource cost on the Pi

Measured on the Mac and scaled: ffmpeg decoding one Opus stream to PCM is
under 3 % of a core; the pure-Python ADPCM encoder at 16 kHz is about 1.4 ms
of CPU per 64 ms block on the Mac, so roughly 8 % of one Pi 5 core while a
session runs and nothing between sessions. The unit's `MemoryMax` is raised
from 64 M to 128 M for ffmpeg's ~30 MB, and `CPUQuota` from 20 % to 30 %.
None of this touches the controller, the recognition path or the FTP watcher,
which run in other units.

## Enabling it — in this order

1. Deploy the Worker (access-gate-ui) first. Its heartbeat narrowing keeps
   unknown keys, so the new `talkback` block in `camera_control` is harmless
   either way, but the `/api/media/talk-session` route and the `/api/media/whip`
   proxy must exist before the app can use them.
2. Re-run the media installer for the release that carries this change: it
   publishes the `talk` path in `mediamtx.yml`, the `/talk/whip` nginx
   locations, and the sidecar that accepts talk tokens. Every flag stays as it
   is.
3. Add `GATE_CAMERA_TALK_ENABLED=true` to `/etc/gate-camera-control.env`,
   validate it, and re-run `install-camera-control.sh` through `bash` from the
   release tree (the library outside the managed tree is not republished by an
   auto-update; see camera-control.md § Re-run the installer).
4. Watch the journal for `stage=talk_probe outcome=ready`. Any other outcome
   names what to fix; `unsupported` means the camera offered no 16-bit mono
   ADPCM format and this design does not apply to it.
5. Set `GATE_MEDIA_TALKBACK_CONFIGURED=true` in `/etc/gate-media-auth.env` and
   restart `gate-media-auth`. The heartbeat now reports talkback
   `ready` and `hardware_unverified`. The app still hides the button.

## Acceptance test — supervised, at the gate

Two people, or one person and a phone recording at the gate. Nothing about
this test is remote-only: the whole point is a human hearing the speaker.

1. From the app, as an operator, on the Home page with the live feed running.
   The button is hidden until step 5 of the enabling order is done **and** the
   verified flag is set, so for this test drive the Worker route directly or
   temporarily set `GATE_MEDIA_TALKBACK_VERIFIED=true`, restart
   `gate-media-auth`, and put it back to `false` immediately after if the test
   fails.
2. Hold the button and count to five. Expected, at the gate: your voice from
   the camera within about a second, intelligible, no motorboating or gaps.
   Expected in the journal: `talk_armed`, `talk_publisher outcome=ready`,
   `talk_stream outcome=started`, and on release `talk_ended reason=publisher_gone`
   with a block count near 78 per five seconds at 16 kHz.
3. Hold the button past `GATE_CAMERA_TALK_MAX_SECONDS`. Expected: the audio
   stops at the limit, the button drops to **Hold to talk** on its own, the
   journal shows `reason=time_limit`, and `GET /camera/talk` shows
   `active: false`.
4. While one operator is talking, have a second operator press. Expected: the
   second gets "Talk busy" and nothing changes for the first.
5. Pull the camera's Ethernet during a session. Expected: the session ends
   with `camera_unreachable`, the state file drops to `available: false`, and
   the heartbeat reports talkback `gateway_unhealthy` within 30 s; plug it
   back in and the next probe (or the next arm) recovers it.
6. Check the listen path is unaffected: with **Listen to gate** on, the gate's
   audio still arrives during and after a talk session.
7. Confirm the IR lease and the still are unaffected: `GET /camera/state`
   during a session answers normally; `GET /camera/snap` still works.

Only when every step passes: set `GATE_MEDIA_TALKBACK_VERIFIED=true`, restart
`gate-media-auth`, and record the date, firmware version and the
`talk_stream format=` line in `docs/reolink-rlc-811a.md`. From then on the app
shows **Hold to talk** to operators and admins.

## Rollback

Set `GATE_CAMERA_TALK_ENABLED=false` (or remove the key), re-run the
camera-control installer, and set `GATE_MEDIA_TALKBACK_CONFIGURED=false`. The
service stops opening port 9000, the heartbeat reports `not_configured`, and
the app hides the button. Nothing else in the pipeline depends on talkback.

## Not done here

- **Full-duplex.** The camera's `TalkAbility` reports `FDX` and the listen path
  keeps running during a session, but echo cancellation is the browser's
  (`echoCancellation: true` on the microphone track). If the gate speaker is
  audible in the gate microphone at the stop position, expect the operator to
  hear themselves with a delay; a hold-to-talk mute of the listen track is the
  obvious follow-up.
- **The RLC-810A.** It has no speaker. `GATE_CAMERA_TALK_ENABLED` must stay
  `false` on the rollback unit; its `TalkAbility` answer, if any, is untested.
