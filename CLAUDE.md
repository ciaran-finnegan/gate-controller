# Working on gate-controller

This repository drives a physical farm gate. Some rules here exist because the
gate was broken by an automated test on 2026-09-21; read
[docs/gate-operator.md](docs/gate-operator.md) before touching anything that
can pulse the relay.

## The relay, in one paragraph

The relay is a 2.0 s dry contact on the TOPENS operator's **step-by-step**
input (open / stop / close / stop / open), driving a **two-leaf swing gate**
whose leaves must close in a fixed order so one overlaps the other on the
correct side. Measured on the gate: a pulse while it is fully open starts it
closing; a pulse while it is moving **stops it dead**; the next pulse
**reverses** it. Leaves stopped mid-travel and then re-pulsed lose their
closing order and can cross, after which the gate can neither latch nor open
until someone frees it by hand. That is exactly what happened on 2026-09-21.

## Hard rules for anything that sends `open_gate`

1. **Never pulse a gate that is not provably shut and still.** "Provably" means
   a full-length closing run ended with a latch clang and silence followed, or
   a person has looked at it. A run without a clang is not proof of anything.
2. **Never pulse a gate that is moving, on purpose or by accident.** The only
   exception is a supervised test with a person at the gate.
3. **Never send automatic "recovery" pulses.** With auto-close, the safe path
   to a shut gate is to stop pulsing and listen. If the state is unknown, say
   so and stop; do not try to pulse your way back to a known state.
4. **At most two full cycles per half hour** from any test or diagnostic, and
   never more than one pulse per cycle. A cycle is opening (~20 s), hold
   (16-27 s) and closing (~23 s), latched at about +70 s.
5. **No remote test that includes a mid-travel pulse** unless someone is
   standing at the gate and has agreed to free the leaves by hand.
6. The production cooldowns exist for these reasons: automatic actuations wait
   `GATE_ACTUATION_COOLDOWN_SECONDS` (90 s, longer than a full cycle) after
   any pulse; app commands wait `GATE_COMMAND_COOLDOWN_SECONDS` (20 s). Do not
   lower either for a test.
7. A test harness that sends real pulses must refuse to run without an explicit
   confirmation flag, must log every command it sends, and must be reviewed
   against rules 1-5 by a person before it runs for real. The audio-following
   harness that broke the gate had all three and still broke it, because its
   recovery logic violated rule 3.

## Invariants: read before touching the decision path

[docs/invariants.md](docs/invariants.md) lists what the controller must
always do -- the burst thread never waits on the network, the gate opens on
the Pi's own read with no link to the house, no stale frame opens it, far
frames are not sent to the cloud, a waiting car keeps being read, the lens is
never left changed, the camera clock is UTC, only CI-passed commits deploy,
the suite does not depend on the date -- with the incident behind each and
the tests that guard it. `tests/test_invariants_doc.py` fails if a guard it
names disappears.

- Before changing routing, availability, deadlines, the sweep, uploads, the
  camera or the updater, find the invariants your change touches and run
  their guards. If your change adds a new way for one to fail -- a value that
  can change between two steps, a new thread, a new caller -- add a test that
  drives that case through the real path, and add it to the document.
- Guard behaviour, not code. A test that reads source text and asserts a line
  is present pins one implementation; it stayed green through the 2026-10-05
  fast-lane regression (#183 + #189 undid #165; fixed in #197) while the
  behaviour it stood for was broken. Contract tests on wording and wiring are
  fine as extras; they are never the guard.
- Two PRs that each pass can break an invariant together. Before merging,
  rebase onto current `master` and re-run the full suite; when two open PRs
  touch the same function, merge one, rebase the other, re-test.
- A change to an invariant is a change to `docs/invariants.md` in the same PR.

## Other things that have bitten

- Merging to `master` deploys to the Pi within ~5 minutes of CI passing.
  Merge one PR at a time and test each PR merged with current `master`
  locally first; two green PRs that both restructure the same function can
  produce a `NameError` on the path every frame takes.
- Every fix must be tested through the path production hits (the real
  alarm → sweep → processor → relay chain), not a helper in isolation.
  Helpers tested alone have shipped while nothing called them.
- Never work in the shared checkout; use a `git worktree`.
- Never print `/etc/gate-controller.env` or any value named
  TOKEN/SECRET/PASSWORD/KEY. Camera/NVR passwords are never typed or echoed.
- Departing cars come from behind the camera and show their rear; the relay
  fires for their rear plate too (#171). Look at the frames before asserting
  anything about direction.
