"""Who may use the uplink, and in what order.

One roughly 4.5 Mbit/s link carries three very different kinds of traffic, and
they are not equal:

1. **Gate decisions.** A camera event, a presence session, an OCR request. A
   vehicle is at the gate and a person is waiting for it to open.
2. **Event delivery.** The outbox, carrying events and evidence images that
   the owner is waiting to see. Measured delivery lag is already p90 84 s with
   p99 pinned at the 600 s ceiling, so this tier is not idle either.
3. **The training corpus.** Frames nobody is waiting for.

This module makes that order explicit and testable. Tier 1 marks itself busy
around the work it is doing; tier 2 is read from the outbox queue depth; tier 3
asks :meth:`ActivityGate.blocked_by` before it starts and again between
artefacts, and watches :meth:`ActivityGate.epoch` while it is sending so it can
abandon a transfer the moment a vehicle arrives rather than finish it.

The gate deliberately knows nothing about uploading. It reports state; the
decision to defer belongs to whoever is about to spend the link.
"""

from __future__ import annotations

from contextlib import contextmanager
from threading import Lock
from time import monotonic

#: How long the link must have been idle before the corpus may use it. Long
#: enough that a presence session's gaps -- the spacing between frames, the
#: wait for a verdict -- never look like quiet.
DEFAULT_QUIET_SECONDS = 60.0
MIN_QUIET_SECONDS = 5.0
MAX_QUIET_SECONDS = 3600.0

#: The blocking reasons, in the order they are checked. Each is a fixed token
#: so the journal line stays greppable.
BLOCKED_GATE_ACTIVITY = "gate_activity"
BLOCKED_QUIET_WINDOW = "quiet_window"
BLOCKED_EVENT_DELIVERY = "event_delivery"
BLOCKED_UNKNOWN_QUEUE = "queue_unreadable"


def bounded_quiet_seconds(value) -> float:
    seconds = float(value)
    if seconds != seconds or seconds in (float("inf"), float("-inf")):
        raise ValueError("the corpus quiet window must be finite")
    if not MIN_QUIET_SECONDS <= seconds <= MAX_QUIET_SECONDS:
        raise ValueError(
            f"the corpus quiet window must be between {MIN_QUIET_SECONDS:g} "
            f"and {MAX_QUIET_SECONDS:g} seconds"
        )
    return seconds


class ActivityGate:
    """The controller's own account of whether the link is wanted right now."""

    def __init__(self, *, quiet_seconds: float = DEFAULT_QUIET_SECONDS,
                 pending_events=None, clock=monotonic):
        self._quiet_seconds = bounded_quiet_seconds(quiet_seconds)
        self._pending_events = pending_events
        self._clock = clock
        self._lock = Lock()
        self._depth = 0
        self._reason: str | None = None
        # Bumped on every begin(). A transfer that started at one epoch and
        # sees another has been overtaken by a gate event, even if that event
        # has already finished: comparing epochs cannot miss a short one the
        # way a busy flag can.
        self._epoch = 0
        self._activities = 0
        self._idle_since = clock()

    # -- tier 1: gate decisions ------------------------------------------
    def begin(self, reason: str) -> None:
        """Mark the link as wanted by a gate decision. Never raises."""
        token = _token(reason)
        with self._lock:
            self._depth += 1
            self._epoch += 1
            self._activities += 1
            self._reason = token

    def end(self, reason: str = "") -> None:
        """Release one :meth:`begin`. Never raises, and never goes negative."""
        with self._lock:
            self._depth = max(0, self._depth - 1)
            if self._depth == 0:
                self._reason = None
                self._idle_since = self._clock()

    @contextmanager
    def activity(self, reason: str):
        """Hold the link for the duration of one gate decision."""
        self.begin(reason)
        try:
            yield self
        finally:
            self.end(reason)

    # -- tier 3: what the corpus asks ------------------------------------
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    def disturbed_since(self, epoch: int) -> bool:
        """Has a gate decision started, or is one running, since ``epoch``?"""
        with self._lock:
            return self._epoch != epoch or self._depth > 0

    def busy_reason(self) -> str | None:
        with self._lock:
            return self._reason if self._depth > 0 else None

    def quiet_seconds(self) -> float:
        """Seconds since the last gate decision finished; 0 while one runs."""
        with self._lock:
            if self._depth > 0:
                return 0.0
            return max(0.0, self._clock() - self._idle_since)

    def blocked_by(self) -> str | None:
        """The reason the corpus must not transmit, or ``None`` if it may.

        The order is the priority ladder: a gate decision beats the quiet
        window, which beats event delivery, which beats the corpus. A queue
        depth that cannot be read counts as work pending -- not being able to
        tell is not permission to compete.
        """
        if self.busy_reason() is not None:
            return BLOCKED_GATE_ACTIVITY
        if self.quiet_seconds() < self._quiet_seconds:
            return BLOCKED_QUIET_WINDOW
        if self._pending_events is None:
            return None
        try:
            pending = self._pending_events()
        except Exception:
            return BLOCKED_UNKNOWN_QUEUE
        if not isinstance(pending, int) or isinstance(pending, bool):
            return BLOCKED_UNKNOWN_QUEUE
        return BLOCKED_EVENT_DELIVERY if pending > 0 else None

    def status(self) -> dict:
        return {
            "quiet_window_seconds": self._quiet_seconds,
            "quiet_for_seconds": round(self.quiet_seconds(), 1),
            "busy": self.busy_reason(),
            "activities": self._activities,
        }


class NullActivityGate(ActivityGate):
    """A gate that is never busy and never asks anyone to wait.

    The default wherever a gate is optional, so a component built without one
    -- every existing test, and any deployment with no corpus upload -- behaves
    exactly as it did before this module existed.
    """

    def __init__(self):
        super().__init__(quiet_seconds=MIN_QUIET_SECONDS)

    def blocked_by(self) -> str | None:
        return None


NULL_GATE = NullActivityGate()


def _token(reason: object) -> str:
    """A short, greppable activity name. Never raises on odd input."""
    try:
        collapsed = "_".join(str(reason).split())
    except Exception:
        return "activity"
    kept = "".join(
        character for character in collapsed
        if character.isalnum() or character in "_-."
    )
    return kept[:32] or "activity"
