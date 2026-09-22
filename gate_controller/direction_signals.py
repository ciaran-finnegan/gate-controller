"""Which way the vehicle was going, from every signal rather than one.

The estimator this joins fits the slope of ``log(box width)`` against time. On
the camera it was measured against, that separated entering from exiting
completely. On the one fitted now it almost never answers: over the seven days
to 2026-09-21 it resolved 7 of 78 passages and did not say ``exiting`` once. It
needs three boxed frames spanning two seconds, and a passage here is one to
three events.

So width becomes one opinion among several rather than the answer. Each signal
here states a verdict and how much it is worth, and says which it is, so they
can be scored against each other over a season instead of argued about.

What each signal is, and what it is not
---------------------------------------
**The photo.** An arriving car shows the camera its front; a departing car
comes from *behind* the camera and shows its rear. ``direction_vision`` reads
that from one frame. A passage is several frames, and they do not always agree,
so the passage's opinion is the strongest reading on one side less the
strongest on the other.

**The gate's own timing.** The camera cannot see inside the property. A car
leaving therefore cannot be seen until it is through the gate, which means the
gate had to open *before* the camera saw anything -- and if we did not open it,
somebody inside did. A car arriving is the other way round: it is seen first,
at a gate that has been standing still, and the gate moves afterwards.

**Not the relay.** An earlier version of this module read "we fired the relay"
as proof of an arrival, on the grounds that we only fire for a plate read on
the approach. That is false (gate-controller#171): a departing car shows its
*rear* plate to the same camera, it is on the same allow-list, and the relay
fires for it too. Over the week to 2026-09-21 the relay fired in 24 passages
and **five of them were departures**, each confirmed by eye from its photo;
on the four that had audio, the gate had begun opening 26-77 s before the
camera saw anything. The relay is used here only to say which gate movements
were ours, never as a verdict.

**The gate-sound detector over-counts, and hears engines.** It reports about
122 movements a day, and a vehicle's own engine reads as the motor. So a
movement that a clang vouches for is believed and a motor-only one can nudge
but never decide; a run that began 10-22 s before the first frame is ignored
because a real opening and an approaching car both look like that; and a farm
machine, which is audible for most of a minute, silences the gate signal
altogether.

**The passage next door.** An arriving car is seen twice -- waiting at the shut
gate, then half a minute later driving through it, side-on and too close for
the photo to say anything. The second sighting is lent the first's verdict.

**Not sound.** A vehicle is audible before the camera sees it, and the hope was
that an arriving car would ramp where a departing one starts cold. Measured on
2026-09-21 over the eleven by-eye-labelled passages whose audio was still on
the card: the level left its floor 4-20 s before the first frame on six
arrivals and 5-27 s before it on five departures. The ranges overlap almost
entirely -- a departure is preceded by its own gate opening, which is louder
and earlier than any car -- so onset lead is left out rather than given a
weight it has not earned.

Nothing here knows a pixel. The camera's zoom is about to change, and every
constant below is a number of seconds or a weight.
"""
from __future__ import annotations

from dataclasses import dataclass, field

VERDICT_ENTERING = "entering"
VERDICT_EXITING = "exiting"
VERDICT_UNKNOWN = "unknown"
_DECISIVE = (VERDICT_ENTERING, VERDICT_EXITING)

#: The name the combined verdict is recorded and shipped under.
METHOD = "combined-v1"

# --- what each signal is worth ---------------------------------------------
#: No single signal is proof, so none may reach 1.0: a combiner that could be
#: certain from one would have no room left to be corrected by the others.
VISION_CEILING = 0.9
#: The width fit is halved and capped below ``STRONG``: it was fitted on the
#: previous camera's geometry, and must never be able to veto a causal signal.
BOX_WIDTH_CEILING = 0.45
#: A front reading at or above this is "the car was seen coming". See
#: ``from_vision``: what follows it is the same car passing the lens.
FRONT_SEEN_SCORE = 0.5
#: What a rear reading is worth once a front has been seen in the same passage.
PASSING_DISCOUNT = 0.25
#: The gate opened from inside before anything was seen, and the movement was
#: closed by a clang, so it really was the gate.
OPENED_INSIDE_CONFIRMED = 0.6
#: The same shape from a motor-only run. The detector over-counts, and a loud
#: slow vehicle reads as the motor, so this is a lean and not a finding.
OPENED_INSIDE_MOTOR_ONLY = 0.15
#: Seen at a gate that had been still, which then moved -- for our relay or
#: for somebody's fob. Weaker than the exit case because a gate left standing
#: open lets a departing car through with no movement before it either.
OPENED_AFTER_SEEN_CONFIRMED = 0.6
OPENED_AFTER_SEEN_MOTOR_ONLY = 0.15
#: A gate that opened and shut with no vehicle seen at all.
UNSEEN_DEPARTURE_CONFIDENCE = 0.4
#: The most a neighbouring passage can lend. Below ``STRONG`` on purpose: what
#: was inferred from the passage next door must never be able to veto what was
#: seen in this one.
SAME_ARRIVAL_CEILING = 0.45
SAME_ARRIVAL_SHARE = 0.6
#: A rear reading weaker than this, in the second sighting of a car whose first
#: sighting was judged ``entering`` *and had the gate open for it*, is the same
#: flank ``PASSING_DISCOUNT`` covers -- only the front that explains it was in
#: the passage next door rather than this one. At or above it the photo is
#: making a claim of its own and is left alone, so a confident rear still wins.
FLANK_REAR_SCORE = 0.5
#: What such a reading is worth: halved, the same discount and for the same
#: reason as a wheel arch read after a front in a single passage.
FLANK_REAR_DISCOUNT = 0.5

# --- combining ---------------------------------------------------------------
#: At or above this a signal is strong. Two strong signals that disagree are
#: not averaged: the passage is ``unknown`` and flagged as a conflict.
STRONG = 0.5
#: Below this the combined answer is not worth putting in an access log.
MIN_CONFIDENCE = 0.2
COMBINED_CEILING = 0.95

# --- gate timing, all in seconds --------------------------------------------
#: A vehicle is audible before the camera fires and its engine reads as the
#: motor. On relay-opened arrivals, where the true motor start is known to
#: follow the relay, the detector timed the run 1-6 s before the first frame on
#: eight and **19 s** before it on two (2026-09-18 16:56 and 2026-09-19 10:30,
#: both arrivals by their photos). Nothing this close is "already moving".
VEHICLE_NOISE_SECONDS = 10.0
#: ...and a real opening from inside led the first frame by 24-37 s on nine of
#: ten departures, and by 19 s on the tenth. Between the two numbers the timing
#: cannot say which it is looking at, and says nothing.
ALREADY_OPENING_SECONDS = 22.0
#: The gate holds open 15-33 s before closing itself (measured, nine cycles).
#: A movement that *ended* longer ago than this was over before this vehicle
#: could have used it.
HELD_OPEN_SECONDS = 45.0
#: How long after the first frame a movement still counts as opened for it.
OPENED_AFTER_SECONDS = 30.0
#: Past this a movement belongs to a different passage entirely.
SAME_PASSAGE_SECONDS = 120.0
#: An arriving car is seen twice: waiting at the gate, and again 24-32 s later
#: driving through it. Two passages this close are one arrival.
SAME_ARRIVAL_SECONDS = 60.0
#: The relay claims a motor run from this long before it to this long after.
#: The same numbers ``gate_audio_detect`` uses, for the same measured reasons.
COMMAND_BEFORE_SECONDS = 12.0
COMMAND_AFTER_SECONDS = 75.0
#: An opening is confirmed by the clang of the leaves meeting again within this
#: long of the motor stopping.
CYCLE_CLOSES_WITHIN_SECONDS = 150.0


@dataclass(frozen=True)
class Opinion:
    """One signal's answer, and what it is worth."""

    verdict: str
    confidence: float
    method: str
    detail: str = ""

    @property
    def decisive(self) -> bool:
        return self.verdict in _DECISIVE and self.confidence > 0


@dataclass(frozen=True)
class Movement:
    """One motor run from ``gate_movements``, in epoch seconds."""

    started_at: float
    ended_at: float
    clang: bool = False


@dataclass(frozen=True)
class PassageEvidence:
    """Everything known about one passage, from whatever recorded it.

    ``first_seen_at`` is the first *camera* event; a passage that is only a
    remote command has none, and the gate can say nothing about it.
    ``commands_at`` is every relay pulse of ours near the passage, remote
    commands included. ``others_seen`` is the ``(first, last)`` of every other
    vehicle passage nearby: a gate opened for the car in front says nothing
    about the car behind it. ``gate_heard`` is whether the gate-sound scan
    covered this passage at all -- a gate that was not listened to is not a
    gate that stood still.
    """

    first_seen_at: float | None
    last_seen_at: float | None = None
    frames: tuple = ()
    box_width: object | None = None
    movements: tuple = ()
    commands_at: tuple = ()
    others_seen: tuple = ()
    gate_heard: bool = False
    #: The photo showed a tractor or telehandler rather than a car.
    machine: bool = False


@dataclass(frozen=True)
class DirectionVerdict:
    """What every signal together says, with the workings kept."""

    verdict: str = VERDICT_UNKNOWN
    confidence: float = 0.0
    opinions: tuple = field(default_factory=tuple)
    #: True when strong signals disagreed. The verdict is then ``unknown``.
    conflict: bool = False

    @property
    def decisive(self) -> bool:
        return self.verdict in _DECISIVE

    @property
    def contributing(self) -> tuple:
        """The methods that argued for the verdict that was reached."""
        if not self.decisive:
            return ()
        return tuple(o.method for o in self.opinions if o.decisive and o.verdict == self.verdict)

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "confidence": round(self.confidence, 3),
            "conflict": self.conflict,
            "contributing": list(self.contributing),
            "signals": [
                {
                    "method": opinion.method, "verdict": opinion.verdict,
                    "confidence": round(opinion.confidence, 3), "detail": opinion.detail,
                }
                for opinion in self.opinions
            ],
        }


# --------------------------------------------------------------------------
# the photo

def from_vision(frames) -> Opinion:
    """One opinion from every frame of the passage that was read.

    ``frames`` is ``(verdict, score)`` per frame, **in the order they were
    taken**. The strongest reading on one side, less the strongest on the
    other: five frames of a rear and one glimpse of something front-like is
    still a departure.

    The order matters because of where the camera is. An arriving car drives
    *past* it to get through the gate, so its passage reads front, then flank,
    then a wheel arch a foot from the lens -- and the model, rightly for its
    purpose, reads a close flank as "rear". A departing car comes from behind
    the camera and never shows its front at all. So once a confident front has
    been seen, a later rear is the same car going by, and is discounted rather
    than allowed to cancel the front (2026-09-17 11:08: front 0.97, then a
    wheel arch read rear 0.77 -- an Audi driving in).
    """
    best = {VERDICT_ENTERING: 0.0, VERDICT_EXITING: 0.0}
    read = 0
    front_seen = False
    for verdict, score in frames or ():
        if verdict not in best:
            continue
        read += 1
        score = float(score or 0.0)
        if verdict == VERDICT_ENTERING and score >= FRONT_SEEN_SCORE:
            front_seen = True
        elif verdict == VERDICT_EXITING and front_seen:
            score *= PASSING_DISCOUNT
        best[verdict] = max(best[verdict], score)
    if not read:
        return Opinion(VERDICT_UNKNOWN, 0.0, "vision", "no frame showed the front or rear of a vehicle")
    verdict = max(best, key=lambda key: best[key])
    other = VERDICT_EXITING if verdict == VERDICT_ENTERING else VERDICT_ENTERING
    margin = best[verdict] - best[other]
    if margin <= 0:
        return Opinion(VERDICT_UNKNOWN, 0.0, "vision", "the frames contradict each other")
    detail = f"{'front' if verdict == VERDICT_ENTERING else 'rear'} at {best[verdict]:.2f} over {read} frame(s)"
    if best[other] > 0:
        detail += f", against {best[other]:.2f} the other way"
    return Opinion(verdict, min(VISION_CEILING, margin), "vision", detail)


# --------------------------------------------------------------------------
# the slope fit

def from_box_width(estimate) -> Opinion:
    """The existing slope fit, demoted to one vote.

    Kept because on a different aim it worked completely, and it may again once
    the camera is re-aimed -- but it is not trusted to decide alone here, and
    its own score is halved to say so. Its thresholds are deliberately not
    touched: the zoom is about to change, and a number re-fitted today would be
    a fact about a mount that is about to stop existing.

    Accepts a ``DirectionEstimate`` or the telemetry block stored with the
    event, which is the same six keys as a dict.
    """
    if isinstance(estimate, dict):
        verdict, score = estimate.get("verdict", VERDICT_UNKNOWN), estimate.get("score")
    else:
        verdict, score = getattr(estimate, "verdict", VERDICT_UNKNOWN), getattr(estimate, "score", None)
    try:
        score = float(score or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    if verdict not in _DECISIVE:
        return Opinion(VERDICT_UNKNOWN, 0.0, "box_width", f"fit returned {verdict}")
    return Opinion(verdict, min(BOX_WIDTH_CEILING, score * 0.5), "box_width",
                   f"slope fit at {score:.2f}, halved: fitted on the previous camera")


# --------------------------------------------------------------------------
# the gate

def _commanded(movement: Movement, commands_at) -> bool:
    return any(
        -COMMAND_BEFORE_SECONDS <= movement.started_at - moment <= COMMAND_AFTER_SECONDS
        for moment in commands_at or ()
    )


def _opened_for_another(movement: Movement, others_seen) -> bool:
    """Was some other vehicle at the gate when this movement began?

    A departure is first seen up to ``HELD_OPEN_SECONDS`` after its gate began
    to open, and an arrival is seen before; either way the movement is that
    passage's, and the car behind it merely drove through.
    """
    for first, last in others_seen or ():
        if first - HELD_OPEN_SECONDS - COMMAND_BEFORE_SECONDS <= movement.started_at <= last + COMMAND_AFTER_SECONDS:
            return True
    return False


def _confirmed(movement: Movement, movements) -> bool:
    """Did the gate demonstrably move, rather than the detector merely say so?

    A clang is the leaves meeting or reaching an end-stop, and only the gate
    makes it. Either this run carried one, or the cycle it began was closed by
    one shortly afterwards.
    """
    if movement.clang:
        return True
    return any(
        other.clang and 0 <= other.started_at - movement.ended_at <= CYCLE_CLOSES_WITHIN_SECONDS
        for other in movements
    )


def from_gate(first_seen_at, *, movements=(), commands_at=(), others_seen=(),
              gate_heard: bool = True, machine: bool = False) -> Opinion:
    """What the gate's own behaviour says about which way the car was going.

    Two cases, and a deliberate absence:

    * **The gate began to open before anything was seen, and not for us or for
      another vehicle.** Somebody opened it from inside and drove out. This is
      the only way a departing car can reach the camera at all.
    * **The vehicle was seen at a gate that had been still, and the gate moved
      afterwards** -- on our relay, or on somebody's fob. That is an arrival:
      a departure could not have been seen yet.
    * **The relay having fired says nothing.** It fires for a departing car's
      rear plate as readily as for an arriving car's front one (#171).

    And two silences. A run that began 10-22 s before the first frame could be
    either, and is neither. And a farm ``machine`` is audible for most of a
    minute before it is seen and its engine reads as the motor throughout: on
    2026-09-19 at 14:55 a telehandler *arriving* had a "gate movement" 45 s
    before its first frame, clang and all.

    Every timing is relative to the first frame and none is a pixel.
    """
    if first_seen_at is None:
        return Opinion(VERDICT_UNKNOWN, 0.0, "gate", "no camera event to time the gate against")
    if machine:
        return Opinion(VERDICT_UNKNOWN, 0.0, "gate",
                       "a farm machine: its engine reads as the gate motor from a long way out")
    # Only a vehicle *in front* can have had the gate opened for it.
    others_seen = tuple(span for span in others_seen or () if span[0] < first_seen_at)
    near = sorted(
        (m for m in movements or () if abs(m.started_at - first_seen_at) <= SAME_PASSAGE_SECONDS),
        key=lambda m: m.started_at,
    )
    if not near:
        if not gate_heard:
            return Opinion(VERDICT_UNKNOWN, 0.0, "gate", "the gate was not being listened to")
        return Opinion(VERDICT_UNKNOWN, 0.0, "gate", "no movement recorded near this passage")

    before = [
        m for m in near
        if m.started_at <= first_seen_at - ALREADY_OPENING_SECONDS
        and m.ended_at >= first_seen_at - HELD_OPEN_SECONDS
    ]
    ours = [m for m in before if _commanded(m, commands_at)]
    theirs = [m for m in before if _opened_for_another(m, others_seen) and m not in ours]
    inside = [m for m in before if m not in ours and m not in theirs]
    if inside:
        movement = inside[-1]
        lead = first_seen_at - movement.started_at
        if _confirmed(movement, movements):
            return Opinion(
                VERDICT_EXITING, OPENED_INSIDE_CONFIRMED, "gate_opened_from_inside",
                f"the gate began moving {lead:.0f}s before anything was seen, not on our command, "
                "and a clang confirms it moved",
            )
        return Opinion(
            VERDICT_EXITING, OPENED_INSIDE_MOTOR_ONLY, "gate_opened_from_inside",
            f"a motor run began {lead:.0f}s before anything was seen, not on our command; no clang confirms it",
        )
    if before:
        return Opinion(VERDICT_UNKNOWN, 0.0, "gate",
                       "the gate was already open, for us or for the vehicle in front")
    if not gate_heard:
        return Opinion(VERDICT_UNKNOWN, 0.0, "gate", "the gate was not listened to before this passage")
    if any(-ALREADY_OPENING_SECONDS < m.started_at - first_seen_at <= -VEHICLE_NOISE_SECONDS for m in near):
        return Opinion(VERDICT_UNKNOWN, 0.0, "gate",
                       "a motor run began 10-22s before anything was seen: an opening from inside "
                       "and the car's own engine both look like that")

    after = [
        m for m in near
        if -VEHICLE_NOISE_SECONDS < m.started_at - first_seen_at <= OPENED_AFTER_SECONDS
        and not _opened_for_another(m, others_seen)
    ]
    if after:
        movement = after[0]
        by = "our relay" if _commanded(movement, commands_at) else "somebody's fob or keypad"
        if _confirmed(movement, movements):
            return Opinion(
                VERDICT_ENTERING, OPENED_AFTER_SEEN_CONFIRMED, "gate_opened_after_seen",
                f"seen at a gate that had been still, which then opened on {by}; a clang confirms it moved",
            )
        return Opinion(
            VERDICT_ENTERING, OPENED_AFTER_SEEN_MOTOR_ONLY, "gate_opened_after_seen",
            f"seen at a gate that had been still; a motor run followed on {by}, with no clang to confirm it",
        )
    return Opinion(VERDICT_UNKNOWN, 0.0, "gate", "the timing does not separate the two")


def from_same_arrival(neighbour, gap_seconds) -> Opinion:
    """What the passage next door says, when it is the same car.

    An arriving car is seen twice: pulled up at the shut gate, and again half a
    minute later driving through it, side-on and too close for the photo to say
    anything. Ten such pairs were looked at by eye over the week to 2026-09-21
    and every one was a single arrival. So a passage within
    ``SAME_ARRIVAL_SECONDS`` of one judged ``entering`` *on its own evidence*
    is lent that verdict, at a discount and never strongly.

    Arrivals only. A departing car does not stop in view, so two passages close
    together of which one is leaving are two vehicles, and nothing follows.
    """
    if neighbour is None or gap_seconds is None or gap_seconds > SAME_ARRIVAL_SECONDS:
        return Opinion(VERDICT_UNKNOWN, 0.0, "same_arrival", "no arrival close enough to be the same car")
    if neighbour.verdict != VERDICT_ENTERING:
        return Opinion(VERDICT_UNKNOWN, 0.0, "same_arrival", "the passage beside this one was not an arrival")
    return Opinion(
        VERDICT_ENTERING, min(SAME_ARRIVAL_CEILING, neighbour.confidence * SAME_ARRIVAL_SHARE),
        "same_arrival", f"{gap_seconds:.0f}s from a passage judged entering at {neighbour.confidence:.2f}",
    )


def passing_the_lens_again(vision: Opinion) -> Opinion:
    """Discount a weak rear reading that is the same car's flank going by.

    ``from_vision`` already does this *within* one passage: once a confident
    front has been seen, a later rear is a wheel arch a foot from the lens and
    is halved rather than allowed to cancel the front. An arriving car is
    often seen as two passages, though -- waiting at the shut gate, then
    driving through it -- and then the front is in the passage next door and
    this one holds nothing but flank.

    2026-09-22 10:00, an Audi driving in: the first passage read front 0.93,
    the gate had been still and opened on our relay, and it was judged
    ``entering`` at 0.95. Twenty-one seconds later the same car, side-on and
    filling the frame, read rear 0.42 -- and 0.36 of vision against 0.45 lent
    by the neighbour left 0.09, under the bar, so a passage nothing was wrong
    with came out ``unknown``.

    Only applied where the neighbour was judged ``entering`` on its own
    evidence *and the gate actually opened for it*, and only below
    ``FLANK_REAR_SCORE``. A rear the model is sure of is a departure through a
    gate that opened for the car in front, which is a real thing that happens,
    and it is left to win.
    """
    if vision.verdict != VERDICT_EXITING or vision.confidence >= FLANK_REAR_SCORE:
        return vision
    return Opinion(
        VERDICT_EXITING, vision.confidence * FLANK_REAR_DISCOUNT, vision.method,
        f"{vision.detail}; halved: the car that arrived next door is passing the lens",
    )


def unseen_departures(movements, commands_at, seen) -> list:
    """Gate cycles that no vehicle passage accounts for.

    The gate opened, not on our command, a clang confirms it shut again, and
    the camera reported nothing anywhere near it. The car was never in view
    because it came from behind the camera -- a departure that leaves no
    passage, and the reason the count of passages has always been short.

    Only clang-confirmed cycles: the detector reports about 122 movements a day
    and a motor-only run with nobody in frame is more likely the wind than a
    car. Returns ``(movement, Opinion)`` pairs.
    """
    movements = sorted(movements or (), key=lambda m: m.started_at)
    found = []
    claimed_until = None
    for movement in movements:
        if claimed_until is not None and movement.started_at <= claimed_until:
            continue
        if movement.clang or _commanded(movement, commands_at):
            continue
        if not _confirmed(movement, movements):
            continue
        if any(
            first - SAME_PASSAGE_SECONDS <= movement.started_at <= last + SAME_PASSAGE_SECONDS
            for first, last in seen or ()
        ):
            continue
        claimed_until = movement.ended_at + CYCLE_CLOSES_WITHIN_SECONDS
        found.append((movement, Opinion(
            VERDICT_EXITING, UNSEEN_DEPARTURE_CONFIDENCE, "gate_moved_unseen",
            "the gate opened and shut, not on our command, and the camera saw nothing",
        )))
    return found


# --------------------------------------------------------------------------
# together

def combine(*opinions: Opinion) -> DirectionVerdict:
    """One verdict from many, with every signal's own answer kept beside it.

    Signals that agree reinforce each other: each side's support is
    ``1 - prod(1 - confidence)``, so two independent 0.6s are worth 0.84 and
    never 1. The answer's confidence is its side's support less the other
    side's.

    Two things make it ``unknown`` rather than a coin toss. If both sides hold a
    **strong** signal, the passage is a conflict and is recorded as one -- a
    photo of a rear and a gate that says arrival is a fact worth counting, not
    a 0.15 lean. And if what is left after the subtraction is below
    ``MIN_CONFIDENCE``, it is not an answer.
    """
    kept = tuple(opinion for opinion in opinions if opinion is not None)
    decisive = [opinion for opinion in kept if opinion.decisive]
    if not decisive:
        return DirectionVerdict(VERDICT_UNKNOWN, 0.0, kept)

    doubt = {VERDICT_ENTERING: 1.0, VERDICT_EXITING: 1.0}
    strongest = {VERDICT_ENTERING: 0.0, VERDICT_EXITING: 0.0}
    for opinion in decisive:
        confidence = min(1.0, max(0.0, opinion.confidence))
        doubt[opinion.verdict] *= 1.0 - confidence
        strongest[opinion.verdict] = max(strongest[opinion.verdict], confidence)
    support = {verdict: 1.0 - value for verdict, value in doubt.items()}

    if min(strongest.values()) >= STRONG:
        return DirectionVerdict(VERDICT_UNKNOWN, 0.0, kept, conflict=True)
    verdict = max(support, key=lambda key: support[key])
    other = VERDICT_EXITING if verdict == VERDICT_ENTERING else VERDICT_ENTERING
    confidence = min(COMBINED_CEILING, support[verdict] - support[other])
    if confidence < MIN_CONFIDENCE:
        return DirectionVerdict(VERDICT_UNKNOWN, 0.0, kept)
    return DirectionVerdict(verdict, confidence, kept)


def judge(evidence: PassageEvidence, *, neighbour=None, neighbour_gap=None,
          neighbour_opened: bool = False) -> DirectionVerdict:
    """The one place a passage's evidence becomes a verdict.

    Production calls this and nothing else; the opinion functions above are its
    parts, not alternatives to it. ``neighbour`` is the verdict of the nearest
    other passage *judged without a neighbour of its own*, so that a verdict
    can be lent once and never passed down a line of cars.
    ``neighbour_opened`` says the gate really did open for that passage -- our
    relay fired inside it -- which is what makes a weak rear here the same
    car's flank rather than a vehicle of its own. See ``passing_the_lens_again``.
    """
    vision = from_vision(evidence.frames)
    same_arrival = from_same_arrival(neighbour, neighbour_gap) if neighbour is not None else None
    if same_arrival is not None and same_arrival.decisive and neighbour_opened:
        vision = passing_the_lens_again(vision)
    opinions = [
        vision,
        from_gate(
            evidence.first_seen_at, movements=evidence.movements,
            commands_at=evidence.commands_at, others_seen=evidence.others_seen,
            gate_heard=evidence.gate_heard, machine=evidence.machine,
        ),
        from_box_width(evidence.box_width),
    ]
    if same_arrival is not None:
        opinions.append(same_arrival)
    return combine(*opinions)
