# Fast, Reliable Gate Controller Design

## Goals

- Minimise the time between a complete camera image arriving and relay activation.
- Improve night recognition without allowing unsafe partial-plate matches.
- Keep automatic recognition and manual opening available during optional cloud-service outages.
- Give the web application an authenticated, audited command channel and honest controller status.
- Provide optional fixed audio prompts without making the controller depend on audio hardware.

## Safety Rules

- OCR, network, parsing and ambiguity failures fail closed.
- Exact normalised plate matches are preferred.
- A fuzzy match must have equal length, exactly one known OCR-confusion substitution, high OCR confidence, two-frame consensus, and a unique authorised candidate.
- Vehicle make and colour are supporting audit data and never independently authorise entry.
- Relay commands are serialised, idempotent, expire quickly and respect a persisted cooldown.
- Remote logging, image upload, email and analytics never run before relay activation.
- The camera, RTSP credentials and GPIO relay are never exposed directly to the public browser.

## Runtime Architecture

The system runs as one long-lived Python process. A watchdog observer receives completed or moved JPEG files and sends paths to a burst collector. The collector waits a short configurable window, removes incomplete images and ranks frames by sharpness. The sharpest frame is sent to Plate Recognizer first. A confident exact match can open immediately; ambiguous results can use up to two more ranked frames.

`GateProcessor` coordinates pure matching policy, the OCR client, local SQLite state and `RelayController`. `RelayController` activates the hardware before the processor records or queues optional remote work. A background outbox worker performs S3/PostgreSQL synchronisation. A second background control-plane worker polls Supabase for short-lived commands and publishes controller heartbeats.

## Command Contract

The browser invokes a Supabase `request_gate_command` RPC. The database records the authenticated requester, idempotency key, expiry and status. The Pi claims one command through a service-role-only RPC, validates its type and expiry, performs it, and acknowledges completion or failure. The first supported commands are `open_gate` and `play_prompt`; fixed prompt keys map to local files and arbitrary shell commands or file paths are forbidden.

## Observability

Each event records separate OCR confidence, match score, decision reason and stage timings. Heartbeats report the controller's last-seen time, latest camera image, queue depth and optional audio capability. Performance targets are a median relay activation below two seconds and a 95th percentile below four seconds, measured from receipt of a complete image while the OCR service is available.

## Rollout

The existing FTP path remains supported for the new Reolink camera. Camera-specific RTSP and MediaMTX configuration is documented but live media remains an independently configurable URL. Existing SQLite data is retained and migrated in place. Missing Supabase, S3, SMTP or audio configuration disables only that optional feature.
