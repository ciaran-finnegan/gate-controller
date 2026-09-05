"""Log cloud-path failures as transitions instead of once per attempt."""
import logging
import time
from collections.abc import Callable

import requests

DEFAULT_REPEAT_INTERVAL_SECONDS = 600.0


def error_detail(error: BaseException) -> str:
    """A bounded, secret-free description of a cloud request failure."""
    if isinstance(error, requests.HTTPError):
        status = getattr(getattr(error, "response", None), "status_code", None)
        if isinstance(status, int):
            return f"http_{status}"
    return type(error).__name__


class TransitionLogger:
    """Warn on the first failure, repeat at most every interval, note recovery."""

    def __init__(self, logger: logging.Logger, stage: str, *,
                 repeat_interval: float = DEFAULT_REPEAT_INTERVAL_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        if not repeat_interval > 0:
            raise ValueError("repeat_interval must be positive")
        self._logger = logger
        self._stage = stage
        self._repeat_interval = repeat_interval
        self._clock = clock
        self._consecutive_failures = 0
        self._last_logged_at: float | None = None

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def failure(self, error: BaseException) -> None:
        self._consecutive_failures += 1
        now = self._clock()
        if (
            self._last_logged_at is not None
            and now - self._last_logged_at < self._repeat_interval
        ):
            return
        self._last_logged_at = now
        self._logger.warning(
            "gate_cloud stage=%s_failed error_type=%s detail=%s consecutive=%d",
            self._stage, type(error).__name__, error_detail(error),
            self._consecutive_failures,
        )

    def success(self) -> None:
        if self._consecutive_failures:
            self._logger.info(
                "gate_cloud stage=%s_recovered failures=%d",
                self._stage, self._consecutive_failures,
            )
        self._consecutive_failures = 0
        self._last_logged_at = None
