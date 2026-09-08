"""Which way the vehicle was going, from the boxes the frames already carry.

Shadow only. Nothing here reaches ``decide_access``, the relay, the presence
session or the cloud lookup budget: the estimate is journalled and shipped as
one bounded telemetry block so a week of real passages can be reviewed before
gate-controller#95 is allowed to act on it.

The signal, and why it is this one
----------------------------------
Measured on 42 hand-labelled passages
(``gate-controller-data/analysis/vehicle-direction-2026-09-08.md``): the
least-squares slope of ``log(box width)`` against time separates entering from
exiting **completely** once the fit is gated at a span of at least 2 s and at
least 3 boxed frames -- entering ran +0.010..+0.098 (n=15), exiting
-0.663..-0.093 (n=7), with nothing in between. Ungated, the very same rule
called one entering car an exit: a two-frame burst 0.8 s apart whose
timestamps are filesystem-ingress times rather than capture times. The gate is
therefore a safety property, not a tuning detail, and this module refuses to
fit a series that does not meet it rather than fitting it and hedging.

Plate-box geometry did not separate the two classes at all, and the cheap
tail-light proxy is confounded by this site's red tractor and red courier van,
so neither is a vote here. Night is unmeasured -- exactly one of the 42
labelled passages is genuinely dark -- so a dark event is ``unknown``.

Cost
----
No new model and no extra decode. The samples are boxes the pipeline already
produced: the on-device detector's plate box (``LocalRecognition.box``) and
the cloud read's vehicle and plate boxes, each already mapped to fractions of
the whole frame. A sample is a two-float append.

What a "passage" is here
-----------------------
One processing trace is one *burst*, and a burst is usually a single frame:
the fit needs the frames of the whole camera alarm. The samples are therefore
keyed by the camera event -- ``(source, event_type, rule_id, event_at)``, the
same identity ``worker.BurstIdentity.camera_event`` already uses to decide
which queued frame supersedes which, and the same one every frame of a
presence session carries -- and each processing trace is *bound* to that key
by the processor. A burst with no correlated alarm (a bare FTP burst) keys on
its own trace id and can therefore only ever fit its own frames. Two alarms
have different ``event_at`` values, so one passage's boxes can never be
fitted into another's.

Bounds
------
At most ``MAX_TRACKED_EVENTS`` passages and the same number of trace
bindings; at most ``MAX_SAMPLES`` samples per source per passage; a passage
untouched for ``PASSAGE_TTL_SECONDS`` is dropped, and past the cap the least
recently touched goes first. The processor drops a trace's binding as soon as
its event finishes.
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from dataclasses import dataclass
from math import isfinite, log
from threading import Lock
from time import monotonic

LOGGER = logging.getLogger(__name__)

VERDICT_ENTERING = "entering"
VERDICT_EXITING = "exiting"
VERDICT_STATIONARY = "stationary"
VERDICT_UNKNOWN = "unknown"
VERDICTS = frozenset({
    VERDICT_ENTERING, VERDICT_EXITING, VERDICT_STATIONARY, VERDICT_UNKNOWN,
})

#: The only method this release implements. The wire vocabulary is fixed by
#: the app's ingest contract and ``none`` may only ever carry ``unknown``.
METHOD_BOX_WIDTH = "box_width"
METHOD_NONE = "none"
METHODS = frozenset({METHOD_BOX_WIDTH, METHOD_NONE})

#: Where a width came from. Kept out of the wire block on purpose -- the
#: contract has no key for it -- but journalled, because widths from two
#: different detectors are on two different scales and must never be fitted
#: as one series.
SOURCE_VEHICLE_BOX = "vehicle_box"
SOURCE_LOCAL_PLATE_BOX = "local_plate_box"
SOURCE_PLATE_BOX = "plate_box"
#: Preference order at estimate time: the vehicle box is the one the analysis
#: measured; the on-device plate box is the densest, because that detector
#: runs on every frame; the cloud plate box only exists where a lookup came
#: back with a result, which is the class of frame direction is least needed
#: for. Never merged: three detectors, three scales.
SOURCES = (SOURCE_VEHICLE_BOX, SOURCE_LOCAL_PLATE_BOX, SOURCE_PLATE_BOX)

#: The app's contract caps ``frames`` at 64 and ``span_ms`` at 600 000, and
#: rejects the whole event for anything outside them. 32 samples is twice the
#: most boxed frames any labelled passage produced (15).
MAX_SAMPLES = 32
MAX_SPAN_MS = 600_000
MAX_SLOPE = 10.0
#: Passages tracked at once. The same order as the local recogniser's own
#: per-event summary cap, and far above what one gate can have in flight.
MAX_TRACKED_EVENTS = 64
#: A passage untouched for this long is over. Well past the longest presence
#: window the capture config allows (``MAX_PRESENCE_WINDOW_SECONDS``, 120 s).
PASSAGE_TTL_SECONDS = 300.0

#: d(log width)/dt at or below which the vehicle is receding. The analysis's
#: gated exits ran to -0.093 at their weakest and its gated entering cars to
#: +0.010 at their weakest, so the whole empty band is -0.093..+0.010; -0.06
#: sits inside it and caught 7/7 exits with 0/15 false exits on entering cars.
DEFAULT_EXIT_SLOPE = -0.06
#: ...and the other side of the same band.
DEFAULT_ENTER_SLOPE = 0.01
#: The gate. Both may be tightened by configuration and neither may be
#: loosened: below these the measured false-exit rate is 1/22 rather than 0/15.
DEFAULT_MIN_FRAMES = 3
MIN_MIN_FRAMES = 3
DEFAULT_MIN_SPAN_SECONDS = 2.0
MIN_MIN_SPAN_SECONDS = 2.0
#: Frame brightness below which the event is night. 22 of 210 passages with
#: brightness telemetry sat under 0.20 and none of them produced a plate read;
#: exactly one labelled passage is dark, so night direction is unmeasured and
#: ships `unknown`/`none` rather than a guess.
DEFAULT_MIN_BRIGHTNESS = 0.20

#: A verdict of `stationary` is a claim that the vehicle stayed put, so it
#: needs a longer look than the direction gate and a width that really did not
#: move: the total log-width change over the fit must stay inside this, i.e.
#: the box ended between 0.61x and 1.65x the width it started at.
STATIONARY_MIN_SPAN_SECONDS = 3.0
STATIONARY_MAX_LOG_RANGE = 0.5

#: Score shape. ``margin`` is how far past its threshold the slope sits, in
#: slope units; this is the margin at which the slope term saturates. 0.30 is
#: the analysis's own confidence scale (`min(1, |slope| / 0.30)`), measured
#: here from the threshold rather than from zero so a verdict that only just
#: clears the gate scores near zero.
SCORE_SLOPE_SCALE = 0.30
#: Frames beyond the minimum at which frame support saturates: 3 frames is
#: the least the gate allows and 6 is the most any labelled exit produced.
SCORE_FRAME_SCALE = 4.0
#: Frame support never scales the score below half: the gate has already
#: refused everything under three frames, so a three-frame fit is weak
#: evidence, not no evidence.
SCORE_FRAME_FLOOR = 0.5

#: How many estimates between rollup counter lines. The app's heartbeat has
#: no allowlisted slot for direction counters (`PI_STATUS_CAPABILITY_KEYS` in
#: access-gate-ui `worker/routes/controller.ts`), so the journal is where the
#: shadow run is counted.
COUNTER_LOG_INTERVAL = 25


class DirectionConfigError(ValueError):
    """A ``GATE_DIRECTION_*`` value the estimator refuses to run under."""


@dataclass(frozen=True)
class DirectionConfig:
    """The operating point, all of it env-tunable and all of it validated."""

    enabled: bool = True
    exit_slope: float = DEFAULT_EXIT_SLOPE
    enter_slope: float = DEFAULT_ENTER_SLOPE
    min_frames: int = DEFAULT_MIN_FRAMES
    min_span_seconds: float = DEFAULT_MIN_SPAN_SECONDS
    min_brightness: float = DEFAULT_MIN_BRIGHTNESS


def load_direction_config(environment=None) -> DirectionConfig:
    """Read ``GATE_DIRECTION_*``; an unset environment means the defaults.

    The gate (``MIN_FRAMES``, ``MIN_SPAN_SECONDS``) may only be tightened.
    Loosening it is what produced the single measured false exit, so it is
    refused here rather than left to a deployment note.
    """
    environment = os.environ if environment is None else environment
    enabled = _boolean(environment.get("GATE_DIRECTION_ENABLED"), True)
    exit_slope = _number(
        environment, "GATE_DIRECTION_EXIT_SLOPE", DEFAULT_EXIT_SLOPE,
    )
    enter_slope = _number(
        environment, "GATE_DIRECTION_ENTER_SLOPE", DEFAULT_ENTER_SLOPE,
    )
    if not -MAX_SLOPE <= exit_slope < 0:
        raise DirectionConfigError(
            f"GATE_DIRECTION_EXIT_SLOPE must be negative and at least {-MAX_SLOPE}"
        )
    if not 0 < enter_slope <= MAX_SLOPE:
        raise DirectionConfigError(
            f"GATE_DIRECTION_ENTER_SLOPE must be positive and at most {MAX_SLOPE}"
        )
    if exit_slope >= enter_slope:
        raise DirectionConfigError(
            "GATE_DIRECTION_EXIT_SLOPE must sit below GATE_DIRECTION_ENTER_SLOPE"
        )
    min_frames = _integer(
        environment, "GATE_DIRECTION_MIN_FRAMES", DEFAULT_MIN_FRAMES,
    )
    if not MIN_MIN_FRAMES <= min_frames <= MAX_SAMPLES:
        raise DirectionConfigError(
            "GATE_DIRECTION_MIN_FRAMES must be between "
            f"{MIN_MIN_FRAMES} and {MAX_SAMPLES}"
        )
    min_span = _number(
        environment, "GATE_DIRECTION_MIN_SPAN_SECONDS", DEFAULT_MIN_SPAN_SECONDS,
    )
    if not MIN_MIN_SPAN_SECONDS <= min_span <= MAX_SPAN_MS / 1000:
        raise DirectionConfigError(
            "GATE_DIRECTION_MIN_SPAN_SECONDS must be between "
            f"{MIN_MIN_SPAN_SECONDS} and {MAX_SPAN_MS // 1000}"
        )
    min_brightness = _number(
        environment, "GATE_DIRECTION_MIN_BRIGHTNESS", DEFAULT_MIN_BRIGHTNESS,
    )
    if not 0 <= min_brightness <= 1:
        raise DirectionConfigError(
            "GATE_DIRECTION_MIN_BRIGHTNESS must be between 0 and 1"
        )
    return DirectionConfig(
        enabled=enabled,
        exit_slope=exit_slope,
        enter_slope=enter_slope,
        min_frames=min_frames,
        min_span_seconds=min_span,
        min_brightness=min_brightness,
    )


@dataclass(frozen=True)
class DirectionEstimate:
    """One event's answer, shaped for the app's ``telemetry.direction`` block.

    ``method`` says whether an estimator ran at all. ``none`` means it did
    not -- the feature is off, the event carried no box, or the scene was too
    dark to measure -- and the contract allows it to carry no verdict but
    ``unknown``. ``box_width`` with ``verdict='unknown'`` is the other honest
    answer: boxes were seen, and the gate refused to fit them.
    """

    verdict: str = VERDICT_UNKNOWN
    method: str = METHOD_NONE
    score: float | None = None
    slope: float | None = None
    frames: int = 0
    span_ms: int = 0
    #: Which box series the fit used. Journalled, never sent: the contract
    #: has no key for it and an unknown key rejects the whole event.
    source: str | None = None

    def to_wire(self) -> dict[str, object]:
        """The six contract keys, each narrowed to what ingest accepts.

        Transcribed from ``validateDirection`` in access-gate-ui
        ``worker/contracts/gate-event-ingest/contract.ts``: an out-of-range
        value or an unknown key is a 400 the outbox retries forever, so every
        field is clamped here rather than trusted.
        """
        method = self.method if self.method in METHODS else METHOD_NONE
        verdict = self.verdict if self.verdict in VERDICTS else VERDICT_UNKNOWN
        # The Worker enforces this pairing and rejects the whole event when it
        # does not hold. A verdict is a claim about a measurement, so a block
        # that says nothing was measured may only say `unknown`.
        if method == METHOD_NONE:
            verdict = VERDICT_UNKNOWN
        return {
            "verdict": verdict,
            "method": method,
            "score": _optional_ratio(self.score),
            "slope": _optional_slope(self.slope),
            "frames": _bounded_int(self.frames, 0, MAX_SAMPLES),
            "span_ms": _bounded_int(self.span_ms, 0, MAX_SPAN_MS),
        }

    def journal(self) -> str:
        """The ``gate_direction`` fields, in the order the review reads them."""
        wire = self.to_wire()
        return (
            "verdict=%s method=%s slope=%s frames=%d span_ms=%d score=%s source=%s"
            % (
                wire["verdict"], wire["method"],
                "-" if wire["slope"] is None else f"{wire['slope']:.4f}",
                wire["frames"], wire["span_ms"],
                "-" if wire["score"] is None else f"{wire['score']:.3f}",
                self.source or "-",
            )
        )


#: What every event ships when nothing was measured.
UNMEASURED = DirectionEstimate()


def estimate_direction(samples, config: DirectionConfig | None = None,
                       *, source: str | None = None) -> DirectionEstimate:
    """Fit ``log(width)`` against time over one event's samples of one source.

    ``samples`` is an iterable of ``(seconds, width)`` where ``width`` is a
    fraction of the frame. Widths from two different detectors are on two
    different scales, so they must be fitted separately; the caller picks
    which series to pass.

    Returns a ``box_width``/``unknown`` estimate -- never an exception -- when
    the gate is not met. The gate is deliberately the first thing that
    happens: a short span is discarded, not fitted.
    """
    config = config or DirectionConfig()
    points = _clean(samples)
    frames = len(points)
    if frames == 0:
        return UNMEASURED
    span_seconds = points[-1][0] - points[0][0]
    span_ms = _bounded_int(round(span_seconds * 1000), 0, MAX_SPAN_MS)
    partial = DirectionEstimate(
        verdict=VERDICT_UNKNOWN, method=METHOD_BOX_WIDTH,
        frames=_bounded_int(frames, 0, MAX_SAMPLES), span_ms=span_ms,
        source=source,
    )
    if frames < config.min_frames or span_seconds < config.min_span_seconds:
        return partial
    slope = _slope(points)
    if slope is None:
        return partial
    verdict = _verdict(slope, span_seconds, config)
    if verdict == VERDICT_UNKNOWN:
        return DirectionEstimate(
            verdict=VERDICT_UNKNOWN, method=METHOD_BOX_WIDTH, slope=slope,
            frames=partial.frames, span_ms=span_ms, source=source,
        )
    return DirectionEstimate(
        verdict=verdict,
        method=METHOD_BOX_WIDTH,
        score=_score(verdict, slope, frames, config),
        slope=slope,
        frames=partial.frames,
        span_ms=span_ms,
        source=source,
    )


def _verdict(slope: float, span_seconds: float, config: DirectionConfig) -> str:
    if slope <= config.exit_slope:
        return VERDICT_EXITING
    if slope >= config.enter_slope:
        return VERDICT_ENTERING
    # Between the two thresholds. That is only a claim of stationarity when
    # the look was long enough for a slow vehicle to have moved, and the
    # width really did not move over it.
    if (span_seconds >= STATIONARY_MIN_SPAN_SECONDS
            and abs(slope) * span_seconds <= STATIONARY_MAX_LOG_RANGE):
        return VERDICT_STATIONARY
    return VERDICT_UNKNOWN


def _score(verdict: str, slope: float, frames: int, config: DirectionConfig) -> float:
    """Confidence, 0..1: how far past the boundary, and on how many frames.

    ``score = min(1, margin / 0.30) * (0.5 + 0.5 * min(1, (frames - min_frames + 1) / 4))``

    ``margin`` is the distance in slope units from the boundary the verdict
    crossed -- ``exit_slope - slope`` for an exit, ``slope - enter_slope`` for
    an entry, and the distance to the nearer of the two for ``stationary``.
    Both terms are non-decreasing, so the score is monotone in the strength of
    the slope and in the number of frames behind it, and a verdict sitting
    exactly on its threshold scores 0 rather than borrowing confidence from
    having been measured at all.
    """
    if verdict == VERDICT_EXITING:
        margin = config.exit_slope - slope
    elif verdict == VERDICT_ENTERING:
        margin = slope - config.enter_slope
    else:
        margin = min(slope - config.exit_slope, config.enter_slope - slope)
    strength = min(1.0, max(0.0, margin) / SCORE_SLOPE_SCALE)
    support = min(1.0, max(0.0, frames - config.min_frames + 1) / SCORE_FRAME_SCALE)
    return round(strength * (SCORE_FRAME_FLOOR + (1 - SCORE_FRAME_FLOOR) * support), 3)


def _clean(samples) -> list[tuple[float, float]]:
    """Finite, positive-width samples in time order, bounded to the wire cap."""
    points: list[tuple[float, float]] = []
    for sample in samples or ():
        try:
            at, width = sample
            at = float(at)
            width = float(width)
        except (TypeError, ValueError):
            continue
        if not isfinite(at) or not isfinite(width) or width <= 0:
            continue
        points.append((at, width))
    points.sort(key=lambda point: point[0])
    return points[-MAX_SAMPLES:]


def _slope(points) -> float | None:
    """Least-squares d(log width)/dt, or None when time never moved."""
    try:
        times = [point[0] for point in points]
        widths = [log(point[1]) for point in points]
    except (TypeError, ValueError):
        return None
    count = len(times)
    mean_time = sum(times) / count
    mean_width = sum(widths) / count
    denominator = sum((value - mean_time) ** 2 for value in times)
    if denominator <= 0:
        return None
    numerator = sum(
        (time - mean_time) * (width - mean_width)
        for time, width in zip(times, widths)
    )
    slope = numerator / denominator
    if not isfinite(slope):
        return None
    return max(-MAX_SLOPE, min(MAX_SLOPE, slope))


def passage_key(trigger) -> str | None:
    """Which camera alarm this frame belongs to, or None when unknown.

    The same rule as ``worker.BurstIdentity.camera_event``, and deliberately
    a copy of it rather than a shared import: this module must not depend on
    the worker, and the per-frame ``delta_ms`` must not take part -- every
    frame of one presence session differs in it and shares everything else.
    Only a correlated webhook trigger identifies an alarm; an FTP burst with
    no correlation is not treated as sharing one with anybody.
    """
    if trigger is None or getattr(trigger, "correlation", None) != "matched":
        return None
    event_at = getattr(trigger, "event_at", None)
    rule_id = getattr(trigger, "rule_id", None)
    if event_at is None and rule_id is None:
        return None
    return "|".join(
        "-" if part is None else str(part)
        for part in (
            getattr(trigger, "source", None),
            getattr(trigger, "event_type", None),
            rule_id,
            event_at,
        )
    )


class _PassageDirection:
    """One passage's samples, one list per box source, plus how dark it was."""

    __slots__ = ("series", "brightest", "touched_at")

    def __init__(self, touched_at: float) -> None:
        self.series: dict[str, list[tuple[float, float]]] = {}
        self.brightest: float | None = None
        self.touched_at = touched_at

    def add(self, source: str, at: float, width: float) -> None:
        samples = self.series.setdefault(source, [])
        samples.append((at, width))
        if len(samples) > MAX_SAMPLES:
            del samples[0]

    def note_brightness(self, brightness: float) -> None:
        if self.brightest is None or brightness > self.brightest:
            self.brightest = brightness


class DirectionTracker:
    """Per-passage box series and the verdict drawn from them.

    Thread-safe: frames of one passage are read on the burst worker and on
    the OCR worker. Never raises into the pipeline -- every public method is
    best-effort, the way ``_BestEffortTrace`` is -- because a shadow signal
    is not worth an event.
    """

    def __init__(self, config: DirectionConfig | None = None, *,
                 clock=monotonic) -> None:
        self._config = config or DirectionConfig()
        self._clock = clock
        self._lock = Lock()
        self._passages: "OrderedDict[str, _PassageDirection]" = OrderedDict()
        self._bindings: "OrderedDict[str, str]" = OrderedDict()
        self._counts: dict[str, int] = {}
        self._since_rollup = 0

    @property
    def config(self) -> DirectionConfig:
        return self._config

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def bind(self, trace_id, passage) -> None:
        """Say which camera alarm this processing trace belongs to.

        Unbound traces key on themselves, so nothing is ever pooled by
        accident: pooling is something the processor has to ask for, with an
        identity that came from the camera.
        """
        if not self._config.enabled or not trace_id or not passage:
            return
        try:
            with self._lock:
                self._bindings[trace_id] = str(passage)
                self._bindings.move_to_end(trace_id)
                while len(self._bindings) > MAX_TRACKED_EVENTS:
                    self._bindings.popitem(last=False)
        except Exception:
            return

    def observe(self, trace_id, *, width=None, box=None,
                source: str = SOURCE_VEHICLE_BOX, at: float | None = None) -> None:
        """Record one frame's box width for this trace's passage.

        ``box`` is ``(x, y, width, height)`` in fractions of the whole frame,
        as ``local_recognizer.box_to_frame`` produces; ``width`` may be given
        instead. Anything unusable is dropped silently.
        """
        if not self._config.enabled or not trace_id or source not in SOURCES:
            return
        if width is None and box is None:
            return
        try:
            if width is None:
                width = box[2]
            width = float(width)
            if not isfinite(width) or width <= 0:
                return
            at = float(self._clock() if at is None else at)
            if not isfinite(at):
                return
            with self._lock:
                passage = self._passage(trace_id, at)
                passage.add(source, at, width)
            self._count("samples")
            LOGGER.debug(
                "gate_direction stage=sample trace_id=%s source=%s width=%.6f at=%.3f",
                trace_id, source, width, at,
            )
        except Exception:
            return

    def note_brightness(self, trace_id, brightness) -> None:
        """Record how light the scene was, for the night gate."""
        if not self._config.enabled or not trace_id:
            return
        try:
            brightness = float(brightness)
            if not isfinite(brightness):
                return
            with self._lock:
                self._passage(trace_id, self._clock()).note_brightness(brightness)
        except Exception:
            return

    def estimate(self, trace_id) -> DirectionEstimate:
        """This trace's verdict. ``unknown``/``none`` whenever in doubt."""
        if not self._config.enabled or not trace_id:
            return UNMEASURED
        try:
            with self._lock:
                passage = self._passages.get(self._key(trace_id))
                if passage is None:
                    return UNMEASURED
                series = {
                    source: list(samples)
                    for source, samples in passage.series.items()
                }
                brightest = passage.brightest
            if not series:
                self._settle("no_boxes")
                return UNMEASURED
            # Night is unmeasured (n=1 dark passage in the labelled set), so
            # a dark event says so rather than reporting a slope nobody has
            # validated.
            if brightest is not None and brightest < self._config.min_brightness:
                self._settle("night")
                return UNMEASURED
            best = UNMEASURED
            for source in SOURCES:
                candidate = estimate_direction(
                    series.get(source, ()), self._config, source=source,
                )
                if _prefer(candidate, best):
                    best = candidate
            self._settle(best.verdict if best.method == METHOD_BOX_WIDTH
                         else "no_boxes")
            return best
        except Exception:
            return UNMEASURED

    def forget(self, trace_id) -> None:
        """Drop a finished event's binding.

        The passage's samples deliberately outlive the event: the next frame
        of the same alarm is what makes the fit possible at all. They go when
        the passage goes -- the idle timeout, or the cap.
        """
        if not trace_id:
            return
        try:
            with self._lock:
                passage = self._bindings.pop(trace_id, None)
                if passage is None:
                    # An unbound trace was its own passage, so it ends here.
                    self._passages.pop(trace_id, None)
                self._expire(self._clock())
        except Exception:
            return

    def tracked(self) -> int:
        with self._lock:
            return len(self._passages)

    def counters(self) -> dict:
        with self._lock:
            return dict(self._counts)

    # -- internals -------------------------------------------------------

    def _key(self, trace_id: str) -> str:
        """Caller holds the lock. The passage this trace belongs to."""
        return self._bindings.get(trace_id, trace_id)

    def _passage(self, trace_id: str, at: float) -> _PassageDirection:
        """Caller holds the lock. This trace's passage, created if needed."""
        self._expire(at)
        key = self._key(trace_id)
        passage = self._passages.get(key)
        if passage is None:
            passage = _PassageDirection(at)
            self._passages[key] = passage
        passage.touched_at = at
        self._passages.move_to_end(key)
        while len(self._passages) > MAX_TRACKED_EVENTS:
            self._passages.popitem(last=False)
            self._counts["evicted"] = self._counts.get("evicted", 0) + 1
        return passage

    def _expire(self, now: float) -> None:
        """Caller holds the lock. Drop passages nothing has touched lately."""
        while self._passages:
            key, passage = next(iter(self._passages.items()))
            if now - passage.touched_at <= PASSAGE_TTL_SECONDS:
                return
            self._passages.pop(key, None)
            self._counts["expired"] = self._counts.get("expired", 0) + 1

    def _count(self, key: str) -> None:
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1

    def _settle(self, outcome: str) -> None:
        """Count one finished estimate and roll the counters up periodically."""
        with self._lock:
            self._counts[outcome] = self._counts.get(outcome, 0) + 1
            self._counts["estimates"] = self._counts.get("estimates", 0) + 1
            self._since_rollup += 1
            if self._since_rollup < COUNTER_LOG_INTERVAL:
                return
            self._since_rollup = 0
            counts = dict(self._counts)
        LOGGER.info(
            "gate_direction stage=counters estimates=%d entering=%d exiting=%d "
            "stationary=%d unknown=%d night=%d no_boxes=%d samples=%d "
            "evicted=%d expired=%d",
            counts.get("estimates", 0), counts.get(VERDICT_ENTERING, 0),
            counts.get(VERDICT_EXITING, 0), counts.get(VERDICT_STATIONARY, 0),
            counts.get(VERDICT_UNKNOWN, 0), counts.get("night", 0),
            counts.get("no_boxes", 0), counts.get("samples", 0),
            counts.get("evicted", 0), counts.get("expired", 0),
        )


def _prefer(candidate: DirectionEstimate, incumbent: DirectionEstimate) -> bool:
    """Keep the more informative of two per-source estimates.

    A fitted verdict beats an unfitted one; between two fitted verdicts the
    earlier source in ``SOURCES`` wins, which is why this only ever replaces
    an incumbent that measured less.
    """
    if candidate.method != METHOD_BOX_WIDTH:
        return False
    if incumbent.method != METHOD_BOX_WIDTH:
        return True
    if (incumbent.verdict == VERDICT_UNKNOWN) != (candidate.verdict == VERDICT_UNKNOWN):
        return incumbent.verdict == VERDICT_UNKNOWN
    return False


def _boolean(value, default: bool) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise DirectionConfigError("GATE_DIRECTION_ENABLED must be a boolean")


def _number(environment, key: str, default: float) -> float:
    raw = str(environment.get(key, "") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as error:
        raise DirectionConfigError(f"{key} must be a number") from error
    if not isfinite(value):
        raise DirectionConfigError(f"{key} must be finite")
    return value


def _integer(environment, key: str, default: int) -> int:
    raw = str(environment.get(key, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise DirectionConfigError(f"{key} must be an integer") from error


def _optional_ratio(value) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(number):
        return None
    return round(max(0.0, min(1.0, number)), 3)


def _optional_slope(value) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(number):
        return None
    return round(max(-MAX_SLOPE, min(MAX_SLOPE, number)), 6)


def _bounded_int(value, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return minimum
    return max(minimum, min(maximum, number))
