import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import re
from collections.abc import Callable, Mapping
from io import BytesIO
from math import isfinite
from pathlib import Path
from threading import Lock, Thread
from time import monotonic, sleep

from PIL import Image

from .backpressure import NULL_GATE
from .direction import (
    SOURCE_LOCAL_PLATE_BOX, SOURCE_PLATE_BOX, SOURCE_VEHICLE_BOX,
)
from .local_recognizer import CLOUD_ALWAYS, NULL_FRAME, STATUS_NO_PLATE, box_to_frame
from .matching import normalise_plate
from .outbox import is_connection_failure
from .plate_region import PlateRegion
from .models import PlateObservation


DEFAULT_ENDPOINT = "https://api.platerecognizer.com/v1/plate-reader/"
DEFAULT_TIMEOUT = (1, 2)
MIN_UPLOAD_WIDTH = 640
MAX_UPLOAD_WIDTH = 3840
UPLOAD_JPEG_QUALITY = 85


@dataclass
class LocalPass:
    """What the on-device reader did for one frame, before any cloud request.

    Returned by :meth:`PlateRecognizerClient.local_pass`, which the processor
    runs *outside* its single serial OCR slot. ``observation`` is the local
    answer when it may decide the frame on its own; ``state`` carries the work
    already done -- the prepared upload bytes, the geometry, and the frame's
    pairing handle -- into the cloud request that follows when it may not.

    Nothing here holds the OCR slot, so a 170 ms local read never waits behind
    a 2.5 s cloud call for an older frame.
    """

    observation: PlateObservation | None = None
    state: dict | None = None

    @property
    def decided(self) -> bool:
        return self.observation is not None

    @property
    def saw_no_plate(self) -> bool:
        """The on-device detector found nothing at all in this frame.

        Distinct from a read too weak to decide: a candidate that was read and
        refused still says a plate is there for the cloud to try, and an
        ``unavailable`` answer (engine not ready, wait exhausted) is no verdict
        either. Only a completed read that found nothing answers True.
        """
        recognition = (self.state or {}).get("recognition")
        if recognition is None:
            return False
        return (
            getattr(recognition, "status", None) == STATUS_NO_PLATE
            and not getattr(recognition, "plate", None)
        )

    def abandon(self) -> None:
        """No cloud request will follow. Settle the frame so it is journalled."""
        frame = (self.state or {}).get("frame")
        if frame is None:
            return
        try:
            frame.abandon_cloud()
        except Exception:
            return


@dataclass(frozen=True)
class _UploadGeometry:
    """How the uploaded image maps back onto the camera frame."""
    frame_width: int
    frame_height: int
    crop_left: int
    crop_top: int
    crop_width: int
    crop_height: int
    upload_width: int
    upload_height: int
    precropped: bool
    cropped: bool

# Plate Recognizer's cloud API throttles each account to one request per
# second, counted from when a request finishes arriving, and answers a faster
# follow-up with HTTP 429 ("Expected available in 1 second"). A burst of three
# frames therefore lost its second request every time. Requests are paced
# process-wide from the previous response, and a throttled or connection-level
# failure is retried once. The API also closes idle keep-alive connections;
# posting on one fails instantly, so an idle session is recycled first.
MIN_REQUEST_INTERVAL_SECONDS = 1.05
# The floor the processor uses when it splits a remaining decision budget into
# connect and read timeouts. Re-sizing that split after a local read has to
# respect the same floor, or a request would be posted with a zero timeout.
MIN_SOCKET_TIMEOUT_SECONDS = 0.1
SESSION_IDLE_RECYCLE_SECONDS = 20.0
MAX_RETRY_AFTER_SECONDS = 2.0
MAX_TRANSIENT_RETRIES = 1
RETRYABLE_STATUS = 429
# A connect timeout never reached the API, so like a refused or dropped
# connection it costs nothing to retry once on a fresh socket. Read timeouts
# are not retried: the request may already have been accepted and billed.
RETRYABLE_TRANSPORT_CAUSES = frozenset({"connection_error", "tls_error", "connect_timeout"})

# Bounded, operator-facing labels describing *why* an OCR attempt failed. They
# separate network problems from API problems without ever carrying a response
# body, a credential, or a filesystem path.
CAUSE_CONNECT_TIMEOUT = "connect_timeout"
CAUSE_READ_TIMEOUT = "read_timeout"
CAUSE_REQUEST_TIMEOUT = "request_timeout"
CAUSE_TLS_ERROR = "tls_error"
CAUSE_CONNECTION_ERROR = "connection_error"
CAUSE_REQUEST_ERROR = "request_error"
CAUSE_INVALID_JSON = "invalid_json"
CAUSE_INVALID_PAYLOAD = "invalid_payload"
CAUSE_INVALID_RESULTS = "invalid_results"
CAUSE_INVALID_RESULT_ENTRY = "invalid_result_entry"
CAUSE_NO_USABLE_PLATE = "no_usable_plate"
CAUSE_INVALID_CONFIDENCE = "invalid_confidence"
CAUSE_INVALID_RESPONSE = "invalid_response"
CAUSE_INVALID_HTTP_STATUS = "http_invalid_status"
CAUSE_REQUEST_ABANDONED = "request_abandoned"
CAUSE_CLIENT_CLOSED = "client_closed"
CAUSE_UNCLASSIFIED = "unclassified"

# Why a frame was not handed to the cloud reader (`gate_ocr stage=cloud_skipped
# reason=...`). `internet_down` is the network probe's fresh, definite
# `failed`; `cloud_unreachable` is this client's own circuit breaker, open
# because its last requests died on the link without an answer.
REASON_INTERNET_DOWN = "internet_down"
REASON_CLOUD_UNREACHABLE = "cloud_unreachable"

# The circuit breaker on the cloud plate reader. Measured on the Pi on
# 2026-09-22 at 40 % packet loss: the probe's single TLS open succeeded, so
# `internet_reachable` said yes, and every passage still handed up to five
# frames to the cloud reader, each dying after ~6 s with `connection_error` or
# `ReadTimeout` (220 ConnectionError + 19 ReadTimeout in the outbox alone over
# three hours). Three such deaths in a row, within five minutes, are taken as
# the state of the link: the breaker opens for a minute, and every doubling
# after a failed trial is capped at ten minutes. A passage is ~5 requests, so
# a lossy link opens it inside one passage; a healthy link with one blip does
# not, because any answer at all -- a plate, an empty result, a 4xx or a 5xx --
# resets the count.
BREAKER_FAILURE_THRESHOLD = 3
BREAKER_FAILURE_WINDOW_SECONDS = 300.0
BREAKER_OPEN_SECONDS = 60.0
BREAKER_OPEN_MAX_SECONDS = 600.0
BREAKER_CLOSED = "closed"
BREAKER_OPEN = "open"
BREAKER_HALF_OPEN = "half_open"

_FAILURE_CAUSE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_LOGGER = logging.getLogger(__name__)


def bounded_failure_cause(value: object) -> str:
    """Return a safe short token, rejecting anything unbounded or unexpected."""
    if isinstance(value, str) and _FAILURE_CAUSE.fullmatch(value):
        return value
    return CAUSE_UNCLASSIFIED


def http_failure_cause(status: object) -> str:
    """Label a non-2xx response by status code only, never by its body."""
    if isinstance(status, bool) or not isinstance(status, int):
        return CAUSE_INVALID_HTTP_STATUS
    if not 100 <= status <= 599:
        return CAUSE_INVALID_HTTP_STATUS
    return f"http_{status}"


def classify_failure_cause(error: BaseException) -> str:
    """Classify an OCR failure into a bounded cause label."""
    declared = getattr(error, "failure_cause", None)
    if isinstance(declared, str) and _FAILURE_CAUSE.fullmatch(declared):
        return declared
    return _classify_transport_error(error)


def _classify_transport_error(error: BaseException) -> str:
    exceptions = _requests_exceptions()
    if exceptions is not None:
        # Most specific first: ConnectTimeout subclasses both ConnectionError
        # and Timeout, and SSLError subclasses ConnectionError.
        for name, cause in (
            ("ConnectTimeout", CAUSE_CONNECT_TIMEOUT),
            ("ReadTimeout", CAUSE_READ_TIMEOUT),
            ("Timeout", CAUSE_REQUEST_TIMEOUT),
            ("SSLError", CAUSE_TLS_ERROR),
            ("ConnectionError", CAUSE_CONNECTION_ERROR),
            ("RequestException", CAUSE_REQUEST_ERROR),
        ):
            candidate = getattr(exceptions, name, None)
            if isinstance(candidate, type) and isinstance(error, candidate):
                return cause
    if isinstance(error, TimeoutError):
        return CAUSE_REQUEST_TIMEOUT
    if isinstance(error, OSError):
        return CAUSE_CONNECTION_ERROR
    return CAUSE_UNCLASSIFIED


def _requests_exceptions():
    try:
        from requests import exceptions
    except Exception:
        return None
    return exceptions


GENERIC_ERROR_CLASSES = frozenset({"OSError", "Exception", "BaseException", "RuntimeError", "Error"})


def _innermost_error_class(error: BaseException | None) -> str:
    """The most specific class name in the exception chain: safe to journal.

    ``requests.ConnectionError`` hides whether a name lookup failed, a socket
    was refused, or a peer reset the connection; the wrapped urllib3 error
    (``NameResolutionError``, ``NewConnectionError``, ``ProtocolError``) or
    the OS error beneath it (``ConnectionResetError``) says which. urllib3
    keeps the wrapped error in ``reason``; the walk follows that as well as
    normal chaining, and steps back from a bare ``OSError`` to the class that
    described it. Class names carry no host, path, or credential.
    """
    if error is None:
        return "unavailable"
    chain = [error]
    current = error
    for _ in range(8):
        inner = current.__cause__ or current.__context__
        if inner is None:
            reason = getattr(current, "reason", None)
            if isinstance(reason, BaseException):
                inner = reason
        if inner is None:
            candidates = [arg for arg in getattr(current, "args", ()) if isinstance(arg, BaseException)]
            inner = candidates[0] if candidates else None
        if inner is None or inner in chain:
            break
        chain.append(inner)
        current = inner
    for candidate in reversed(chain):
        if type(candidate).__name__ not in GENERIC_ERROR_CLASSES:
            return type(candidate).__name__
    return type(chain[-1]).__name__


def _log_failure(cause: str, error: BaseException | None = None) -> None:
    """Journal the bounded cause and the innermost error class only; never a
    body, path, or credential."""
    try:
        if error is None:
            _LOGGER.warning("gate_ocr stage=attempt_failed cause=%s", bounded_failure_cause(cause))
        else:
            _LOGGER.warning(
                "gate_ocr stage=attempt_failed cause=%s detail=%s",
                bounded_failure_cause(cause), _innermost_error_class(error),
            )
    except Exception:
        return


def _log_transport_failure(error: BaseException) -> None:
    """Journal a transport failure without ever masking the original error."""
    try:
        _log_failure(classify_failure_cause(error), error)
    except Exception:
        return


def _response_error(message: str, cause: str) -> "OcrResponseError":
    _log_failure(cause)
    return OcrResponseError(message, cause)


def _closed_client_error() -> RuntimeError:
    error = RuntimeError("OCR client is closed")
    error.failure_cause = CAUSE_CLIENT_CLOSED
    return error


class OcrResponseError(RuntimeError):
    """The OCR service returned a response that cannot be trusted."""

    def __init__(self, message: str, failure_cause: str = CAUSE_INVALID_RESPONSE) -> None:
        super().__init__(message)
        self.failure_cause = bounded_failure_cause(failure_cause)


class _RetryableFailure(Exception):
    """One bounded retry is worth a try: the API throttled the request or
    the connection failed before a response arrived."""

    def __init__(self, error: BaseException, cause: str, interval: float) -> None:
        super().__init__(cause)
        self.error = error
        self.cause = cause
        self.interval = interval


def _retry_after_seconds(response) -> float:
    """Honour a bounded Retry-After, defaulting to the throttle interval."""
    headers = getattr(response, "headers", None)
    value = None
    if isinstance(headers, Mapping):
        value = headers.get("Retry-After")
    try:
        seconds = float(value) if value is not None else MIN_REQUEST_INTERVAL_SECONDS
    except (TypeError, ValueError):
        seconds = MIN_REQUEST_INTERVAL_SECONDS
    if not isfinite(seconds):
        seconds = MIN_REQUEST_INTERVAL_SECONDS
    return min(max(seconds, MIN_REQUEST_INTERVAL_SECONDS), MAX_RETRY_AFTER_SECONDS)


def _read_timed_out(error: BaseException) -> bool:
    """Whether the request was sent in full and only the reply was lost.

    A failing classifier must never mask the original error, so it is simply
    treated as not billable -- the same bias the burn-down keeps everywhere:
    an undercount is recoverable, an overcount closes the gate early.
    """
    try:
        return classify_failure_cause(error) == CAUSE_READ_TIMEOUT
    except Exception:
        return False


def _spent(observation, state: dict):
    """Stamp a read with whether a cloud lookup was actually spent on it.

    One place decides it, and it is the dispatch site rather than the reader
    that answered: under ``GATE_LOCAL_OCR_CLOUD=always`` the on-device read is
    the answer and the request goes out regardless, so the gate opens locally
    and the allowance is charged anyway. Under ``fallback`` a confident local
    read returns before any ``session.post`` and nothing is charged. The quota
    burn-down counts this flag, never ``source``.
    """
    if not isinstance(observation, PlateObservation):
        return observation
    spent = bool(state.get("cloud_lookup"))
    if observation.cloud_lookup == spent:
        return observation
    return replace(observation, cloud_lookup=spent)


def _cloud_permitted(cloud_permit) -> bool:
    """No permit means a camera-origin frame, which may always be sent.

    A permit that raises, or answers anything but True, has answered no.
    """
    if cloud_permit is None:
        return True
    try:
        return cloud_permit() is True
    except Exception:
        return False


def _internet_reachable(internet_reachable) -> bool:
    """The network probe's answer, failing open towards the cloud.

    The opposite of :func:`_cloud_permitted`: no probe means the request goes;
    so does a probe that raises or answers anything but a definite False. It
    only ever removes a request the probe has fresh evidence would time out.
    """
    if internet_reachable is None:
        return True
    try:
        return internet_reachable() is not False
    except Exception:
        return True


def _unavailable_reason(internet_reachable) -> str:
    """Why the predicate answered False, as it names it, or ``internet_down``.

    The probe's own bound method has no opinion and gets the reason #183
    gave it. :class:`CloudAvailability` says which of its two halves refused,
    so the journal never blames the link for the breaker. Bounded to the same
    token shape as a failure cause, and never raising.
    """
    try:
        reason = getattr(internet_reachable, "reason", None)
    except Exception:
        return REASON_INTERNET_DOWN
    if isinstance(reason, str) and _FAILURE_CAUSE.fullmatch(reason):
        return reason
    return REASON_INTERNET_DOWN


class CloudBreaker:
    """A circuit breaker on the cloud plate reader, fed by this client alone.

    It counts one thing: consecutive requests that died on the link without
    any answer -- ``ConnectionError``, ``ConnectTimeout``, ``ReadTimeout``,
    classified exactly as the outbox classifies its own sends
    (:func:`outbox.is_connection_failure`). ``BREAKER_FAILURE_THRESHOLD`` of
    them inside ``BREAKER_FAILURE_WINDOW_SECONDS`` **open** it: no request is
    attempted for ``BREAKER_OPEN_SECONDS``. When that runs out it is
    **half-open**: exactly one real request is let through as a trial. Any
    answer from the cloud -- a plate, no plate, a 4xx, a 5xx -- means the link
    carried a request, and **closes** it with the count and the open time
    reset; a trial that dies on the link re-opens it for twice as long, up to
    ``BREAKER_OPEN_MAX_SECONDS``. The network probe noticing the link come
    back (``failed`` to ``ok``) closes it at once, whatever its timer says.

    It fails open towards the cloud, never towards the gate. An open breaker
    only removes a request that would have died the same way; the local
    reads, the match policy, the cooldown and the relay path are not consulted
    and not changed. Every reader wraps it so that a breaker that raises
    counts as closed, and a trial that is claimed but never resolved (the
    request was abandoned before it left) is released, not kept.

    Thread-safe. ``clock`` is the monotonic clock the timer runs on;
    ``wall_clock`` is only for reporting ``until`` to the heartbeat.
    """

    def __init__(self, *, clock=monotonic, wall_clock=None,
                 failure_threshold: int = BREAKER_FAILURE_THRESHOLD,
                 failure_window_seconds: float = BREAKER_FAILURE_WINDOW_SECONDS,
                 open_seconds: float = BREAKER_OPEN_SECONDS,
                 max_open_seconds: float = BREAKER_OPEN_MAX_SECONDS):
        if isinstance(failure_threshold, bool) or not isinstance(failure_threshold, int):
            raise ValueError("failure_threshold must be an integer")
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least one")
        for name, value in (("failure_window_seconds", failure_window_seconds),
                            ("open_seconds", open_seconds),
                            ("max_open_seconds", max_open_seconds)):
            if not isinstance(value, (int, float)) or not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive, finite number of seconds")
        if max_open_seconds < open_seconds:
            raise ValueError("max_open_seconds must be at least open_seconds")
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._threshold = failure_threshold
        self._window = float(failure_window_seconds)
        self._base_open_seconds = float(open_seconds)
        self._max_open_seconds = float(max_open_seconds)
        self._lock = Lock()
        self._state = BREAKER_CLOSED
        # Consecutive connection-level deaths inside the window; any answer
        # resets it.
        self._failures = 0
        self._last_failure_at: float | None = None
        # How long the next open lasts: the base, doubled per failed trial.
        self._open_seconds = self._base_open_seconds
        self._opened_at: float | None = None
        self._until: float | None = None
        self._opened_after = 0
        # Half-open: whether the one trial request is out.
        self._trial = False

    # -- what the pipeline asks -------------------------------------------

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def available(self) -> bool:
        """Whether a request could be admitted now. Read-only: claims nothing.

        The sweep asks it before a hand-over and the processor before queueing
        a burst for the cloud lane; only :meth:`admit` at the request site
        claims the half-open trial, so a frame that is routed and then not
        posted never uses the trial up.
        """
        with self._lock:
            if self._state == BREAKER_CLOSED:
                return True
            if self._state == BREAKER_OPEN:
                return self._until is not None and self._clock() >= self._until
            return not self._trial

    def admit(self) -> bool:
        """Whether *this* request may leave; claims the half-open trial if so.

        Every ``True`` from a half-open breaker must be answered with one of
        :meth:`record_outcome` or :meth:`abandon_trial`, or the trial stays
        claimed; the client does that in a ``finally``.
        """
        message = None
        with self._lock:
            if self._state == BREAKER_CLOSED:
                return True
            now = self._clock()
            if self._state == BREAKER_OPEN:
                if self._until is None or now < self._until:
                    return False
                self._state = BREAKER_HALF_OPEN
                self._trial = True
                message = self._transition_line(now)
            elif self._trial:
                return False
            else:
                self._trial = True
        if message is not None:
            _LOGGER.info(message)
        return True

    # -- what the client reports ------------------------------------------

    def record_outcome(self, error: BaseException | None = None) -> None:
        """The one ``session.post`` this client made came back, or died.

        ``None`` is a response of any status; an exception is classified with
        the outbox's rule. Anything that is not a connection failure -- an
        HTTP error, a malformed body, a bug in the caller -- says nothing
        about the link and only releases the trial.
        """
        if error is None:
            self._close("response")
            return
        try:
            died = is_connection_failure(error)
        except Exception:
            died = False
        if died:
            self._record_failure()
        else:
            self.abandon_trial()

    def abandon_trial(self) -> None:
        """Release a claimed trial that produced no outcome. No transition."""
        with self._lock:
            self._trial = False

    def internet_restored(self) -> None:
        """The network probe measured ``ok`` after ``failed``: the link is back."""
        self._close("internet_restored")

    def status(self) -> dict:
        """State for the heartbeat: ``state``, ``until`` (wall time, ISO 8601,
        only while open), the consecutive failures and the current open length."""
        with self._lock:
            state, until, failures = self._state, self._until, self._failures
            open_seconds = self._open_seconds
            now = self._clock()
        until_at = None
        if state == BREAKER_OPEN and until is not None:
            until_at = (
                self._wall_clock() + timedelta(seconds=max(0.0, until - now))
            ).isoformat()
        return {
            "state": state, "until": until_at, "failures": failures,
            "open_seconds": open_seconds,
        }

    # -- transitions ------------------------------------------------------

    def _record_failure(self) -> None:
        message = None
        with self._lock:
            now = self._clock()
            if self._last_failure_at is not None and now - self._last_failure_at > self._window:
                self._failures = 0
            self._failures += 1
            self._last_failure_at = now
            if self._state == BREAKER_HALF_OPEN:
                # The trial died: open again, for longer.
                self._trial = False
                self._open_seconds = min(self._open_seconds * 2.0, self._max_open_seconds)
                message = self._open(now)
            elif self._state == BREAKER_CLOSED and self._failures >= self._threshold:
                message = self._open(now)
        if message is not None:
            _LOGGER.info(message)

    def _open(self, now: float) -> str:
        self._state = BREAKER_OPEN
        self._opened_at = now
        self._until = now + self._open_seconds
        self._opened_after = self._failures
        return self._transition_line(now)

    def _close(self, reason: str) -> None:
        message = None
        with self._lock:
            now = self._clock()
            was = self._state
            if was != BREAKER_CLOSED:
                self._state = BREAKER_CLOSED
                message = self._transition_line(now, reason=reason)
            self._trial = False
            self._failures = 0
            self._last_failure_at = None
            self._open_seconds = self._base_open_seconds
            self._opened_at = None
            self._until = None
            self._opened_after = 0
        if message is not None:
            _LOGGER.info(message)

    def _transition_line(self, now: float, *, reason: str | None = None) -> str:
        """One ``gate_ocr stage=cloud_breaker`` line per transition, under the lock."""
        if self._state == BREAKER_OPEN:
            for_seconds = self._open_seconds
        elif self._opened_at is not None:
            for_seconds = max(0.0, now - self._opened_at)
        else:
            for_seconds = 0.0
        line = (
            f"gate_ocr stage=cloud_breaker state={self._state} "
            f"after={self._opened_after} failures for_s={for_seconds:.0f}"
        )
        return line if reason is None else f"{line} reason={reason}"


def _breaker_available(breaker) -> bool:
    """The breaker's read-only answer, failing open: unreadable means closed."""
    if breaker is None:
        return True
    try:
        return breaker.available() is not False
    except Exception:
        return True


class CloudAvailability:
    """``cloud_available()``: the probe's answer *and* the breaker's, as one predicate.

    This is what ``main`` hands the sweep and the processor in place of the
    probe's bare ``internet_reachable`` (#183): the same call, the same
    fail-open rule at each site, and now two reasons to answer no. Either
    half may be absent. ``reason`` names the half that refused, for the
    journal, and is None while the cloud may be asked.
    """

    def __init__(self, breaker: CloudBreaker | None, internet_reachable=None):
        self.breaker = breaker
        self.internet_reachable = internet_reachable

    def __call__(self) -> bool:
        return (
            _internet_reachable(self.internet_reachable)
            and _breaker_available(self.breaker)
        )

    @property
    def reason(self) -> str | None:
        if not _internet_reachable(self.internet_reachable):
            return REASON_INTERNET_DOWN
        if not _breaker_available(self.breaker):
            return REASON_CLOUD_UNREACHABLE
        return None


def _mark_post_started(state: dict) -> None:
    """Tell the caller that a request is about to go out on this attempt.

    A caller that abandons the read gets no return value and so no
    ``cloud_lookup``: from where it stands, a read that died in the throttle
    window, in the downscaled upload or in the on-device guard looks exactly
    like one whose reply was lost. This is the difference, announced from the
    only place that knows.

    It is the last thing before ``session.post`` deliberately: everything that
    can still stop a request from being sent -- the 1.05 s pacing window, the
    upload, the local guard, the abandonment checks -- is already behind us,
    and after this point the allowance may have been charged whatever the
    caller decides to do with its clock.

    Annotation only, and it never raises: the frame is not the metric's
    business.
    """
    callback = state.get("on_post_started")
    if callback is None or state.get("post_started"):
        return
    state["post_started"] = True
    try:
        callback()
    except Exception:
        _LOGGER.debug("ocr_post_start_callback_failed", exc_info=True)


def _retryable_transport_cause(error: BaseException) -> str | None:
    """Return the cause when the failure is worth one fresh-connection retry.
    A failing classifier must never mask the original error, so it is
    simply treated as not retryable."""
    try:
        cause = classify_failure_cause(error)
    except Exception:
        return None
    return cause if cause in RETRYABLE_TRANSPORT_CAUSES else None


def _corpus_plate(payload) -> str | None:
    """The plate the cloud read, straight off the response, or None."""
    if not isinstance(payload, Mapping):
        return None
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return None
    first = results[0]
    plate = first.get("plate") if isinstance(first, Mapping) else None
    return plate if isinstance(plate, str) and plate.strip() else None


def _corpus_local_plate(local) -> str | None:
    """The on-device read's plate, for frames the cloud never saw."""
    sidecar = getattr(local, "to_sidecar", None)
    if not callable(sidecar):
        return None
    try:
        fields = sidecar()
    except Exception:
        return None
    plate = fields.get("plate") if isinstance(fields, dict) else None
    return plate if isinstance(plate, str) and plate.strip() else None


# Retryable failures, counted by the minute they happened in and by bounded
# cause, since the last drain.
#
# `http_429` is the one that has nowhere else to go: the retry at the call
# site usually succeeds, so a throttled request currently leaves no trace in
# `event_telemetry` at all and only a `gate_ocr stage=retry` line in the
# journal. The metrics ring drains this after each burst; nothing else reads
# it, and a drain that never comes cannot grow past the ceiling below.
#
# The minute is stamped here rather than at drain time. A drain happens when a
# burst *finishes*, and a burst that was throttled is precisely the one that
# ran long, so attributing its 429s to the drain minute pushed them into the
# following minute -- and, four times in five, into the following five-minute
# bucket -- whenever a burst straddled the boundary. Stamping costs one
# `strftime` on a path that has just decided to sleep for a second.
_MAX_RETRY_COUNT = 10_000_000
#: Enough minutes to hold anything a drain could reasonably be behind by. The
#: ring drains after every burst; a map this size means a drain that never
#: comes still cannot grow without bound.
_MAX_RETRY_MINUTES = 180
_retry_counts: dict[str, dict[str, int]] = {}
_retry_counts_lock = Lock()


def _retry_minute() -> str:
    """The minute a retryable failure happened in, in the ring's own format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z")


def count_retryable_failure(cause: object) -> None:
    """Count one retryable OCR failure. Never raises: it is on the OCR path."""
    try:
        token = bounded_failure_cause(cause)
        minute = _retry_minute()
        with _retry_counts_lock:
            causes = _retry_counts.get(minute)
            if causes is None:
                if len(_retry_counts) >= _MAX_RETRY_MINUTES:
                    _retry_counts.pop(min(_retry_counts), None)
                causes = _retry_counts[minute] = {}
            current = causes.get(token, 0)
            if current < _MAX_RETRY_COUNT:
                causes[token] = current + 1
    except Exception:
        return


def drain_retry_counts() -> dict[str, dict[str, int]]:
    """Take and reset the counts accumulated since the previous drain.

    Keyed by minute, then by cause: ``{"2026-09-08T10:24:00Z": {"http_429": 2}}``.
    """
    with _retry_counts_lock:
        drained = {minute: dict(causes) for minute, causes in _retry_counts.items()}
        _retry_counts.clear()
    return drained


def _log_retry(cause: str, wait_seconds: float) -> None:
    try:
        _LOGGER.info(
            "gate_ocr stage=retry cause=%s wait_ms=%d",
            bounded_failure_cause(cause), max(0, round(wait_seconds * 1000)),
        )
    except Exception:
        return


class PlateRecognizerClient:
    def __init__(self, token: str, session=None, endpoint: str = DEFAULT_ENDPOINT,
                 timeout: tuple[int, int] = DEFAULT_TIMEOUT,
                 max_upload_width: int = 0, *, clock=monotonic, sleep=sleep,
                 plate_region: PlateRegion | None = None,
                 precropped_directory: Path | None = None,
                 corpus=None, local_recognizer=None, authorised=None,
                 match_policy=None, activity=NULL_GATE, direction=None,
                 cloud_breaker: "CloudBreaker | None" = None):
        self._token = token
        # The circuit breaker on this client's own requests (see
        # `CloudBreaker`). One per client, built here unless `main` hands in
        # the one it also gave the pipeline's predicate and the heartbeat.
        self._breaker = cloud_breaker if cloud_breaker is not None else CloudBreaker(clock=clock)
        self._session = session
        self._session_generation = 0
        self._session_lock = Lock()
        self._closed = False
        self._endpoint = endpoint
        self._timeout = timeout
        self._clock = clock
        self._sleep = sleep
        # Pacing state: the earliest moment the next request may start, and
        # when the pooled connection was last used.
        self._not_before: float | None = None
        self._session_used_at: float | None = None
        if plate_region is not None and not isinstance(plate_region, PlateRegion):
            raise ValueError("plate_region must be a PlateRegion")
        self._plate_region = plate_region
        self._precropped_directory = (
            Path(precropped_directory).resolve() if precropped_directory is not None else None
        )
        # Optional TrainingCorpus: keeps every uploaded frame and answer.
        self._corpus = corpus
        # Optional LocalRecognizer: reads the very same upload bytes on the
        # device. In shadow mode it only journals. In active mode a read that
        # clears the confidence gate and then satisfies the controller's own
        # decide_access answers for the frame, and the cloud request is either
        # skipped or left to label the frame off the decision path.
        self._local = local_recognizer
        # Callable returning the authorised plates, so the shared matching can
        # be applied to a local read exactly as it is applied to a cloud read.
        self._authorised = authorised
        # Optional callable returning the time-of-day matching policy. It is
        # the same provider the processor is given, so a local read is judged
        # under the band the processor is about to apply to the very same
        # frame; without it both sides fall back to the shipped `standard`
        # behaviour, which is what the controller does today.
        self._match_policy = match_policy
        # The backpressure gate. Reading a frame is the busiest the uplink
        # ever is for the gate itself, and the corpus must never be sharing it.
        self._activity = activity or NULL_GATE
        # Optional DirectionTracker: the boxes this client already has -- the
        # on-device detector's and the cloud read's -- pooled by the camera
        # alarm each trace is bound to, so a passage's box widths can be
        # fitted for direction. Shadow only, and free: no new model, no extra
        # decode, one list append per box.
        self._direction = direction
        if isinstance(max_upload_width, bool) or not isinstance(max_upload_width, int):
            raise ValueError("max_upload_width must be an integer")
        if max_upload_width and not MIN_UPLOAD_WIDTH <= max_upload_width <= MAX_UPLOAD_WIDTH:
            raise ValueError("max_upload_width is outside the safe range")
        self._max_upload_width = max_upload_width

    @property
    def cloud_breaker(self) -> "CloudBreaker":
        """The breaker this client feeds and obeys."""
        return self._breaker

    def local_pass(self, path: Path, *, trace_id: str | None = None,
                   budget: float | None = None) -> LocalPass:
        """Read this frame on the device, off the serial cloud OCR slot.

        The processor calls this *before* it queues for the slot, so a local
        grant lands the instant inference returns even while a cloud request
        for an older frame is still in flight. Only frames the local reader
        could not answer go on to hold the slot.

        Never raises: any failure here is simply "no local answer", and the
        frame falls through to the cloud exactly as it did before. In
        ``GATE_LOCAL_OCR_CLOUD=always`` this does nothing at all -- that mode
        deliberately keeps the cloud request on the decision path so every
        frame is labelled for the corpus, and splitting it here would change
        what it collects.
        """
        empty = LocalPass()
        local = self._local
        if local is None or not local.enabled or not local.config.active:
            return empty
        if local.config.cloud == CLOUD_ALWAYS:
            return empty
        try:
            with self._activity.activity("ocr"):
                return self._local_pass(path, trace_id, budget)
        except Exception:
            _LOGGER.warning("gate_ocr stage=local_pass_failed")
            return empty

    def _local_pass(self, path: Path, trace_id, budget) -> LocalPass:
        upload, geometry = self._open_upload(path)
        try:
            image = self._corpus_image(upload)
        finally:
            upload.close()
        if not image:
            # Without the bytes there is nothing to read locally, and nothing
            # worth carrying into the cloud request either.
            return LocalPass()
        state: dict = {
            "frame": None,
            "deadline": self._budget_deadline(budget),
            "upload_bytes": image,
            "geometry": geometry,
        }
        frame = self._local.begin(
            image, trace_id=trace_id, geometry=geometry,
            authorised=self._authorised, policy=self._match_policy,
        )
        state["frame"] = frame
        # Off the slot, the processor has already held the cloud request's
        # reserve out of ``budget`` (`GateProcessor._local_pass_deadline`), so
        # the whole of what is left is the local read's. Keeping the in-slot
        # reserve here as well left the pass with no wait at all whenever
        # under ~3 s of the decision remained, and a 172 ms read that landed a
        # moment later was never looked at again: `_local_decision` had
        # returned, and `_corroborations` is only consulted from
        # `decide_access`, which needs a cloud observation to be reached
        # (2026-09-09 10:10:59, 131D2696 read at 0.999, gate stayed shut).
        observation = self._local_decision(
            path, trace_id, frame, image, geometry, state, reserve=0.0,
        )
        return LocalPass(observation=observation, state=state)

    def adopt_local_read(self, path: Path, read, *, trace_id: str | None = None,
                         digest: str | None = None,
                         budget: float | None = None) -> LocalPass | None:
        """The local pass for a frame the sweep has *already* read. Or None.

        A sweep frame arrives with the on-device read that was taken of it
        (:class:`~gate_controller.local_sweep.SweepRead`). That read stands in
        for the inference :meth:`local_pass` would have run -- and for nothing
        else. It still has to clear ``GATE_LOCAL_OCR_MIN_CONFIDENCE`` and the
        controller's own :func:`decide_access` under the band and the plate
        list in force *now*, through the very same `_local_decision` a fresh
        read goes through; a read that does not decide leaves the frame to the
        cloud exactly as before. The processor then applies the match policy,
        the allow-list, the schedule, the cooldown and the relay path itself,
        once, as it does for every frame.

        Why not simply read it again: the pipeline's upload is the same band
        re-encoded at a different JPEG quality, and that alone moved one
        frame's weakest character from 0.877 to 0.723 (2026-09-20 19:16:14),
        across the bar, so an authorised driver was refused on a frame that
        had already been read correctly.

        ``None`` means "no read to adopt; take the ordinary local pass": the
        recogniser is off, in shadow, or in ``always`` mode, the read is not
        one this process produced, or -- the one that matters -- ``digest``
        (the pipeline's identity for the file) is not the digest of the frame
        the read was taken from. A read can only ever speak for its own
        pixels. Never raises.
        """
        local = self._local
        if local is None or not local.enabled or not local.config.active:
            return None
        if local.config.cloud == CLOUD_ALWAYS:
            return None
        try:
            recognition = getattr(read, "recognition", None)
            read_digest = getattr(read, "frame_digest", None)
            if recognition is None or not read_digest or not digest:
                return None
            if str(read_digest) != str(digest):
                _LOGGER.warning("gate_ocr stage=sweep_read_refused reason=digest_mismatch")
                return None
            frame = local.adopt(
                recognition, trace_id=trace_id,
                authorised=self._authorised, policy=self._match_policy,
            )
            if frame is NULL_FRAME:
                return None
            state: dict = {
                "frame": frame,
                "deadline": self._budget_deadline(budget),
                # Deliberately no "upload_bytes": if this frame goes on to the
                # cloud it is uploaded exactly as any other frame is.
            }
            with self._activity.activity("ocr"):
                observation = self._local_decision(
                    path, trace_id, frame, getattr(read, "image", None),
                    getattr(read, "geometry", None), state, reserve=0.0,
                )
        except Exception:
            _LOGGER.warning("gate_ocr stage=sweep_read_adopt_failed")
            return None
        _LOGGER.info(
            "gate_ocr stage=sweep_read_adopted trace_id=%s plate=%s score=%.3f decided=%s",
            trace_id or "-", getattr(recognition, "plate", None) or "-",
            float(getattr(recognition, "score", 0.0) or 0.0),
            "true" if observation is not None else "false",
        )
        return LocalPass(observation=observation, state=state)

    def recognise(self, path: Path, timeout: tuple[float, float] | None = None,
                  trace_id: str | None = None,
                  budget: float | None = None,
                  attempt: LocalPass | None = None,
                  on_post_started: Callable[[], None] | None = None,
                  cloud_permit: Callable[[], bool] | None = None,
                  internet_reachable: Callable[[], bool] | None = None) -> PlateObservation:
        """Read one frame.

        ``cloud_permit`` travels only with a frame of an *early-origin*
        passage -- a sweep the early trigger started ahead of the camera's
        alarm. It is asked immediately before the request would go out, and
        while it answers no, nothing is posted: the on-device answer stands,
        or "no plate" when there is none. See ``_recognise_once``.

        ``internet_reachable`` is the controller's network probe
        (``NetProbeWorker.internet_reachable``), asked at the same place and
        with the same outcome when it answers a definite False: the on-device
        answer stands, nothing is posted and nothing waits on a link the probe
        has fresh evidence is down. Absent, raising, or answering anything
        else, the request goes exactly as it always has.

        ``budget`` is the seconds of decision time left for this frame when
        the call starts. It bounds what the local guard may spend and re-sizes
        the socket timeouts around whatever it did spend; without it the
        request behaves exactly as it always has.

        ``attempt`` is a :class:`LocalPass` the caller already ran off the OCR
        slot. Its prepared upload and its local read are reused rather than
        redone, so the frame is decoded and inferred once per frame however
        the caller splits the work.

        ``on_post_started`` is called once, immediately before the request
        goes out, so a caller that later abandons this read can tell an
        attempt that may have been billed from one that never left the Pi.
        It changes nothing about the read itself.

        The whole read is held open on the activity gate, so a corpus upload
        defers before it starts and abandons if it is already running. The
        gate is a counter and a timestamp behind one lock; it adds nothing
        measurable to the frame.
        """
        with self._activity.activity("ocr"):
            return self._recognise(
                path, timeout, trace_id, budget, attempt, on_post_started,
                cloud_permit, internet_reachable,
            )

    def _recognise(self, path: Path, timeout, trace_id, budget=None,
                   attempt: LocalPass | None = None,
                   on_post_started=None, cloud_permit=None,
                   internet_reachable=None) -> PlateObservation:
        # The generation is captured once so a retry never outlives an
        # event the processor has already abandoned.
        with self._session_lock:
            if self._closed:
                raise _closed_client_error()
            generation = self._session_generation
        # The local read belongs to the frame, not to a network attempt: a
        # retry reuses the same handle instead of inferring twice.
        state: dict = (attempt.state if attempt is not None and attempt.state else None) or {
            "frame": None, "deadline": self._budget_deadline(budget),
        }
        # The dispatch callback belongs to this call rather than to the pass
        # prepared off the slot, so it is set on whichever state we ended up
        # with: a reused pass must still be able to report that it posted.
        state["on_post_started"] = on_post_started
        state["cloud_permit"] = cloud_permit
        state["internet_reachable"] = internet_reachable
        # A reused pass carries the deadline *it* was bounded by, which the
        # processor holds short of the decision's so the cloud keeps its
        # reserve. The request itself answers to the decision deadline, so
        # re-read it from this call's budget: sized to the pass's bound, which
        # a pass that used its budget has already passed, `_bounded_timeout`
        # hands the socket the (0.1, 0.1) floor and bills a certain
        # `read_timeout`.
        if budget is not None:
            state["deadline"] = self._budget_deadline(budget)
        retries = 0
        try:
            while True:
                try:
                    return _spent(self._recognise_once(
                        path, timeout, generation, trace_id, state,
                    ), state)
                except _RetryableFailure as failure:
                    # Counted before the retry budget is consulted: a second
                    # 429 in one event is still a throttled request, and
                    # counting only the retried ones would understate it.
                    count_retryable_failure(failure.cause)
                    if retries >= MAX_TRANSIENT_RETRIES:
                        raise failure.error
                    retries += 1
                    now = self._clock()
                    self._not_before = now + failure.interval
                    _log_retry(failure.cause, failure.interval)
                    if failure.cause in RETRYABLE_TRANSPORT_CAUSES:
                        self._recycle_session()
        except Exception:
            # In `always` mode the local read has already answered for this
            # frame; a cloud request that then fails costs a label, not the
            # decision.
            decided = state.get("local_observation")
            if decided is None:
                raise
            return _spent(decided, state)
        finally:
            # Whatever happened to the cloud request, the shadow line is
            # still owed once the local read lands.
            frame = state.get("frame")
            if frame is not None:
                frame.abandon_cloud()

    def prewarm(self) -> bool:
        """Open the TLS connection now, in the background, so the first OCR
        request of a vehicle reuses it.

        Called when the camera event arrives, ~0.5 s before the first frame
        is ready. Over a slow uplink the name lookup plus TCP and TLS
        handshakes cost 0.4 to 0.8 s, and a name lookup at that moment is
        exactly what a flapping link breaks. The probe carries no token, so
        it is never billed; a failure is ignored and the real request dials
        as before.
        """
        with self._session_lock:
            if self._closed:
                return False
            generation = self._session_generation
        self._recycle_if_idle()

        def warm():
            session = self._session_for_prewarm(generation)
            if session is None:
                return
            get = getattr(session, "get", None)
            if not callable(get):
                return
            try:
                response = get(
                    self._endpoint, headers={"User-Agent": "gate-controller/1"}, timeout=(2, 2),
                )
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            except Exception:
                return

        Thread(target=warm, name="gate-ocr-prewarm", daemon=True).start()
        return True

    def _session_for_prewarm(self, generation: int):
        """The pooled session, created if absent, unless the client moved on."""
        with self._session_lock:
            if self._closed or generation != self._session_generation:
                return None
            session = self._session
        if session is not None:
            return session
        try:
            created = self._create_session()
        except Exception:
            return None
        with self._session_lock:
            if self._closed or generation != self._session_generation:
                discard = True
            elif self._session is None:
                self._session = created
                discard = False
            else:
                discard = True
                session = self._session
        if discard:
            self._close_session(created)
            return session
        return created

    def _recycle_session(self) -> None:
        """Drop the pooled connection so the next request dials afresh."""
        with self._session_lock:
            session = self._session
            self._session = None
            self._session_used_at = None
        self._close_session(session)

    def _recycle_if_idle(self) -> None:
        with self._session_lock:
            used_at = self._session_used_at
            idle = (
                self._session is not None and used_at is not None
                and self._clock() - used_at > SESSION_IDLE_RECYCLE_SECONDS
            )
        if idle:
            self._recycle_session()

    def _pace(self, generation: int) -> None:
        """Wait out the API throttle window, then re-check abandonment."""
        while True:
            not_before = self._not_before
            wait = 0.0 if not_before is None else not_before - self._clock()
            if wait <= 0:
                return
            self._sleep(min(wait, MAX_RETRY_AFTER_SECONDS))
            with self._session_lock:
                if self._closed:
                    raise _closed_client_error()
                if generation != self._session_generation:
                    raise OcrResponseError(
                        "OCR request was abandoned", CAUSE_REQUEST_ABANDONED
                    )

    def _recognise_once(
        self, path: Path, timeout: tuple[float, float] | None, generation: int,
        trace_id: str | None = None, state: dict | None = None,
    ) -> PlateObservation:
        state = {} if state is None else state
        self._recycle_if_idle()
        with self._session_lock:
            if self._closed:
                raise _closed_client_error()
            if generation != self._session_generation:
                raise OcrResponseError(
                    "OCR request was abandoned", CAUSE_REQUEST_ABANDONED
                )
            session = self._session
        if session is None:
            created = self._create_session()
            discard_created = False
            error = None
            with self._session_lock:
                if self._closed:
                    discard_created = True
                    error = _closed_client_error()
                elif generation != self._session_generation:
                    discard_created = True
                    error = OcrResponseError(
                        "OCR request was abandoned", CAUSE_REQUEST_ABANDONED
                    )
                elif self._session is None:
                    self._session = created
                    session = created
                else:
                    discard_created = True
                    session = self._session
            if discard_created:
                self._close_session(created)
            if error is not None:
                raise error
        with self._session_lock:
            if self._closed:
                raise _closed_client_error()
            if generation != self._session_generation:
                raise OcrResponseError(
                    "OCR request was abandoned", CAUSE_REQUEST_ABANDONED
                )
        local_enabled = self._local is not None and self._local.enabled
        upload, geometry, corpus_image = self._upload_for(
            path, state, want_bytes=self._corpus is not None or local_enabled,
        )
        frame = state.get("frame") or NULL_FRAME
        if local_enabled and corpus_image and state.get("frame") is None:
            frame = self._local.begin(
                corpus_image, trace_id=trace_id, geometry=geometry,
                authorised=self._authorised, policy=self._match_policy,
            )
            state["frame"] = frame
            local_observation = self._local_decision(
                path, trace_id, frame, corpus_image, geometry, state,
            )
            if local_observation is not None:
                if self._local.config.cloud != CLOUD_ALWAYS:
                    upload.close()
                    # The guard may have waited; the burst may have been
                    # abandoned while it did. The local path owes the same
                    # invariant the cloud path keeps below - never answer for
                    # an event the processor has already given up on.
                    self._raise_if_abandoned(generation)
                    return local_observation
                # `always`: the local read is the answer, but the cloud
                # request still runs here, on this thread, so the frame is
                # labelled for the corpus. Keeping it on the decision path is
                # deliberate - a second thread would share this client's
                # pacing window and upload geometry with a gate-critical
                # request, and a stray 429 there is worse than the latency
                # this mode gives up.
                state["local_observation"] = local_observation
        # Preparing a downscaled upload, and any local read before it, can
        # outlast the decision deadline; never post a request the processor
        # has already abandoned.
        with self._session_lock:
            abandoned = self._closed or generation != self._session_generation
        if abandoned:
            upload.close()
            self._raise_if_abandoned(generation)
        # Whatever the local guard spent is gone from the decision budget, so
        # the request is sized for the budget it actually has rather than the
        # one the processor split before the guard ran.
        timeout = self._bounded_timeout(timeout, state.get("deadline"))
        if not _cloud_permitted(state.get("cloud_permit")):
            # The one place every request to the cloud reader leaves from. A
            # frame of a passage the early trigger started goes no further
            # until the camera has raised its own vehicle event for it: the
            # on-device answer stands if there is one, and nothing is sent.
            upload.close()
            _LOGGER.info("gate_ocr stage=cloud_refused reason=no_camera_event")
            decided = state.get("local_observation")
            if decided is not None:
                return decided
            return PlateObservation(
                plate=None, confidence=0.0, source="local", cloud_lookup=False,
            )
        if not _internet_reachable(state.get("internet_reachable")):
            # Same place, same outcome, a different reason: the controller's
            # own probe has fresh evidence the internet is down, so the
            # request would only time out. The on-device answer stands (in
            # `always` mode it was taken just above), and the pacing window,
            # the upload and the 6 s wait are all spent on nothing. The
            # predicate `main` hands down is the probe *and* the breaker, and
            # names which of them refused.
            upload.close()
            _LOGGER.info(
                "gate_ocr stage=cloud_skipped reason=%s",
                _unavailable_reason(state.get("internet_reachable")),
            )
            decided = state.get("local_observation")
            if decided is not None:
                return decided
            return PlateObservation(
                plate=None, confidence=0.0, source="local", cloud_lookup=False,
            )
        if not self._breaker_admits():
            # This client's own breaker: its last requests died on the link
            # without an answer, or the one half-open trial is already out.
            # Same outcome again; the request is the only thing removed.
            upload.close()
            _LOGGER.info("gate_ocr stage=cloud_skipped reason=%s", REASON_CLOUD_UNREACHABLE)
            decided = state.get("local_observation")
            if decided is not None:
                return decided
            return PlateObservation(
                plate=None, confidence=0.0, source="local", cloud_lookup=False,
            )
        try:
            self._pace(generation)
            # Past the throttle window, the upload and every abandonment
            # check: from here on the request really is going out.
            _mark_post_started(state)
            try:
                response = session.post(
                    self._endpoint,
                    data={"regions": "ie"},
                    files={"upload": (path.name, upload, "image/jpeg")},
                    headers={"Authorization": f"Token {self._token}"},
                    timeout=timeout or self._timeout,
                )
                # A response of any status means the link carried a request:
                # the breaker closes on it, before the trial is released
                # below, so no second trial can slip out in between.
                self._breaker_note(None)
            except Exception as error:
                # The breaker hears every death on the link, and nothing else
                # feeds it. Told before the classification below so that a
                # classifier that raises cannot starve it.
                self._breaker_note(error)
                # Classify and journal the transport failure, then let the
                # original exception propagate unchanged, after one retry on
                # a fresh connection when it never produced a response.
                if _read_timed_out(error):
                    # The body went out in full and the reply never came: the
                    # service may already have accepted and charged for it,
                    # which is the same reason this one is never retried.
                    state["cloud_lookup"] = True
                _log_transport_failure(error)
                if _retryable_transport_cause(error) is not None:
                    cause = _retryable_transport_cause(error)
                    raise _RetryableFailure(
                        error, cause, MIN_REQUEST_INTERVAL_SECONDS
                    ) from error
                raise
        finally:
            upload.close()
            # A trial admitted above but never resolved -- the pacing wait was
            # abandoned, or the post raised something that was not the link's
            # doing -- is released here, never left claimed.
            self._breaker_release()

        # A response came back, so the allowance was charged -- whatever the
        # status code says about what it was charged for, and whichever reader
        # ends up answering for this frame.
        state["cloud_lookup"] = True
        responded_at = self._clock()
        self._not_before = responded_at + MIN_REQUEST_INTERVAL_SECONDS
        with self._session_lock:
            if self._session is session:
                self._session_used_at = responded_at
        if response.status_code == RETRYABLE_STATUS:
            error = _response_error(
                f"OCR service returned HTTP {response.status_code}",
                http_failure_cause(response.status_code),
            )
            raise _RetryableFailure(
                error, error.failure_cause, _retry_after_seconds(response)
            )
        if not 200 <= response.status_code < 300:
            raise _response_error(
                f"OCR service returned HTTP {response.status_code}",
                http_failure_cause(response.status_code),
            )
        try:
            payload = response.json()
        except Exception as error:
            raise _response_error(
                "OCR service returned invalid JSON", CAUSE_INVALID_JSON
            ) from error
        if not isinstance(payload, Mapping):
            raise _response_error(
                "OCR service returned a non-object payload", CAUSE_INVALID_PAYLOAD
            )
        results = payload.get("results")
        if not isinstance(results, list):
            raise _response_error(
                "OCR service response has invalid results", CAUSE_INVALID_RESULTS
            )
        settled = frame.settled()
        self._record_corpus(
            corpus_image, payload, path, geometry, local=settled,
        )
        self.observe_direction(
            trace_id, payload=payload, local=settled, geometry=geometry,
        )
        if not results:
            frame.complete_cloud(None, 0.0, decided=False)
            return state.get("local_observation") or PlateObservation(
                plate=None, confidence=0.0,
            )
        first_result = results[0]
        if not isinstance(first_result, Mapping):
            raise _response_error(
                "OCR service response has invalid result", CAUSE_INVALID_RESULT_ENTRY
            )
        plate = first_result.get("plate")
        score = first_result.get("score")
        self._log_plate_box(first_result.get("box"), geometry)
        if not isinstance(plate, str) or not normalise_plate(plate):
            raise _response_error(
                "OCR service response has no usable plate", CAUSE_NO_USABLE_PLATE
            )
        if (isinstance(score, bool) or not isinstance(score, (int, float))
                or not isfinite(score) or not 0 <= score <= 1):
            raise _response_error(
                "OCR service response has invalid confidence", CAUSE_INVALID_CONFIDENCE
            )
        observation = PlateObservation(
            plate=normalise_plate(plate), confidence=float(score),
            make=_optional_string(first_result.get("vehicle", {}), "make"),
            colour=_optional_string(first_result.get("vehicle", {}), "color"),
        )
        decided = state.get("local_observation")
        frame.complete_cloud(
            observation.plate, observation.confidence, decided=decided is None,
        )
        return decided or observation

    def _breaker_admits(self) -> bool:
        """The breaker's answer at the request site; unreadable means yes."""
        try:
            return self._breaker.admit() is not False
        except Exception:
            _LOGGER.debug("gate_ocr stage=cloud_breaker outcome=unreadable", exc_info=True)
            return True

    def _breaker_note(self, error: BaseException | None) -> None:
        """Feed the breaker one outcome. Never raises: the frame is not its business."""
        try:
            self._breaker.record_outcome(error)
        except Exception:
            _LOGGER.debug("gate_ocr stage=cloud_breaker outcome=unreadable", exc_info=True)

    def _breaker_release(self) -> None:
        try:
            self._breaker.abandon_trial()
        except Exception:
            _LOGGER.debug("gate_ocr stage=cloud_breaker outcome=unreadable", exc_info=True)

    def _upload_for(self, path: Path, state: dict, *, want_bytes: bool):
        """The bytes to post, the geometry they map back through, and a copy.

        A :meth:`local_pass` that already prepared this frame hands its bytes
        over here, so the JPEG is decoded, cropped and re-encoded once per
        frame rather than once per stage. Without one this behaves exactly as
        it always did.
        """
        prepared = state.get("upload_bytes")
        if prepared:
            return BytesIO(prepared), state.get("geometry"), prepared
        upload, geometry = self._open_upload(path)
        image = self._corpus_image(upload) if want_bytes else None
        return upload, geometry, image

    def _budget_deadline(self, budget) -> float | None:
        """When the decision this request belongs to gives up, if it said."""
        if budget is None:
            return None
        try:
            seconds = float(budget)
        except (TypeError, ValueError):
            return None
        if not isfinite(seconds):
            return None
        return self._clock() + seconds

    def _raise_if_abandoned(self, generation: int) -> None:
        with self._session_lock:
            if self._closed:
                raise _closed_client_error()
            if generation != self._session_generation:
                raise OcrResponseError(
                    "OCR request was abandoned", CAUSE_REQUEST_ABANDONED
                )

    def _bounded_timeout(self, timeout, deadline):
        """Shrink the connect/read split to the budget that is left.

        The processor sizes the request from the budget it had *before* the
        call; a local read in between spends some of it. Only ever narrower
        than what the processor asked for, and never below the floor it uses.
        """
        if timeout is None or deadline is None:
            return timeout
        try:
            connect, read = (float(value) for value in timeout)
        except (TypeError, ValueError):
            return timeout
        if not (isfinite(connect) and isfinite(read)):
            return timeout
        remaining = deadline - self._clock()
        if not isfinite(remaining) or remaining >= connect + read:
            return timeout
        connect = min(connect, max(MIN_SOCKET_TIMEOUT_SECONDS, remaining / 2))
        read = min(read, max(MIN_SOCKET_TIMEOUT_SECONDS, remaining - connect))
        return (connect, read)

    def _local_decision(self, path: Path, trace_id, frame, image, geometry, state,
                        *, reserve: float | None = None):
        """The local read, when it may answer for this frame. Else None.

        Only ``GATE_LOCAL_OCR_MODE=active`` reaches past the first line. The
        read must clear ``GATE_LOCAL_OCR_MIN_CONFIDENCE`` and then satisfy
        the controller's own :func:`decide_access` -- exact or fuzzy, the
        very same function and thresholds the cloud read goes through, and on
        the strength of this frame's own plate. Anything else falls through
        to the cloud unchanged.

        The wait is bounded by the recogniser's own budget: the stuck-engine
        ceiling, the decision budget minus the cloud request's reserve, and
        what is left of this event's total local waiting. The guard runs on
        the ``gate-ocr-request`` worker holding the OCR slot, so time spent
        here is time the burst does not have.

        ``reserve`` overrides that cloud reserve for a caller whose budget was
        already reserved from; left unset, the recogniser's own default
        applies and this path is unchanged.
        """
        if not self._local.config.active:
            return None
        deadline = state.get("deadline")
        started = self._clock()
        remaining = None if deadline is None else deadline - started
        try:
            wait = (
                self._local.wait_seconds(trace_id, remaining) if reserve is None
                else self._local.wait_seconds(trace_id, remaining, reserve=reserve)
            )
            recognition = frame.result(wait)
        finally:
            self._local.record_event_wait(trace_id, self._clock() - started)
        # Kept on the pass so the processor can tell "nothing found" from
        # "found and refused" before it decides whether the cloud is worth
        # asking (`LocalPass.saw_no_plate`).
        state["recognition"] = recognition
        if not self._local.decides(frame, recognition):
            return None
        frame.decided_locally()
        if self._local.config.cloud != CLOUD_ALWAYS:
            frame.cloud_skipped()
            self._record_corpus(
                image, None, path, geometry, local=recognition, cloud="skipped",
            )
            # Only on the path that skips the cloud request. In `always` the
            # request still runs and settles this very read, and observing in
            # both places would count one frame twice.
            self.observe_direction(trace_id, local=recognition, geometry=geometry)
        return recognition.observation()

    def local_observations(self, trace_id: str | None) -> tuple:
        """This event's confident on-device reads, for the agreement rule.

        Keyed by trace id, so a read of one event can never corroborate
        another. An empty tuple leaves the processor's decision exactly as it
        would be with no local reader at all.
        """
        if self._local is None or not trace_id:
            return ()
        try:
            return tuple(self._local.observations(trace_id))
        except Exception:
            return ()

    def bind_direction(self, trace_id, passage) -> None:
        """Bind this trace to its camera alarm for the direction tracker."""
        tracker = self._direction
        if tracker is None:
            return
        try:
            tracker.bind(trace_id, passage)
        except Exception:
            return

    def observe_direction(self, trace_id, *, payload=None, local=None,
                          geometry=None) -> None:
        """Feed this frame's boxes to the direction tracker. Never raises.

        Three sources, all of them already computed for other reasons: the
        cloud read's vehicle box, the cloud read's plate box, and the
        on-device detector's plate box. Vehicle widths and plate widths are
        kept apart -- they are on different scales, and fitting them as one
        series would manufacture a slope out of a change of source.
        """
        tracker = self._direction
        if tracker is None or not trace_id:
            return
        try:
            first = None
            if isinstance(payload, Mapping):
                results = payload.get("results")
                if isinstance(results, list) and results:
                    candidate = results[0]
                    first = candidate if isinstance(candidate, Mapping) else None
            if first is not None:
                vehicle = first.get("vehicle")
                if isinstance(vehicle, Mapping):
                    tracker.observe(
                        trace_id, box=self._frame_box(vehicle.get("box"), geometry),
                        source=SOURCE_VEHICLE_BOX,
                    )
                tracker.observe(
                    trace_id, box=self._frame_box(first.get("box"), geometry),
                    source=SOURCE_PLATE_BOX,
                )
            local_box = getattr(local, "box", None)
            if local_box is not None:
                # Already fractions of the whole frame: `LocalRecognition.box`
                # is mapped through `box_to_frame` when the read is made.
                tracker.observe(
                    trace_id, box=local_box, source=SOURCE_LOCAL_PLATE_BOX,
                )
        except Exception:
            return

    def _frame_box(self, box, geometry):
        """A Plate Recognizer pixel box as fractions of the whole frame."""
        if not isinstance(box, Mapping):
            return None
        try:
            corners = tuple(
                float(box[key]) for key in ("xmin", "ymin", "xmax", "ymax")
            )
        except (KeyError, TypeError, ValueError):
            return None
        return box_to_frame(corners, geometry, self._plate_region)

    def note_direction_brightness(self, trace_id, brightness) -> None:
        """How light the frame was, for the estimator's night gate."""
        tracker = self._direction
        if tracker is None:
            return
        try:
            tracker.note_brightness(trace_id, brightness)
        except Exception:
            return

    def direction_estimate(self, trace_id: str | None):
        """This event's shadow direction verdict, or None without a tracker."""
        tracker = self._direction
        if tracker is None:
            return None
        try:
            return tracker.estimate(trace_id)
        except Exception:
            return None

    def forget_direction(self, trace_id: str | None) -> None:
        tracker = self._direction
        if tracker is None:
            return
        try:
            tracker.forget(trace_id)
        except Exception:
            return

    def local_ocr_summary(self, trace_id: str | None):
        """The compact per-event local block for the telemetry payload."""
        if self._local is None:
            return None
        try:
            return self._local.summary(trace_id)
        except Exception:
            return None

    def forget_local_ocr(self, trace_id: str | None) -> None:
        if self._local is None:
            return
        try:
            self._local.forget(trace_id)
        except Exception:
            return

    @staticmethod
    def _corpus_image(upload) -> bytes | None:
        """The exact bytes about to be uploaded, without consuming them."""
        try:
            data = upload.read()
            upload.seek(0)
            return data
        except Exception:
            return None

    def _record_corpus(self, image: bytes | None, payload, path: Path, geometry,
                       *, local=None, cloud: str = "requested") -> None:
        if self._corpus is None or not image:
            return
        extra = {
            "precropped": self._is_precropped(path),
            "cloud": cloud,
            "local_ocr": self._local.config.mode if self._local is not None else "off",
            # What the reader saw, not what the gate decided: the processor
            # has not run yet. A training set needs to know which it is, so
            # the name says `authorised`, and the cloud index says the same.
            "authorised": self._frame_authorised(payload, local),
        }
        try:
            self._corpus.record(
                image, payload=payload,
                source="local_recognizer" if payload is None else "plate_recognizer",
                geometry=geometry, extra=extra,
                local=local.to_sidecar() if local is not None else None,
            )
        except Exception:
            pass

    def _frame_authorised(self, payload, local) -> bool | None:
        """Whether this frame's own plate is on the authorised list.

        An exact membership test against the cached set, nothing more: the
        fuzzy policy belongs to the decision, and this is a label. Returns
        None when there is no plate to test or no list to test against, and
        never raises -- a corpus field is not worth a frame.
        """
        if self._authorised is None:
            return None
        try:
            plate = _corpus_plate(payload) or _corpus_local_plate(local)
            if plate is None:
                return None
            authorised = self._authorised()
            if not authorised:
                return None
            return normalise_plate(plate) in {
                normalise_plate(candidate) for candidate in authorised
            }
        except Exception:
            return None

    def _is_precropped(self, path: Path) -> bool:
        """Frames the keyframe decoder already cropped to the plate region."""
        if self._precropped_directory is None:
            return False
        try:
            return Path(path).resolve().is_relative_to(self._precropped_directory)
        except (OSError, ValueError):
            return False

    def _open_upload(self, path: Path):
        """Return ``(bytes to upload, geometry)``: the file itself, or a cropped, bounded copy.

        The plate region is cut out first at native resolution (unless the
        decoder already produced a region-only frame), then the result is
        downscaled only if it is still wider than the limit. The original
        file is never modified. Any decode problem falls back to uploading
        the file unchanged so OCR still runs, with a ``None`` geometry.

        The geometry is *returned*, never stashed on the client. Two threads
        prepare uploads for the same client now -- the local pass runs on the
        decision thread while a cloud request for an older frame is still in
        flight on the request thread -- and an instance attribute would let
        one frame's crop be read back as another's, putting the plate box in
        the wrong place in the journal and the corpus sidecar.
        """
        precropped = self._is_precropped(path)
        region = None if precropped else self._plate_region
        geometry = None
        try:
            with Image.open(path) as image:
                frame_width, frame_height = image.size
                if region is not None:
                    left, top, right, bottom = region.pixel_box(frame_width, frame_height)
                else:
                    left, top, right, bottom = 0, 0, frame_width, frame_height
                crop_width, crop_height = right - left, bottom - top
                target_width = (
                    self._max_upload_width
                    if self._max_upload_width and crop_width > self._max_upload_width
                    else crop_width
                )
                target_height = max(1, round(crop_height * target_width / crop_width))
                geometry = _UploadGeometry(
                    frame_width, frame_height, left, top, crop_width, crop_height,
                    target_width, target_height, precropped, region is not None,
                )
                if region is None and target_width == frame_width:
                    return path.open("rb"), geometry
                # draft() lets the JPEG decoder skip detail the resize would
                # discard. It scales the whole frame by a power of two, so the
                # crop box is rescaled to whatever size the decoder chose.
                image.draft("RGB", (
                    max(1, -(-frame_width * target_width // crop_width)),
                    max(1, -(-frame_height * target_height // crop_height)),
                ))
                decoded = image.convert("RGB")
                if region is not None:
                    factor = decoded.width / frame_width
                    decoded = decoded.crop(tuple(round(edge * factor) for edge in (left, top, right, bottom)))
                if decoded.width > target_width:
                    decoded.thumbnail((target_width, target_height), Image.LANCZOS)
                buffer = BytesIO()
                decoded.save(buffer, format="JPEG", quality=UPLOAD_JPEG_QUALITY)
        except (OSError, ValueError, Image.DecompressionBombError):
            _LOGGER.warning("gate_ocr upload_downscale=failed")
            return path.open("rb"), None
        buffer.seek(0)
        _LOGGER.info(
            "gate_ocr upload_downscale=applied source_width=%d upload_width=%d upload_bytes=%d crop=%s",
            frame_width, decoded.width, buffer.getbuffer().nbytes,
            f"{left},{top},{right},{bottom}" if region is not None else "none",
        )
        return buffer, geometry

    def _log_plate_box(self, box, geometry) -> None:
        """Journal where the plate sat, as fractions of the whole camera frame.

        Boxes accumulate in the journal so GATE_PLATE_REGION can be set, and
        later tightened, from where plates were actually read.
        """
        if geometry is None or not isinstance(box, Mapping):
            return
        try:
            xmin, ymin, xmax, ymax = (float(box[key]) for key in ("xmin", "ymin", "xmax", "ymax"))
        except (KeyError, TypeError, ValueError):
            return
        if geometry.upload_width <= 0 or geometry.upload_height <= 0 or xmax <= xmin or ymax <= ymin:
            return
        x = (geometry.crop_left + xmin / geometry.upload_width * geometry.crop_width) / geometry.frame_width
        y = (geometry.crop_top + ymin / geometry.upload_height * geometry.crop_height) / geometry.frame_height
        width = (xmax - xmin) / geometry.upload_width * geometry.crop_width / geometry.frame_width
        height = (ymax - ymin) / geometry.upload_height * geometry.crop_height / geometry.frame_height
        frame = "full"
        if geometry.precropped and self._plate_region is not None:
            x, y, width, height = self._plate_region.to_frame((x, y, width, height))
            frame = "region"
        elif geometry.cropped:
            frame = "cropped"
        _LOGGER.info(
            "gate_ocr plate_box=%.3f,%.3f,%.3f,%.3f frame=%s", x, y, width, height, frame,
        )

    def abandon_in_flight(self) -> bool:
        """Detach a timed-out request so later work receives a fresh session."""
        with self._session_lock:
            session = self._session
            self._session_generation += 1
            self._session = None
        self._close_session(session)
        return False

    def close(self) -> None:
        with self._session_lock:
            if self._closed:
                return
            self._closed = True
            self._session_generation += 1
            session = self._session
            self._session = None
        self._close_session(session)
        if self._local is not None:
            try:
                self._local.close()
            except Exception:
                pass

    @staticmethod
    def _close_session(session) -> None:
        close = getattr(session, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    @staticmethod
    def _create_session():
        try:
            import requests
        except ImportError as error:
            raise RuntimeError("requests is required for the default OCR client") from error
        return requests.Session()


def _optional_string(value, key: str) -> str | None:
    if not isinstance(value, Mapping):
        return None
    result = value.get(key)
    return result if isinstance(result, str) else None
