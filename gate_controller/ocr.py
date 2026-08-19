from collections.abc import Mapping
from math import isfinite
from pathlib import Path
from threading import Lock

from .matching import normalise_plate
from .models import PlateObservation


DEFAULT_ENDPOINT = "https://api.platerecognizer.com/v1/plate-reader/"
DEFAULT_TIMEOUT = (1, 2)


class OcrResponseError(RuntimeError):
    """The OCR service returned a response that cannot be trusted."""


class PlateRecognizerClient:
    def __init__(self, token: str, session=None, endpoint: str = DEFAULT_ENDPOINT,
                 timeout: tuple[int, int] = DEFAULT_TIMEOUT):
        self._token = token
        self._session = session
        self._session_generation = 0
        self._session_lock = Lock()
        self._endpoint = endpoint
        self._timeout = timeout

    def recognise(self, path: Path, timeout: tuple[float, float] | None = None) -> PlateObservation:
        with self._session_lock:
            generation = self._session_generation
            session = self._session
        if session is None:
            created = self._create_session()
            with self._session_lock:
                if generation == self._session_generation:
                    if self._session is None:
                        self._session = created
                    session = self._session
                else:
                    session = created
        with path.open("rb") as image:
            response = session.post(
                self._endpoint,
                data={"regions": "ie"},
                files={"upload": (path.name, image, "image/jpeg")},
                headers={"Authorization": f"Token {self._token}"},
                timeout=timeout or self._timeout,
            )

        if not 200 <= response.status_code < 300:
            raise OcrResponseError(f"OCR service returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except Exception as error:
            raise OcrResponseError("OCR service returned invalid JSON") from error
        if not isinstance(payload, Mapping):
            raise OcrResponseError("OCR service returned a non-object payload")
        results = payload.get("results")
        if not isinstance(results, list):
            raise OcrResponseError("OCR service response has invalid results")
        if not results:
            return PlateObservation(plate=None, confidence=0.0)
        first_result = results[0]
        if not isinstance(first_result, Mapping):
            raise OcrResponseError("OCR service response has invalid result")
        plate = first_result.get("plate")
        score = first_result.get("score")
        if not isinstance(plate, str) or not normalise_plate(plate):
            raise OcrResponseError("OCR service response has no usable plate")
        if (isinstance(score, bool) or not isinstance(score, (int, float))
                or not isfinite(score) or not 0 <= score <= 1):
            raise OcrResponseError("OCR service response has invalid confidence")
        return PlateObservation(
            plate=normalise_plate(plate), confidence=float(score),
            make=_optional_string(first_result.get("vehicle", {}), "make"),
            colour=_optional_string(first_result.get("vehicle", {}), "color"),
        )

    def abandon_in_flight(self) -> None:
        """Detach a timed-out request so later work receives a fresh session."""
        with self._session_lock:
            self._session_generation += 1
            self._session = None

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
