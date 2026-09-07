# Plate Matching Levels And Schedule

The controller decides whether an OCR read may open the gate. How far a read is
allowed to stray from an authorised plate is a *level*, and which level applies
is a *schedule* of local-time bands. Both are managed from Gate Mate; the
controller polls them and fails closed whenever anything about them is unclear.

## Levels

| Level | What opens the gate | Edit budget |
| --- | --- | --- |
| `strict` | The normalised plate exactly, at confidence ≥ 0.90. Nothing else. | 0 |
| `standard` | Exact, or equal length with exactly one known OCR-confusion substitution, on two high-confidence frames, against a single authorised candidate. | 1, confusion pairs only |
| `relaxed` | Exact, or up to two edits (substitution, insertion, deletion) on two high-confidence frames, against a single authorised candidate, for reads of at least six characters. | 2, any character |

`standard` is what the controller has always done. The confusion pairs are
`0/O`, `1/I/L`, `2/Z`, `5/S`, and `8/B`.

Every level keeps the protections that were already there:

- exact matching is tried first and wins outright;
- a non-exact match needs two frames that read the same plate at confidence
  ≥ 0.90 — one frame never opens the gate on a fuzzy read;
- a read that is close to **more than one** authorised plate is denied as
  `ambiguous_fuzzy_match`, never resolved by picking a favourite;
- the authorised snapshot is re-checked under the relay lock immediately
  before activation, so a plate revoked mid-decision does not open the gate.

`relaxed` is materially riskier than `standard`: two free edits over a
seven-character plate will match neighbouring registrations. It exists because
some sites would rather let a familiar car in than turn it away, but it should
not be the overnight setting.

## Schedule

A schedule is a list of local-time bands, each naming a level. Bands must tile
the 24-hour clock exactly once — no gaps, no overlaps. A band may wrap past
midnight (`22:00-08:00`). Times are in the site's timezone, `Europe/Dublin` by
default, so the boundaries follow Irish Summer Time without anyone editing
them twice a year.

The schedule Gate Mate offers as a starting point:

| Band | Level |
| --- | --- |
| 08:00-22:00 | `standard` |
| 22:00-08:00 | `strict` |

With **no schedule configured at all**, the controller behaves exactly as it
did before this feature existed: `standard`, around the clock.

## Settings Channel

The controller polls `GET /api/controller/settings?controller_id=...` every
`GATE_SETTINGS_REFRESH_SECONDS` seconds (default 60), authenticated with the
same Cloudflare Access service token as the plate snapshot.

```json
{
  "controller_id": "primary",
  "settings_version": 1,
  "updated_at": "2026-09-07T09:00:00Z",
  "plate_matching": {
    "schema_version": 1,
    "timezone": "Europe/Dublin",
    "bands": [
      {"start": "08:00", "end": "22:00", "level": "standard"},
      {"start": "22:00", "end": "08:00", "level": "strict"}
    ]
  }
}
```

- `settings_version` versions the envelope; `plate_matching.schema_version`
  versions the policy document inside it. A controller that meets a *newer*
  version of either rejects the document rather than guessing.
- A good document is cached at `match-policy.json` beside the database, so a
  restart during a cloud outage keeps the schedule the owner configured.
- `plate_matching` may be absent. That means "no schedule configured" and
  keeps today's behaviour.

## Failing Closed

| Situation | Result |
| --- | --- |
| No settings channel configured, or no `plate_matching` in the document | `standard` all day — today's behaviour |
| Document rejected (gap, overlap, bad time, unknown timezone, newer version) | `strict` all day, until a readable document arrives |
| A band names a level this controller does not know | That band becomes `strict`; the rest of the schedule stands |
| The timezone cannot be loaded on this host | The strictest level *in the configured schedule*, for every decision |
| Settings fetch fails | The last good schedule stays in force; the failure is reported in the heartbeat |
| The policy cache itself raises | `standard` — the shipped default, logged as `match_policy status=unavailable` |

## What An Event Records

Every OCR decision carries a `match_policy` block in its V3 telemetry, both in
the local journal and on the wire to Cloudflare:

```json
"match_policy": {
  "band": "08:00-22:00",
  "level": "standard",
  "timezone": "Europe/Dublin",
  "local_time": "14:07",
  "rule": "ocr_confusion",
  "edit_distance": 1,
  "observed_plate": "12O3456",
  "authorised_plate": "1203456"
}
```

`rule` is `exact`, `ocr_confusion`, or `edit_distance`. On a denial the block
carries `near_miss_plate` and `near_miss_distance` instead of
`authorised_plate` — the closest authorised plate and how far away it was — so
the owner can tell "the schedule turned this car away" from "the camera could
not read it". The near miss is computed for review only and never widens a
match.

The wire keys are frozen by the Cloudflare ingest contract. Adding one here
without extending the Worker's `MATCH_POLICY_KEYS` allowlist first will make
the Worker reject the whole event.

## Deployment Order

`match_policy` is a new block in the telemetry envelope and the Worker
validates telemetry against a strict key allowlist. **Deploy the Gate Mate
Worker before this controller release.** A controller sending `match_policy`
to a Worker that has not learned the key yet will have its events rejected
with `400 Invalid controller event`.
