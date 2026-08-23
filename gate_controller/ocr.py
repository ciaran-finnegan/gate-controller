import logging
import re
from collections.abc import Mapping
from math import isfinite
from pathlib import Path
from threading import Lock

from .matching import normalise_plate
from .models import PlateObservation


DEFAULT_ENDPOINT = "https://api.platerecognizer.com/v1/plate-reader/"
DEFAULT_TIMEOUT = (1, 2)

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


class PlateRecognizerClient:
    def __init__(self, token: str, session=None, endpoint: str = DEFAULT_ENDPOINT,
                 timeout: tuple[int, int] = DEFAULT_TIMEOUT):
        self._token = token
        self._session = session
        self._session_generation = 0
        self._session_lock = Lock()
        self._closed = False
        self._endpoint = endpoint
        self._timeout = timeout

    def recognise(self, path: Path, timeout: tuple[float, float] | None = None) -> PlateObservation:
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
        with path.open("rb") as image:
            try:
                response = session.post(
                    self._endpoint,
                    data={"regions": "ie"},
                    files={"upload": (path.name, image, "image/jpeg")},
                    headers={"Authorization": f"Token {self._token}"},
                    timeout=timeout or self._timeout,
                )
            except Exception as error:
                # Classify and journal the transport failure, then let the
                # original exception propagate unchanged.
                _log_transport_failure(error)
                raise

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
