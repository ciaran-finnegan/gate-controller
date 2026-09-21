"""Hear what the gate did, from the sound the recorder already kept.

The controller fires the relay and then assumes the gate moved. Nothing
anywhere confirms it, and at this site most gate movements are not the
controller's doing at all: over 30 days, 296 passages produced 46 relay
firings. The other 250 were key fobs, keypads and hands, invisible to every
part of the system.

This reads the recorded segments and says what the gate actually did.

Why this is a rule and not a model
-----------------------------------
Measured on four commanded cycles on 2026-09-16 (events 2927-2930), the three
sounds at this gate occupy different bands and do not overlap:

* **the motor** puts 50-90% of its energy in 1.2-3 kHz, for the whole of its
  travel -- fifteen to twenty-seven seconds at a time;
* **the clang** of the leaves meeting is one transient with 67-79% of its
  energy above 3 kHz, peaking at -22 to -26 dBFS;
* **wind**, the only competing sound at this site, is 70-99% below 150 Hz and
  carries under 5% above 1.2 kHz.

A classifier trained on this would be learning a threshold. The thresholds
below were written from the first cycle and were correct on all four, and they
have the property a model would not: when one of them is wrong, it is obvious
which number to change and why.

The signals are also not equally informative, which is why both are kept:

* The motor says the gate *moved*, and how long for. Duration is what would
  distinguish a completed travel from a stall against an obstruction.
* The clang says the gate *shut*. A motor run that ends without one is the
  gate stopping open -- which is the moment a vehicle is about to drive out,
  and the only positive confirmation of closure the system can have.

What this is not
----------------
It is not a vehicle detector. Vehicle sound is untested, quieter than the gate,
and competes with the same sub-150 Hz wind; nothing here should be read as
evidence about it. Every threshold was measured in daylight, in one weather, in
a ten-minute window, with no vehicle passing through the gate. They are
starting values to be checked against a season, not constants.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import math
import cmath

LOGGER = logging.getLogger(__name__)

#: 1024 samples at the camera's 16 kHz is a 64 ms window, hopped by half, so
#: the analysis grid is 32 ms. The clang is a single event a few frames wide;
#: anything coarser would average it away against its own tail.
FRAME_SAMPLES = 1024
HOP_SAMPLES = 512

#: (low, high) in Hz. The split points are where the three sounds actually
#: divide, not round numbers: 150 is the top of the wind, 1200 the bottom of
#: the motor, 3000 the bottom of the clang's brightness.
BANDS = ((20, 150), (150, 400), (400, 1200), (1200, 3000), (3000, 8000))
WIND_BAND = 0
MOTOR_BANDS = (3, 4)
CLANG_BAND = 4

#: The motor against the wind. Measured motor frames run 0.42-0.92; measured
#: wind frames run 0.00-0.05. 0.40 sits in the empty space between, nearer the
#: motor, because a false motor run invents a gate movement that never happened.
MOTOR_HF_SHARE = 0.40
#: A run, not a click. The shortest measured travel was fifteen seconds.
MOTOR_MIN_SECONDS = 3.0
#: The motor's own sound fluctuates as the leaves swing; this bridges the dips
#: without joining an opening to the closing forty seconds later.
MOTOR_GAP_SECONDS = 1.5

#: The clang, measured at 0.67-0.79 of total energy above 3 kHz. The threshold
#: is well below that because the quietest part of the transient's tail still
#: belongs to it, and a level floor is what stops birdsong qualifying.
CLANG_HIGH_SHARE = 0.25
CLANG_MIN_DBFS = -38.0
#: How far the impact must stand above the audio around it.
#:
#: This is the number that stops the rule calling loud noise a latch. Measured
#: on the ten commanded cycles in 49.2 hours of retained recording, taking the
#: median level of the two seconds either side of each candidate:
#:
#: ===================  =========  ========  ==========
#: candidate            local med  peak      prominence
#: ===================  =========  ========  ==========
#: latch 09-19 08:28        -51.5     -24.2      27.3 dB
#: latch 09-19 18:35        -55.3     -27.8      27.5 dB
#: latch 09-20 09:58        -58.3     -26.2      32.1 dB
#: latch 09-20 15:12        -57.4     -25.2      32.2 dB
#: latch 09-20 18:16        -36.5     -15.0      21.5 dB
#: *not* a latch 18:59       -9.0      -5.5       3.5 dB
#: *not* a latch 12:29       -6.4      -5.4       1.0 dB
#: *not* a latch 16:58       -6.8      -5.3       1.5 dB
#: ===================  =========  ========  ==========
#:
#: The three at the bottom are a repeating chirp -- a bird, four calls a
#: second -- in audio the microphone has pinned at about -7 dBFS. Every frame
#: of it clears both the share and the level floor, because when everything is
#: loud a level floor means nothing. Clustering happened to keep the real
#: latch in those three windows, so this fixes a near miss rather than a
#: reported error: over the same 49.2 hours it removes 75 of 795 candidates
#: and keeps all eight of the commanded latches.
#:
#: Prominence is a difference of levels, so unlike a band *share* it has no
#: denominator to saturate, which is the same fault that killed the first
#: motor rule. Twelve decibels sits in the empty space between 3.5 and 21.5,
#: nearer the noise, because inventing a closure is worse than missing one:
#: the claim this supports is that the property is secured.
CLANG_PROMINENCE_DB = 12.0
#: How much audio either side of a candidate counts as "the background it
#: stands above". Two seconds is long enough to average over a gust and short
#: enough that the measurement belongs to this moment rather than to the
#: minute; it is also the window the eight commanded latches above were
#: measured over.
CLANG_BACKGROUND_SECONDS = 2.0
#: One impact rings for a few hundred milliseconds. Frames inside this of each
#: other are the same clang, reported once.
CLANG_CLUSTER_SECONDS = 1.5
#: How long after a motor stops a clang still belongs to it. Measured: the
#: clang lands 0.3-2.3 s before the closing run's last motor frame, so this is
#: generous in both directions.
CLANG_ATTRIBUTION_SECONDS = 4.0

#: How far either side of a relay firing a motor run still counts as ours.
#:
#: Wider than it looks like it needs to be, in both directions. Late, because
#: the measured gap from the relay to the motor starting is one to two seconds
#: and the auto-close follows forty seconds later. Early, because the detector
#: hears the motor start *before* the relay row is written: on 2026-09-17 at
#: 11:08 the run was timed 3.2 s ahead of the firing, partly onset error and
#: partly the arriving car's own engine, which reads much like the motor.
#:
#: Erring wide is the safe direction. A commanded opening mistaken for a fob
#: inflates the one number this exists to produce -- how often people give up
#: on recognition -- and a fob mistaken for ours only understates it.
COMMAND_BEFORE_SECONDS = 12.0
COMMAND_AFTER_SECONDS = 75.0


@dataclass(frozen=True)
class MotorRun:
    start: datetime
    end: datetime

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass(frozen=True)
class Clang:
    at: datetime
    high_share: float
    peak_dbfs: float


#: A movement two independent things agree on: the relay fired, or the latch
#: was heard, or both. Only these should ever be counted out loud.
CONFIRMED = "confirmed"
#: A movement only the motor classifier believes in. It may well be real -- a
#: fob opening fires no relay and a gate standing open has no latch to hear --
#: but nothing outside the model says so, and over 49.2 hours of recording the
#: previous model produced 25-59 of these a day that a person looking at the
#: spectrogram identified as wind, rain, a passing car or a farm machine.
UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True)
class GateMovement:
    """One motor run and what it turned out to be."""

    start: datetime
    end: datetime
    seconds: float
    #: ``shut`` when a clang closed it, ``open`` when the motor simply stopped.
    #: The second is the load-bearing one: it means the gate is standing open.
    outcome: str
    clang: Clang | None
    #: True when no relay command preceded this. Somebody used a fob, a keypad
    #: or a hand, and until now nothing in the system would have known.
    uncommanded: bool = False
    #: A relay firing brackets this run. Unlike ``uncommanded`` -- which is
    #: about openings only, because nobody commands the auto-close -- this is
    #: asked of every run, and is half of what ``confirmation`` is made of.
    commanded: bool = False
    #: A latch heard beside this run's end, *whatever the state machine made
    #: of it*. ``clang`` above is the subset the alternation accepted as proof
    #: of closure; this is the physical observation, kept separately because
    #: the alternation is the least reliable belief in the system and a latch
    #: is not. Measured on the ten commanded cycles in the retained audio: the
    #: latch is audible on eight of the nine whose auto-close was recorded,
    #: and six to seven of those eight land within the four seconds of a motor
    #: run's end that the state machine will look at -- but whether that run
    #: is read as a *closing* depends on an alternation that one false
    #: movement inverts for the rest of the scan.
    latch: Clang | None = None

    @property
    def confirmation(self) -> str:
        """Whether anything outside the motor model agrees this happened."""
        return CONFIRMED if (self.commanded or self.latch is not None) else UNCONFIRMED

    def as_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "seconds": round(self.seconds, 2),
            "outcome": self.outcome,
            "uncommanded": self.uncommanded,
            "confirmation": self.confirmation,
            "clang": None if self.clang is None else {
                "at": self.clang.at.isoformat(),
                "high_share": round(self.clang.high_share, 3),
                "peak_dbfs": round(self.clang.peak_dbfs, 1),
            },
            "latch": None if self.latch is None else {
                "at": self.latch.at.isoformat(),
                "high_share": round(self.latch.high_share, 3),
                "peak_dbfs": round(self.latch.peak_dbfs, 1),
            },
        }


def _fft(values):
    size = len(values)
    if size == 1:
        return values
    even, odd = _fft(values[0::2]), _fft(values[1::2])
    out = [0j] * size
    for k in range(size // 2):
        twiddle = cmath.exp(-2j * math.pi * k / size) * odd[k]
        out[k] = even[k] + twiddle
        out[k + size // 2] = even[k] - twiddle
    return out


def analyse_frames(samples, sample_rate: int, started_at: datetime):
    """Per-frame level and band shares. Pure arithmetic, no decoder state.

    ``samples`` is signed 16-bit PCM as any integer sequence. Yields a dict per
    frame so the rules below read as rules rather than as index arithmetic.
    """
    window = [0.5 - 0.5 * math.cos(2 * math.pi * i / (FRAME_SAMPLES - 1))
              for i in range(FRAME_SAMPLES)]
    edges = [(int(lo * FRAME_SAMPLES / sample_rate),
              max(int(hi * FRAME_SAMPLES / sample_rate),
                  int(lo * FRAME_SAMPLES / sample_rate) + 1))
             for lo, hi in BANDS]
    for start in range(0, len(samples) - FRAME_SAMPLES, HOP_SAMPLES):
        frame = [samples[start + i] / 32768.0 * window[i] for i in range(FRAME_SAMPLES)]
        rms = math.sqrt(sum(v * v for v in frame) / FRAME_SAMPLES)
        power = [abs(c) ** 2 for c in _fft([complex(v, 0) for v in frame])[:FRAME_SAMPLES // 2]]
        band = [sum(power[a:b]) for a, b in edges]
        total = sum(band) or 1e-12
        yield {
            "at": started_at + timedelta(seconds=start / sample_rate),
            "dbfs": 20 * math.log10(rms) if rms > 1e-12 else -120.0,
            "hf_share": (band[MOTOR_BANDS[0]] + band[MOTOR_BANDS[1]]) / total,
            "high_share": band[CLANG_BAND] / total,
            "wind_share": band[WIND_BAND] / total,
        }


def find_motor_runs(frames) -> list[MotorRun]:
    """Stretches where the motor was turning."""
    step = HOP_SAMPLES / 16000.0
    runs: list[MotorRun] = []
    start = last = None
    quiet = 0.0
    for frame in frames:
        if frame["hf_share"] >= MOTOR_HF_SHARE:
            if start is None:
                start = frame["at"]
            last = frame["at"]
            quiet = 0.0
        elif start is not None:
            quiet += step
            if quiet > MOTOR_GAP_SECONDS:
                if (last - start).total_seconds() >= MOTOR_MIN_SECONDS:
                    runs.append(MotorRun(start, last))
                start = last = None
    if start is not None and (last - start).total_seconds() >= MOTOR_MIN_SECONDS:
        runs.append(MotorRun(start, last))
    return runs


def find_clangs(frames) -> list[Clang]:
    """Impacts bright enough to be metal, loud enough, and *prominent*.

    The third test is what was missing. ``high_share`` and ``dbfs`` both
    describe the frame on its own, and a frame of continuous loud noise
    satisfies them as readily as an impact does -- which is why a bird calling
    four times a second into a microphone pinned at -7 dBFS produced latch
    candidates at all, in three of the eight commanded cycles measured.

    An impact is by definition a departure from what came before it. The
    caller passes the window it cares about -- ``scan_segment`` passes the
    four seconds either side of a motor run's end -- and the median level of
    that window is the background the candidate has to stand above.
    """
    frames = list(frames)
    if not frames:
        return []
    levels = [frame["dbfs"] for frame in frames]
    span = max(1, int(CLANG_BACKGROUND_SECONDS / (HOP_SAMPLES / 16000.0)))
    found: list[Clang] = []
    for index, frame in enumerate(frames):
        if frame["high_share"] < CLANG_HIGH_SHARE or frame["dbfs"] < CLANG_MIN_DBFS:
            continue
        # The background is taken from beside this frame rather than from the
        # whole of whatever the caller passed in: a caller handing over two
        # minutes would otherwise measure a candidate against a median from a
        # different minute of weather.
        near = sorted(levels[max(0, index - span):index + span + 1])
        middle = len(near) // 2
        background = (near[middle] if len(near) % 2
                      else (near[middle - 1] + near[middle]) / 2)
        if frame["dbfs"] < background + CLANG_PROMINENCE_DB:
            continue
        candidate = Clang(frame["at"], frame["high_share"], frame["dbfs"])
        if found and (candidate.at - found[-1].at).total_seconds() <= CLANG_CLUSTER_SECONDS:
            # The same impact still ringing. Keep whichever frame of it was
            # loudest, so the reported level is the impact's, not its tail's.
            if candidate.peak_dbfs > found[-1].peak_dbfs:
                found[-1] = candidate
            continue
        found.append(candidate)
    return found


def movements(frames, *, commanded_at=(), initial_state: str = "shut") -> list[GateMovement]:
    """What the gate did: one entry per motor run, with how it ended.

    Two facts from the real recordings shape this, and both were learned the
    hard way by running an earlier version over them:

    **A clang is not by itself proof the gate shut.** The leaves meeting is
    metallic, but so is the gate reaching its *open* end-stop. On event 2927
    the opening run carried a clang 3.5 s before the motor stopped, and the
    four measured closing clangs land 1.1-2.5 s before theirs -- overlapping
    ranges, so timing alone cannot separate them. What separates them is that
    a gate alternates: only a run that is *closing* can end shut.

    **Only an opening can be uncommanded.** The auto-close is never commanded
    by anybody, so charging every closing run to "somebody used a fob" made
    the flag fire on all four commanded cycles. A fob is an *opening* the
    controller did not ask for.

    ``initial_state`` is what the gate is believed to be doing at the start of
    the window -- ``shut`` by default, since that is where a gate rests.
    """
    frames = list(frames)
    return movements_from(
        find_motor_runs(frames), find_clangs(frames),
        commanded_at=commanded_at, initial_state=initial_state,
    )


def movements_from(runs, clangs, *, commanded_at=(), initial_state: str = "shut") -> list[GateMovement]:
    """The same state machine, over runs and clangs found however you like.

    The band-ratio rule above finds both, and on a quiet night it saturates and
    calls 62% of an empty half hour a gate moving -- which is why the motor is
    now found by a classifier over pretrained embeddings instead. The clang is
    not affected: it is a transient in a band that is empty the rest of the
    time, and share has nothing to saturate against over 32 ms.

    What neither of them can do is decide what a run *meant*. That needs the
    gate's alternation, and it lives here, once.
    """
    runs = sorted(runs, key=lambda run: run.start)
    clangs = list(clangs)
    commands = sorted(commanded_at)
    state = initial_state if initial_state in {"shut", "open"} else "shut"
    out: list[GateMovement] = []
    for run in runs:
        closing = state == "open"
        # The loudest latch beside this run's end, found whatever the state
        # machine believes. It is recorded either way and only *interpreted*
        # when the gate was open, which keeps the outcome rule exactly as it
        # was while stopping a heard latch from being thrown away because the
        # alternation had drifted.
        latch = None
        for clang in clangs:
            offset = (clang.at - run.end).total_seconds()
            if -CLANG_ATTRIBUTION_SECONDS <= offset <= CLANG_ATTRIBUTION_SECONDS:
                if latch is None or clang.peak_dbfs > latch.peak_dbfs:
                    latch = clang
        ending = latch if closing else None
        if closing:
            # A closing run with no clang did not finish: the gate is stuck,
            # was reversed, or the recorder missed it. "open" is the safe
            # reading, because it is the one that does not claim the property
            # is secured.
            outcome = "shut" if ending is not None else "open"
        else:
            outcome = "open"
        commanded = any(
            -COMMAND_BEFORE_SECONDS <= (run.start - moment).total_seconds() <= COMMAND_AFTER_SECONDS
            for moment in commands
        )
        uncommanded = (not closing) and not commanded
        out.append(GateMovement(
            start=run.start, end=run.end, seconds=run.seconds,
            outcome=outcome, clang=ending, uncommanded=uncommanded,
            commanded=commanded, latch=latch,
        ))
        state = "shut" if outcome == "shut" else "open"
    return out


def summarise(moves) -> dict:
    """The state the gate was left in, and the counts worth a heartbeat."""
    moves = list(moves)
    if not moves:
        return {"movements": 0, "final_state": "unknown", "uncommanded": 0,
                "shut": 0, "left_open": 0, "confirmed": 0, "unconfirmed": 0,
                "latches_heard": 0}
    return {
        "movements": len(moves),
        # The count worth saying out loud, and the count that is only the
        # model's opinion. Reporting the sum as though it were one number is
        # how a hundred and twenty movements a day came to be presented as a
        # fact about a gate that moves about twenty times.
        "confirmed": sum(1 for m in moves if m.confirmation == CONFIRMED),
        "unconfirmed": sum(1 for m in moves if m.confirmation == UNCONFIRMED),
        "latches_heard": sum(1 for m in moves if m.latch is not None),
        # The last movement is what the gate is doing now: if it ended with a
        # clang the gate is shut, and if it did not, the gate is standing open.
        "final_state": "shut" if moves[-1].outcome == "shut" else "open",
        "final_at": moves[-1].end.isoformat(),
        "uncommanded": sum(1 for m in moves if m.uncommanded),
        "shut": sum(1 for m in moves if m.outcome == "shut"),
        "left_open": sum(1 for m in moves if m.outcome == "open"),
    }
