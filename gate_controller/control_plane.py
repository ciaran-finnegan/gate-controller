import logging
import time
from threading import Event, Lock

from .cloud_health import TransitionLogger

LOGGER = logging.getLogger(__name__)


class HeartbeatWorker:
    def __init__(self, control_plane, status, poll_interval: float = 15.0, *,
                 health: TransitionLogger | None = None, clock=time.perf_counter):
        self._control_plane = control_plane
        self._status = status
        self._poll_interval = poll_interval
        self._health = health or TransitionLogger(LOGGER, "heartbeat")
        self._clock = clock
        self._lock = Lock()
        self._last_rtt_ms: float | None = None

    def metrics(self) -> dict:
        """The measured cost of the POST this worker already makes.

        The round trip is timed rather than probed, so measuring the Pi to
        Cloudflare path costs no extra traffic at all. The value reported is
        the previous POST's, because the current one has not returned yet.
        """
        with self._lock:
            return {
                "heartbeat_rtt_ms": self._last_rtt_ms,
                "heartbeat_consecutive_failures": self._health.consecutive_failures,
            }

    def run_once(self) -> bool:
        # Timing is an observation, never a precondition: a clock that fails
        # must cost the measurement, not the heartbeat.
        try:
            started = self._clock()
        except Exception:
            started = None
        try:
            self._control_plane.heartbeat(self._status())
        except Exception as error:
            self._record_round_trip(started)
            self._health.failure(error)
            return False
        self._record_round_trip(started)
        self._health.success()
        return True

    def _record_round_trip(self, started: float | None) -> None:
        if started is None:
            return
        try:
            elapsed = (self._clock() - started) * 1000
        except Exception:
            return
        with self._lock:
            self._last_rtt_ms = round(max(0.0, elapsed), 1)

    def run_forever(self, stop_event: Event) -> None:
        while not stop_event.is_set():
            self.run_once()
            stop_event.wait(self._poll_interval)
