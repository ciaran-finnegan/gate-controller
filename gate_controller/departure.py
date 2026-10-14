"""Withholding the automatic pulse from a car that is leaving (gate-controller#171).

The relay is on the TOPENS operator's step-by-step input. A departing car
comes from behind the camera, is let out by the exit wand or the auto-open,
and shows the camera its rear plate -- which is on the same allow-list, so
the Pi used to pulse for it. A pulse then lands on a gate that is open or
moving: an open gate starts closing behind the car, a moving one stops dead,
and stopped leaves pulsed again can cross and jam (docs/gate-operator.md).
From 2026-10-10 18:26 to 2026-10-14 15:30 UTC eight of the eighteen vehicle
pulses went to departing cars; on 2026-10-14 at 13:11 the pulse closed a gate
that had begun opening for the car eleven seconds earlier.

The signal is the sweep's own plate widths (`trigger_capture.DepartingPlate`):
a departing car's plate shrinks as it recedes up the approach, an arriving
car's only grows until it stops. The verdict is often late -- a grant can land
before the plate has shrunk far enough to say so -- so a grant whose passage
has not yet shown its plate coming closer is *held* for up to
``DEFAULT_HOLD_SECONDS`` while the sweep keeps reading, and pulsed the moment
the reads settle it (or the hold runs out). A grant whose passage has shown
nothing at all -- no boxed plate yet, or only far ones -- is not held.

``GATE_DEPARTING_WITHHOLD=off|shadow|on`` (default ``shadow``):

* ``off``: nothing is asked.
* ``shadow``: every automatic pulse goes exactly as before, with no hold; the
  journal says what ``on`` would have done (``gate_actuation departure=shadow``
  and, for a grant ``on`` would have held, ``stage=settled``).
* ``on``: a grant judged departing is withheld and recorded as the denial it
  is, ``reason``/``actuation_outcome`` ``departing``; an undecided one is held.

This can only withhold or delay a pulse. It never adds, retries or hurries one,
and a person's command from the app is never asked -- nor kept waiting: a
command that arrives while a grant is held ends the hold at once, and the
grant is judged on what the reads have said by then.

The hold waits inside ``ActuationCoordinator.actuate``, on the thread that
decided the frame -- the burst thread for a frame the device read, the cloud
lane for a cloud answer -- under the coordinator's lock. It polls the passage
and sleeps; it never touches the network. The sweep reads on its own thread
meanwhile, and its reads are what settle the hold.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)

ENV_MODE = "GATE_DEPARTING_WITHHOLD"
ENV_HOLD = "GATE_DEPARTING_HOLD_SECONDS"
MODES = ("off", "shadow", "on")
DEFAULT_MODE = "shadow"

#: How long an automatic grant whose passage is undecided may wait for the
#: sweep's reads to settle it. Replayed on the eight departing pulses of
#: 2026-10-10..14, the three the verdict reached late were settled 1.16 s,
#: 1.25 s and 1.45 s after their grant; 2.0 s leaves half a second over the
#: slowest. Of the ten arrivals let in over the same days none was held at
#: all: every one had already shown its plate growing, or showed nothing to
#: measure. The hold never runs past the grant's own decision deadline or
#: its frame's freshness (the processor passes what is left of both).
DEFAULT_HOLD_SECONDS = 2.0
MAX_HOLD_SECONDS = 3.0
#: Left over when a hold is cut short by the grant's own deadline, so the
#: pulse that follows still lands inside it.
HOLD_DEADLINE_MARGIN_SECONDS = 0.25
#: How often a held grant asks the passage again. The sweep reads two to
#: three frames a second.
HOLD_POLL_SECONDS = 0.05

#: The passage's plate has receded far enough, for long enough, to say the
#: car is leaving. The grant is withheld.
DEPARTING = "departing"
#: Plates have been boxed, but the first is still the widest, or the latest
#: is well short of the widest: the reads have not yet said which way.
UNDECIDED = "undecided"
#: The plate has grown since it was first boxed and is near its widest.
APPROACHING = "approaching"
#: The plate has stood still across the last couple of seconds: a car stopped
#: in view -- waiting at the gate -- is not leaving. (A departing car that
#: stops beside the camera looks the same, and is not caught: see
#: docs/vehicle-direction.md.)
STANDING = "standing"
#: Plates were boxed, but none wide enough to measure a shrink from.
FAR = "far"
#: The states a held grant waits through. Everything else is decided.
HELD_STATES = frozenset({UNDECIDED})


@dataclass(frozen=True)
class DepartureConfig:
    mode: str = DEFAULT_MODE
    hold_seconds: float = DEFAULT_HOLD_SECONDS

    @property
    def active(self) -> bool:
        return self.mode in ("shadow", "on")


@dataclass(frozen=True)
class DirectionReading:
    """What one passage's plate widths say, at one moment."""

    state: str
    peak_px: int = 0
    latest_px: int = 0
    reads: int = 0

    def describe(self) -> str:
        return (
            f"state={self.state} peak_px={self.peak_px} latest_px={self.latest_px} "
            f"reads={self.reads}"
        )


def load_config(environment) -> DepartureConfig:
    """``GATE_DEPARTING_WITHHOLD`` and ``GATE_DEPARTING_HOLD_SECONDS``. Never raises.

    An unreadable value is rejected whole, logged at ERROR and replaced by its
    shipped default: a typo must neither take the controller down nor switch
    the withholding on.
    """
    environment = environment or {}
    raw_mode = str(environment.get(ENV_MODE, "") or "").strip().lower()
    mode = DEFAULT_MODE
    if raw_mode:
        if raw_mode in MODES:
            mode = raw_mode
        else:
            LOGGER.error(
                "departure_withhold key=%s status=rejected allowed=%s using_default=%s",
                ENV_MODE, ",".join(MODES), DEFAULT_MODE,
            )
    raw_hold = str(environment.get(ENV_HOLD, "") or "").strip()
    hold = DEFAULT_HOLD_SECONDS
    if raw_hold:
        try:
            value = float(raw_hold)
        except (TypeError, ValueError):
            value = math.nan
        if math.isfinite(value) and 0.0 <= value <= MAX_HOLD_SECONDS:
            hold = value
        else:
            LOGGER.error(
                "departure_withhold key=%s status=rejected allowed=0..%g using_default=%g",
                ENV_HOLD, MAX_HOLD_SECONDS, DEFAULT_HOLD_SECONDS,
            )
    return DepartureConfig(mode=mode, hold_seconds=hold)


def read_direction(reader) -> DirectionReading | None:
    """Ask ``reader`` once. Never raises; anything unusable is None (unjudged)."""
    if reader is None:
        return None
    try:
        reading = reader()
    except Exception:
        LOGGER.warning("gate_actuation departure=unreadable", exc_info=True)
        return None
    return reading if isinstance(reading, DirectionReading) else None


_UNREAD = object()


def settle(reader, *, hold_seconds: float, budget=None, clock, sleep,
           poll: float = HOLD_POLL_SECONDS,
           first=_UNREAD, stop=None) -> tuple[DirectionReading | None, float]:
    """Wait while ``reader`` says the passage is undecided; return the last reading.

    Bounded by ``hold_seconds`` and by ``budget`` -- a callable answering the
    seconds left before the grant's own deadline -- less
    ``HOLD_DEADLINE_MARGIN_SECONDS``, and ended at once when ``stop`` answers
    true (a person's command is waiting for the coordinator). ``first`` is an
    answer already had, so the passage is not asked twice in the same
    instant. Returns the reading the wait ended on and how long it lasted,
    by ``clock``. Never raises.
    """
    reading = read_direction(reader) if first is _UNREAD else first
    try:
        started = clock()
    except Exception:
        return reading, 0.0
    while reading is not None and reading.state in HELD_STATES:
        try:
            if stop is not None and stop():
                break
            remaining = hold_seconds - (clock() - started)
            if budget is not None:
                remaining = min(remaining, float(budget()) - HOLD_DEADLINE_MARGIN_SECONDS)
        except Exception:
            break
        if not remaining > 0:
            break
        try:
            sleep(min(poll, remaining))
        except Exception:
            break
        reading = read_direction(reader)
    try:
        held = max(0.0, clock() - started)
    except Exception:
        held = 0.0
    return reading, held
