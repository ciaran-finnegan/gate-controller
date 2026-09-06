import logging
from dataclasses import dataclass
import re
from collections.abc import Mapping
from io import BytesIO
from math import isfinite
from pathlib import Path
from threading import Lock, Thread
from time import monotonic, sleep

from PIL import Image

from .matching import normalise_plate
from .plate_region import PlateRegion
from .models import PlateObservation


DEFAULT_ENDPOINT = "https://api.platerecognizer.com/v1/plate-reader/"
DEFAULT_TIMEOUT = (1, 2)
MIN_UPLOAD_WIDTH = 640
MAX_UPLOAD_WIDTH = 3840
UPLOAD_JPEG_QUALITY = 85


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
                 max_upload_width: int = 0, *, clock=monotonic, sleep=sleep,
                 plate_region: PlateRegion | None = None,
                 precropped_directory: Path | None = None):
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
        if plate_region is not None and not isinstance(plate_region, PlateRegion):
            raise ValueError("plate_region must be a PlateRegion")
        self._plate_region = plate_region
        self._precropped_directory = (
            Path(precropped_directory).resolve() if precropped_directory is not None else None
        )
        self._upload_geometry: _UploadGeometry | None = None
        if isinstance(max_upload_width, bool) or not isinstance(max_upload_width, int):
            raise ValueError("max_upload_width must be an integer")
        if max_upload_width and not MIN_UPLOAD_WIDTH <= max_upload_width <= MAX_UPLOAD_WIDTH:
            raise ValueError("max_upload_width is outside the safe range")
        self._max_upload_width = max_upload_width

    def recognise(self, path: Path, timeout: tuple[float, float] | None = None) -> PlateObservation:
        # The generation is captured once so a retry never outlives an
        # event the processor has already abandoned.
        with self._session_lock:
            if self._closed:
                raise _closed_client_error()
            generation = self._session_generation
        retries = 0
        while True:
            try:
                return self._recognise_once(path, timeout, generation)
            except _RetryableFailure as failure:
                if retries >= MAX_TRANSIENT_RETRIES:
                    raise failure.error
                retries += 1
                now = self._clock()
                self._not_before = now + failure.interval
                _log_retry(failure.cause, failure.interval)
                if failure.cause in RETRYABLE_TRANSPORT_CAUSES:
                    self._recycle_session()

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
    ) -> PlateObservation:
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
        self._log_plate_box(first_result.get("box"))
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

    def _is_precropped(self, path: Path) -> bool:
        """Frames the keyframe decoder already cropped to the plate region."""
        if self._precropped_directory is None:
            return False
        try:
            return Path(path).resolve().is_relative_to(self._precropped_directory)
        except (OSError, ValueError):
            return False

    def _open_upload(self, path: Path):
        """Return the bytes to upload: the file itself, or a cropped, bounded copy.

        The plate region is cut out first at native resolution (unless the
        decoder already produced a region-only frame), then the result is
        downscaled only if it is still wider than the limit. The original
        file is never modified. Any decode problem falls back to uploading
        the file unchanged so OCR still runs.
        """
        precropped = self._is_precropped(path)
        region = None if precropped else self._plate_region
        self._upload_geometry = None
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
                self._upload_geometry = _UploadGeometry(
                    frame_width, frame_height, left, top, crop_width, crop_height,
                    target_width, target_height, precropped, region is not None,
                )
                if region is None and target_width == frame_width:
                    return path.open("rb")
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
            self._upload_geometry = None
            return path.open("rb")
        buffer.seek(0)
        _LOGGER.info(
            "gate_ocr upload_downscale=applied source_width=%d upload_width=%d upload_bytes=%d crop=%s",
            frame_width, decoded.width, buffer.getbuffer().nbytes,
            f"{left},{top},{right},{bottom}" if region is not None else "none",
        )
        return buffer

    def _log_plate_box(self, box) -> None:
        """Journal where the plate sat, as fractions of the whole camera frame.

        Boxes accumulate in the journal so GATE_PLATE_REGION can be set, and
        later tightened, from where plates were actually read.
        """
        geometry = self._upload_geometry
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
