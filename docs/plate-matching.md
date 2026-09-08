# Plate Matching Levels And Schedule

The controller decides whether an OCR read may open the gate. How far a read is
allowed to stray from an authorised plate is a *level*, and which level applies
is a *schedule* of local-time bands. Both are managed from Gate Mate; the
controller polls them and fails closed whenever anything about them is unclear.

## Levels

| Level | What opens the gate | Edit budget |
| --- | --- | --- |
| `strict` | The normalised plate exactly, at confidence ≥ 0.90. Nothing else. | 0 |
| `standard` | Exact at confidence ≥ 0.75, or equal length with exactly one known OCR-confusion substitution, on two frames at confidence ≥ 0.85, against a single authorised candidate. | 1, confusion pairs only |

Those are the only two levels. A band naming anything else — including the
withdrawn `relaxed`, below — becomes `strict` on its own, without taking the
rest of the schedule down.

`standard` is what the controller has always done. The confusion pairs are
`0/O`, `1/I/L`, `2/Z`, `5/S`, and `8/B`.

Every level keeps the protections that were already there:

- exact matching is tried first and wins outright;
- a non-exact match needs two frames that read the same plate at the level's
  fuzzy confidence — one frame never opens the gate on a fuzzy read;
- a read that is close to **more than one** authorised plate is denied as
  `ambiguous_fuzzy_match`, never resolved by picking a favourite;
- the authorised snapshot is re-checked under the relay lock immediately
  before activation, so a plate revoked mid-decision does not open the gate.

## Confidence Bars

Each level carries its own bars, and each is tunable from the controller's
environment. The bars are **not** part of the settings document Gate Mate
sends: that schema is unchanged, and the app still chooses only the level per
band. These are a site's own risk posture for the hardware it has.

| Variable | Default | What it gates |
| --- | --- | --- |
| `GATE_MATCH_MIN_CONFIDENCE_STANDARD` | `0.75` | An exact match by day |
| `GATE_MATCH_MIN_CONFIDENCE_STRICT` | `0.90` | An exact match overnight |
| `GATE_MATCH_MIN_FUZZY_CONFIDENCE_STANDARD` | `0.85` | Each frame of a one-confusion match by day |
| `GATE_MATCH_MIN_FUZZY_CONFIDENCE_STRICT` | `0.90` | Unused: `strict` allows no fuzzy match |
| `GATE_MATCH_AGREEMENT_MIN_LOCAL_CONFIDENCE_STANDARD` | `0.50` | The on-device read, when both readers agree, by day |
| `GATE_MATCH_AGREEMENT_MIN_CLOUD_CONFIDENCE_STANDARD` | `0.70` | The cloud read, when both readers agree, by day |
| `GATE_MATCH_AGREEMENT_MIN_LOCAL_CONFIDENCE_STRICT` | `0.90` | As above, overnight — the historical bar, so agreement adds nothing |
| `GATE_MATCH_AGREEMENT_MIN_CLOUD_CONFIDENCE_STRICT` | `0.90` | As above, overnight |

A value must be a finite number in **0.10-1**. Anything else — `nan`, `inf`,
out of range, unparseable — is refused and that one bar falls back to **0.90**,
the value the controller required before these knobs existed, logged once at
start-up as `match_confidence key=… status=rejected using=0.90`. A mistyped
knob therefore never widens the gate, not even to this release's own default.

The floor is 0.10 rather than 0 because a bar no read can fail is not a bar:
`GATE_MATCH_MIN_CONFIDENCE_STANDARD=0` would open the gate on a 0.0-confidence
exact read, and `1e-9` is the same thing with a decimal point in it. Both
readers score far above 0.10 even when they are wrong (0.772 on wrong reads
against 0.998 on correct ones, measured here), and the laxest bar anything
ships with is the 0.50 local agreement bar, so 0.10 leaves every real posture
reachable while refusing a disabled one.

`GATE_MATCH_AGREEMENT_MIN_LOCAL_CONFIDENCE_*` has a second floor under it that
does not live here: `GATE_LOCAL_OCR_MIN_CONFIDENCE` (default `0.5`) is the
on-device recogniser's own admission gate, and a read below it never reaches
the matcher to corroborate anything. The effective local agreement bar is the
larger of the two. See
[local recognition](local-recognition.md#environment-variables).

### Why the daytime exact bar is 0.75

An exact match is a character-for-character equality with an authorised plate.
For a low-confidence read to open the gate wrongly, the reader has to
hallucinate one specific eight-character Irish registration; a doubtful read
produces a wrong string, not a different owner's valid one. The old 0.90
turned away a cloud read of `10CE1990` at 0.806 on 2026-09-08 with the
authorised car sitting at the gate. 0.75 admits that read with margin and
still discards the genuinely unreadable.

A *fuzzy* match is deliberately not the authorised plate, so its bar stays
higher at 0.85, and it still needs the two frames and the unique candidate.

Overnight, `strict` is untouched: every bar stays at 0.90.

## When Both Readers Agree

When the on-device recogniser and the cloud service independently produce the
**same normalised plate for the same event**, each only has to clear its
agreement bar rather than the full one. Two readers, two different models,
producing the same seven or eight characters is a stronger signal than either
score alone; that is what buys the lower bar.

It is narrow by construction:

- both readers must be present. One reader agreeing with itself across two
  frames is the two-frame fuzzy rule, not this one, and a controller running
  without `GATE_LOCAL_OCR_MODE=active` can never reach it at all;
- the agreed plate must still authorise **under the band's own rule**: exact
  membership, or that band's one-confusion rule against a single authorised
  candidate, on the number of frames that band requires **of each reader
  separately**. The frame count the rule is tested against is the *smaller* of
  the two readers' counts, so a reader that saw the plate twice cannot cover
  for one that saw it once: that is the one-reader case, and the two-frame
  fuzzy rule already covers it at a much higher bar. Agreement lowers what a
  read has to carry. It never buys an extra edit, and it never buys a frame
  from the other reader's count;
- reads of one event never corroborate another: the on-device reads are keyed
  by trace id;
- a non-finite confidence is not a confidence and is dropped before any
  comparison.

A grant this rule produced is journalled distinctly on the Pi:

```
gate_match stage=agreement_grant plate=10CE1990 local_score=0.566 \
  cloud_score=0.806 match_rule=exact level=standard band=08:00-22:00 \
  min_local=0.50 min_cloud=0.70
```

On the wire the event keeps the reason it would otherwise carry —
`exact_match` or `two_frame_ocr_confusion` — and `match_policy.rule` keeps
saying `exact` or `ocr_confusion`, because both are already in the Worker's
allowlists. Nothing new is sent. See "What the app cannot show yet" below.

## Why There Is No `relaxed` Level

An earlier draft of this feature carried a third level, `relaxed`: up to two
edits of any kind — substitution, insertion, or deletion — against a read of at
least six characters. It was withdrawn before release, and `relaxed` is now
treated as a level this controller has never heard of.

It was measured against this matching code. With `131-D-2696` as the only
authorised plate, `relaxed` returned **allowed for 101 other syntactically
valid Irish registrations at edit distance 1 and 3,360 at distance 2 — 3,461 in
total**. Among them `131-D-2695`, `132-D-2696`, `141-D-2696` and `131-C-2696`:
real registrations, all of them different vehicles.

The reason is structural, not a tuning problem. An Irish registration puts the
year and the county in its first three or four characters, so plates differing
only by year or by county are one edit apart by construction. Neither guard the
level relied on closes that hole:

- the **two-frame rule** protects against OCR noise, not against a stranger's
  plate that the camera read perfectly twice;
- the **unique-candidate rule** only fires when a *second* authorised plate
  happens to sit within the same budget, which a single-vehicle household does
  not have.

`gate_controller/matching.py` still contains the free-edit branch, guarded by
`LevelRule.confusion_only`, for a future level that can justify it. No level in
`LEVELS` sets it, so nothing a settings document can say will reach it.

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
- A *rejected* document writes `match-policy.json.rejected` beside that cache.
  Without the marker a restart would quietly reinstate the cached schedule
  while the cloud was still serving the document that was refused, handing the
  gate back exactly the fuzziness the rejection removed. The marker is cleared
  by the first readable document. A marker that cannot be parsed still counts
  as a marker: the gate stays closed and the reason becomes generic.
- `plate_matching` may be absent. That means "no schedule configured" and
  keeps today's behaviour.

## Failing Closed

| Situation | Result |
| --- | --- |
| No settings channel configured, or no `plate_matching` in the document | `standard` all day — today's behaviour |
| Document rejected (gap, overlap, bad time, unknown timezone, newer version) | `strict` all day, until a readable document arrives |
| **Restart after a rejection, cloud still serving the same document** | **`strict` all day — the rejection marker outlives the process; the cached schedule is not reinstated** |
| A band names a level this controller does not know, `relaxed` included | That band becomes `strict`; the rest of the schedule stands |
| The timezone cannot be loaded on this host | The strictest level *in the configured schedule*, for every decision |
| A naive (timezone-less) datetime reaches the schedule | Read as UTC, never as local wall time — reading it as local would misfile a 22:30 decision into the daytime band all summer |
| Settings fetch fails | The last good schedule stays in force; the failure is reported in the heartbeat |
| The policy cache itself raises | `standard` — the shipped default, logged as `match_policy status=unavailable` |

## What The Heartbeat Reports

The controller status payload carries a `match_policy` block, and the key names
are a contract with the Gate Mate Worker, which narrows the heartbeat against
an allowlist and silently drops anything it does not recognise:

```json
"match_policy": {
  "configured": true,
  "bands": [
    {"start": "08:00", "end": "22:00", "level": "standard"},
    {"start": "22:00", "end": "08:00", "level": "strict"}
  ],
  "timezone": "Europe/Dublin",
  "refreshed_at": "2026-09-07T09:01:00+00:00",
  "last_error": null
}
```

`configured` is false until a document has been adopted or refused;
`last_error` is the rejection or fetch failure, collapsed to one line and
truncated to 200 characters. Both matter: a controller that has fallen closed
reports `configured: true` with a `last_error`, and the Settings page says so
rather than showing a healthy badge. `refreshed_at` only advances on a document
that was *accepted*, so it also serves as the staleness signal.

Renaming any of these keys means renaming them in
`worker/routes/controller.ts` in the same change.

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

`rule` is `exact` or `ocr_confusion` (`edit_distance` belongs to the withdrawn
level and no shipped level emits it). On a denial the block
carries `near_miss_plate` and `near_miss_distance` instead of
`authorised_plate` — the closest authorised plate and how far away it was — so
the owner can tell "the schedule turned this car away" from "the camera could
not read it". The near miss is computed for review only and never widens a
match.

The wire keys are frozen by the Cloudflare ingest contract. Adding one here
without extending the Worker's `MATCH_POLICY_KEYS` allowlist first will make
the Worker reject the whole event.

## What The App Cannot Show Yet

An agreement grant is indistinguishable from an ordinary one in Gate Mate
today, and deliberately so: every value it sends is already on the Worker's
allowlists. Three app changes would make it visible, in
`~/dev/access-gate-ui`:

1. `worker/contracts/gate-event-ingest/contract.ts:145`
   (`LOCAL_OCR_DECISION_SOURCE`) accepts only `local|cloud|none`. An
   `agreement` value there would say which rule opened the gate.
2. `worker/contracts/gate-event-ingest/contract.ts:144`
   (`LOCAL_OCR_AUTHORISED`) accepts only `local_match|cloud_match|both|none`.
   An agreement grant is currently journalled `none`, because each reader is
   still labelled at the full bar it did not clear.
3. `src/lib/passage.ts:321` (`REASON_SENTENCE`) has no wording for a grant
   that rests on two readers agreeing; it would need one only if the reason
   string ever changes, which it has not.

Until those land, the Pi journal is the record: `gate_match
stage=agreement_grant` above, beside the `gate_local_ocr` line for the frame.

## Deployment Order

`match_policy` is a new block in the telemetry envelope and the Worker
validates telemetry against a strict key allowlist. **Deploy the Gate Mate
Worker before this controller release.** A controller sending `match_policy`
to a Worker that has not learned the key yet will have its events rejected
with `400 Invalid controller event`.
