import logging
from threading import Event

from .cloud_health import TransitionLogger

LOGGER = logging.getLogger(__name__)


class HeartbeatWorker:
    def __init__(self, control_plane, status, poll_interval: float = 15.0, *,
                 health: TransitionLogger | None = None):
        self._control_plane = control_plane
        self._status = status
        self._poll_interval = poll_interval
        self._health = health or TransitionLogger(LOGGER, "heartbeat")

    def run_once(self) -> bool:
        try:
            self._control_plane.heartbeat(self._status())
        except Exception as error:
            self._health.failure(error)
            return False
        self._health.success()
        return True

    def run_forever(self, stop_event: Event) -> None:
        while not stop_event.is_set():
            self.run_once()
            stop_event.wait(self._poll_interval)
