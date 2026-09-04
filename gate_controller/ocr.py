import logging
import re
from collections.abc import Mapping
from io import BytesIO
from math import isfinite
from pathlib import Path
from threading import Lock
from time import monotonic, sleep

from PIL import Image

from .matching import normalise_plate
from .models import PlateObservation


DEFAULT_ENDPOINT = "https://api.platerecognizer.com/v1/plate-reader/"
DEFAULT_TIMEOUT = (1, 2)
MIN_UPLOAD_WIDTH = 640
MAX_UPLOAD_WIDTH = 3840
UPLOAD_JPEG_QUALITY = 90

# Plate Recognizer's cloud API throttles each account to one request per
# second, counted from when a request finishes arriving, and answers a faster
# follow-up with HTTP 429 ("Expected available in 1 second"). A burst of three
# frames therefore lost its second request every time. Requests are paced
# process-wide from the previous response, and a throttled or connection-level
# failure is retried once. The API also closes idle keep-alive connections;
# posting on one fails instantly, so an idle session is recycled first.
MIN_REQUEST_INTERVAL_SECONDS = 1.05
SESSION_IDLE_RECYCLE_SECONDS = 20.0
MAX_RETRY_AFTER_SECONDS = 2.0
MAX_TRANSIENT_RETRIES = 1
RETRYABLE_STATUS = 429
RETRYABLE_TRANSPORT_CAUSES = frozenset({"connection_error", "tls_error"})

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


def _log_failure(cause: str) -> None:
    """Journal the bounded cause only; never a body, path, or credential."""
    try:
        _LOGGER.warning(
            "gate_ocr stage=attempt_failed cause=%s", bounded_failure_cause(cause)
        )
    except Exception:
        return


def _log_transport_failure(error: BaseException) -> None:
    """Journal a transport failure without ever masking the original error."""
    try:
        _log_failure(classify_failure_cause(error))
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


def _retryable_transport_cause(error: BaseException) -> str | None:
    """Return the cause when the failure is worth one fresh-connection retry.
    A failing classifier must never mask the original error, so it is
    simply treated as not retryable."""
    try:
        cause = classify_failure_cause(error)
    except Exception:
        return None
    return cause if cause in RETRYABLE_TRANSPORT_CAUSES else None


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
                 max_upload_width: int = 0, *, clock=monotonic, sleep=sleep):
        self._token = token
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
        if isinstance(max_upload_width, bool) or not isinstance(max_upload_width, int):
            raise ValueError("max_upload_width must be an integer")
        if max_upload_width and not MIN_UPLOAD_WIDTH <= max_upload_width <= MAX_UPLOAD_WIDTH:
            raise ValueError("max_upload_width is outside the safe range")
        self._max_upload_width = max_upload_width

    def recognise(self, path: Path, timeout: tuple[float, float] | None = None) -> PlateObservation:
        retries = 0
        while True:
            try:
                return self._recognise_once(path, timeout)
            except _RetryableFailure as failure:
                if retries >= MAX_TRANSIENT_RETRIES:
                    raise failure.error
                retries += 1
                now = self._clock()
                self._not_before = now + failure.interval
                _log_retry(failure.cause, failure.interval)
                if failure.cause in RETRYABLE_TRANSPORT_CAUSES:
                    self._recycle_session()

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

    def _recognise_once(self, path: Path, timeout: tuple[float, float] | None) -> PlateObservation:
        self._recycle_if_idle()
        with self._session_lock:
            if self._closed:
                raise _closed_client_error()
            generation = self._session_generation
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
        upload = self._open_upload(path)
        # Preparing a downscaled upload can outlast the decision deadline;
        # never post a request the processor has already abandoned.
        with self._session_lock:
            abandoned = self._closed or generation != self._session_generation
            closed = self._closed
        if abandoned:
            upload.close()
            if closed:
                raise _closed_client_error()
            raise OcrResponseError("OCR request was abandoned", CAUSE_REQUEST_ABANDONED)
        try:
            self._pace(generation)
            try:
                response = session.post(
                    self._endpoint,
                    data={"regions": "ie"},
                    files={"upload": (path.name, upload, "image/jpeg")},
                    headers={"Authorization": f"Token {self._token}"},
                    timeout=timeout or self._timeout,
                )
            except Exception as error:
                # Classify and journal the transport failure, then let the
                # original exception propagate unchanged, after one retry on
                # a fresh connection when it never produced a response.
                _log_transport_failure(error)
                if _retryable_transport_cause(error) is not None:
                    cause = _retryable_transport_cause(error)
                    raise _RetryableFailure(
                        error, cause, MIN_REQUEST_INTERVAL_SECONDS
                    ) from error
                raise
        finally:
            upload.close()

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
        if not results:
            return PlateObservation(plate=None, confidence=0.0)
        first_result = results[0]
        if not isinstance(first_result, Mapping):
            raise _response_error(
                "OCR service response has invalid result", CAUSE_INVALID_RESULT_ENTRY
            )
        plate = first_result.get("plate")
        score = first_result.get("score")
        if not isinstance(plate, str) or not normalise_plate(plate):
            raise _response_error(
                "OCR service response has no usable plate", CAUSE_NO_USABLE_PLATE
            )
        if (isinstance(score, bool) or not isinstance(score, (int, float))
                or not isfinite(score) or not 0 <= score <= 1):
            raise _response_error(
                "OCR service response has invalid confidence", CAUSE_INVALID_CONFIDENCE
            )
        return PlateObservation(
            plate=normalise_plate(plate), confidence=float(score),
            make=_optional_string(first_result.get("vehicle", {}), "make"),
            colour=_optional_string(first_result.get("vehicle", {}), "color"),
        )

    def _open_upload(self, path: Path):
        """Return the bytes to upload: the file itself, or a bounded downscale.

        Downscaling only applies when configured and the frame is wider than
        the limit. The original file is never modified. Any decode problem
        falls back to uploading the file unchanged so OCR still runs.
        """
        if not self._max_upload_width:
            return path.open("rb")
        try:
            with Image.open(path) as image:
                width, height = image.size
                if width <= self._max_upload_width:
                    return path.open("rb")
                target_height = max(1, round(height * self._max_upload_width / width))
                # draft() lets the JPEG decoder skip detail the resize would
                # discard, which keeps the Pi-side cost well below the upload
                # time it saves.
                image.draft("RGB", (self._max_upload_width, target_height))
                resized = image.convert("RGB")
                resized.thumbnail((self._max_upload_width, target_height), Image.LANCZOS)
                buffer = BytesIO()
                resized.save(buffer, format="JPEG", quality=UPLOAD_JPEG_QUALITY)
        except (OSError, ValueError, Image.DecompressionBombError):
            _LOGGER.warning("gate_ocr upload_downscale=failed")
            return path.open("rb")
        buffer.seek(0)
        _LOGGER.info(
            "gate_ocr upload_downscale=applied source_width=%d upload_width=%d upload_bytes=%d",
            width, resized.width, buffer.getbuffer().nbytes,
        )
        return buffer

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
