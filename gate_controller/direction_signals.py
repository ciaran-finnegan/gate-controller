"""Which way the vehicle was going, from every signal rather than one.

The estimator this joins fits the slope of ``log(box width)`` against time. On
the camera it was measured against, that separated entering from exiting
completely. On the one fitted now it does neither reliably nor often:

* **It almost never answers.** Over 991 passages with telemetry it produced a
  verdict on **18** -- nine entering, eight exiting, one stationary. 302 came
  back ``unknown`` and 671 carried no estimate at all. It needs three boxed
  frames spanning two seconds, and a passage here is one to three events.
* **When it does answer it can be wrong.** On 2026-09-17 at 09:02 a departing
  Audi read ``entering`` at 0.26 on a slope of +0.135 -- above the top of the
  +0.010..+0.098 band the rule was fitted on.
* **Width cannot separate them at this mount.** The camera is a metre from the
  gate facing the approach. A car arriving drives toward it from the road; a
  car leaving drives toward it from inside the property and past it. *Both*
  grow. On the old camera, aimed further out, a departing car was already
  receding and shrank -- which is why the rule worked there and not here.

So width becomes one opinion among several rather than the answer. Each signal
here states a verdict and how much it is worth, and says which it is, so they
can be scored against each other over a season instead of argued about.

The strongest one costs nothing and needs no model: **the gate itself**. If the
gate was already moving before the camera saw anything, and nobody commanded
it, somebody opened it from inside and is driving out. If we fired the relay
for a plate we read on the approach, the car is coming in. That is causal
rather than empirical, it does not care what the lens sees, and it is the
signal that can label the others.
"""
from __future__ import annotations

from dataclasses import dataclass, field

VERDICT_ENTERING = "entering"
VERDICT_EXITING = "exiting"
VERDICT_UNKNOWN = "unknown"

#: What a signal is worth when it is as sure as it gets. None is 1.0: no single
#: signal here is proof, and a combiner that could reach certainty from one
#: would have no room left to be corrected by the others.
COMMANDED_CONFIDENCE = 0.9
GATE_ALREADY_MOVING_CONFIDENCE = 0.85
GATE_FOLLOWED_CONFIDENCE = 0.6

#: A gate that started moving more than this before the first frame was not
#: opened for the car in the frame. Measured: the relay-to-motor gap is one to
#: two seconds, and the detector's own onset error reached 3.2 s at 11:08.
ALREADY_MOVING_SECONDS = 5.0
#: Past this the movement belongs to a different passage entirely.
SAME_PASSAGE_SECONDS = 120.0


@dataclass(frozen=True)
class Opinion:
    """One signal's answer, and what it is worth."""

    verdict: str
    confidence: float
    method: str
    detail: str = ""

    @property
    def decisive(self) -> bool:
        return self.verdict in (VERDICT_ENTERING, VERDICT_EXITING) and self.confidence > 0


@dataclass(frozen=True)
class DirectionVerdict:
    """What every signal together says, with the workings kept."""

    verdict: str = VERDICT_UNKNOWN
    confidence: float = 0.0
    opinions: tuple[Opinion, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "confidence": round(self.confidence, 3),
            "signals": [
                {
                    "method": opinion.method, "verdict": opinion.verdict,
                    "confidence": round(opinion.confidence, 3), "detail": opinion.detail,
                }
                for opinion in self.opinions
            ],
        }


def from_gate(first_seen_at: float, *, relay_at=None, movement_started_at=None,
              movement_uncommanded: bool | None = None) -> Opinion:
    """What the gate's own behaviour says about which way the car was going.

    ``first_seen_at`` is when the camera first reported this passage; the other
    three are seconds on the same clock, or None where unknown.

    Three cases, in descending order of how sure they are:

    * **We fired the relay for it.** We only do that for a plate read on the
      approach, so the car is coming in.
    * **The gate was already moving before the camera saw anything, and nobody
      commanded it.** Somebody opened it from inside and drove out: the camera
      only picks them up once they are at the gate, which is why a departing
      car is seen late and from behind.
    * **The gate moved after the camera saw the car.** Weaker -- it is the
      normal shape of an arrival, but an exit that happened to be seen early
      looks the same, so it is worth much less.
    """
    if relay_at is not None and abs(relay_at - first_seen_at) <= SAME_PASSAGE_SECONDS:
        return Opinion(
            VERDICT_ENTERING, COMMANDED_CONFIDENCE, "gate_commanded",
            "the relay fired for a plate read on the approach",
        )
    if movement_started_at is None:
        return Opinion(VERDICT_UNKNOWN, 0.0, "gate", "no movement recorded near this passage")
    gap = first_seen_at - movement_started_at
    if gap > SAME_PASSAGE_SECONDS:
        return Opinion(VERDICT_UNKNOWN, 0.0, "gate", "the nearest movement is too far away")
    if gap >= ALREADY_MOVING_SECONDS and movement_uncommanded:
        return Opinion(
            VERDICT_EXITING, GATE_ALREADY_MOVING_CONFIDENCE, "gate_already_moving",
            f"the gate had been moving {gap:.0f}s before the camera saw anything, uncommanded",
        )
    if gap < 0:
        return Opinion(
            VERDICT_ENTERING, GATE_FOLLOWED_CONFIDENCE, "gate_followed",
            "the gate moved after the car was seen",
        )
    return Opinion(VERDICT_UNKNOWN, 0.0, "gate", "the timing does not separate the two")


def from_box_width(estimate) -> Opinion:
    """The existing slope fit, demoted to one vote.

    Kept because on a different aim it worked completely, and it may again once
    the camera is re-aimed -- but it is not trusted to decide alone here, and
    its own score is halved to say so. A signal that was fitted on geometry
    that no longer exists should not outvote one that is causal.
    """
    verdict = getattr(estimate, "verdict", VERDICT_UNKNOWN)
    score = getattr(estimate, "score", None) or 0.0
    if verdict not in (VERDICT_ENTERING, VERDICT_EXITING):
        return Opinion(VERDICT_UNKNOWN, 0.0, "box_width", f"fit returned {verdict}")
    return Opinion(verdict, min(0.45, float(score) * 0.5), "box_width",
                   f"slope fit at {score:.2f}, halved: fitted on the previous camera")


def combine(*opinions: Opinion) -> DirectionVerdict:
    """One verdict from many, with every signal's own answer kept beside it.

    Confidence is the strongest supporting signal reduced by whatever the
    strongest opposing one is worth -- so two signals that disagree leave a
    weak answer rather than a loud one, and the disagreement stays visible in
    ``signals`` where it can be counted later.
    """
    kept = tuple(opinion for opinion in opinions if opinion is not None)
    decisive = [opinion for opinion in kept if opinion.decisive]
    if not decisive:
        return DirectionVerdict(VERDICT_UNKNOWN, 0.0, kept)

    best: dict[str, float] = {}
    for opinion in decisive:
        best[opinion.verdict] = max(best.get(opinion.verdict, 0.0), opinion.confidence)
    verdict = max(best, key=lambda key: best[key])
    against = max((value for key, value in best.items() if key != verdict), default=0.0)
    return DirectionVerdict(verdict, max(0.0, best[verdict] - against), kept)
