from threading import Event
class HeartbeatWorker:
    def __init__(self, control_plane, status, poll_interval: float = 15.0):
        self._control_plane = control_plane
        self._status = status
        self._poll_interval = poll_interval

    def run_once(self) -> bool:
        try:
            self._control_plane.heartbeat(self._status())
        except Exception:
            return False
        return True

    def run_forever(self, stop_event: Event) -> None:
        while not stop_event.is_set():
            self.run_once()
            stop_event.wait(self._poll_interval)
