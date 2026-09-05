import io
import tempfile
import unittest
from math import inf, nan
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

import gate_controller.ocr as ocr_module
from gate_controller.ocr import (
    OcrResponseError,
    PlateRecognizerClient,
    bounded_failure_cause,
    classify_failure_cause,
    http_failure_cause,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, json_error=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error
        self.text = text

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._payload


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []
        self.closed = False

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return self.response

    def close(self):
        self.closed = True


class OcrClientTests(unittest.TestCase):
    def setUp(self):
        self.image = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        self.image.write(b"test image")
        self.image.close()
        self.path = Path(self.image.name)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_propagates_request_timeout(self):
        session = FakeSession(error=TimeoutError("request timed out"))
        client = PlateRecognizerClient("token", session=session)

        with self.assertRaisesRegex(TimeoutError, "request timed out"):
            client.recognise(self.path)

    def test_rejects_non_success_http_response(self):
        client = PlateRecognizerClient(
            "token", session=FakeSession(response=FakeResponse(status_code=503))
        )

        with self.assertRaisesRegex(OcrResponseError, "503"):
            client.recognise(self.path)

    def test_rejects_malformed_response_payload(self):
        client = PlateRecognizerClient(
            "token", session=FakeSession(response=FakeResponse(payload={"results": {}}))
        )

        with self.assertRaises(OcrResponseError):
            client.recognise(self.path)

    def test_returns_an_unknown_observation_for_an_empty_result_set(self):
        client = PlateRecognizerClient(
            "token", session=FakeSession(response=FakeResponse(payload={"results": []}))
        )

        observation = client.recognise(self.path)

        self.assertIsNone(observation.plate)
        self.assertEqual(observation.confidence, 0.0)

    def test_extracts_plate_and_confidence_from_the_first_result(self):
        session = FakeSession(
            response=FakeResponse(
                payload={"results": [{"plate": "12D 3456", "score": 0.93}]}
            )
        )
        client = PlateRecognizerClient("token", session=session)

        observation = client.recognise(self.path)

        self.assertEqual(observation.plate, "12D3456")
        self.assertEqual(observation.confidence, 0.93)
        self.assertEqual(session.calls[0][1]["timeout"], (1, 2))

    def test_reuses_the_default_http_session_across_recognition_calls(self):
        sessions = []

        def create_session():
            session = FakeSession(
                response=FakeResponse(
                    payload={"results": [{"plate": "12D3456", "score": 0.93}]}
                )
            )
            sessions.append(session)
            return session

        with patch.object(PlateRecognizerClient, "_create_session", side_effect=create_session):
            client = PlateRecognizerClient("token")
            client.recognise(self.path)
            client.recognise(self.path)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(sessions[0].calls), 2)

    def test_abandoning_a_timed_out_request_forces_a_fresh_http_session(self):
        original = FakeSession(
            response=FakeResponse(payload={"results": [{"plate": "12D3456", "score": 0.93}]})
        )
        replacement = FakeSession(
            response=FakeResponse(payload={"results": [{"plate": "12D3456", "score": 0.93}]})
        )
        client = PlateRecognizerClient("token", session=original)

        cancelled = client.abandon_in_flight()
        with patch.object(client, "_create_session", return_value=replacement):
            client.recognise(self.path)

        self.assertEqual(original.calls, [])
        self.assertTrue(original.closed)
        self.assertFalse(cancelled)
        self.assertEqual(len(replacement.calls), 1)

    def test_close_during_session_creation_never_posts_and_closes_late_session(self):
        creating = Event()
        release = Event()
        late = FakeSession(
            response=FakeResponse(payload={"results": [{"plate": "12D3456", "score": 0.93}]})
        )
        errors = []
        client = PlateRecognizerClient("token")

        def create_session():
            creating.set()
            release.wait(0.5)
            return late

        def recognise():
            try:
                client.recognise(self.path)
            except Exception as error:
                errors.append(error)

        with patch.object(client, "_create_session", side_effect=create_session):
            worker = Thread(target=recognise, daemon=True)
            worker.start()
            self.assertTrue(creating.wait(0.5))
            client.close()
            release.set()
            worker.join(0.5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(late.calls, [])
        self.assertTrue(late.closed)
        self.assertEqual(len(errors), 1)

    def test_closed_client_never_creates_a_new_session(self):
        client = PlateRecognizerClient("token", session=FakeSession())
        client.close()

        with patch.object(client, "_create_session") as create_session:
            with self.assertRaisesRegex(RuntimeError, "closed"):
                client.recognise(self.path)

        create_session.assert_not_called()

    def test_late_session_creation_cannot_replace_the_fresh_generation(self):
        creating_first = Event()
        release_first = Event()
        obsolete = FakeSession(
            response=FakeResponse(payload={"results": [{"plate": "12D3456", "score": 0.93}]})
        )
        replacement = FakeSession(
            response=FakeResponse(payload={"results": [{"plate": "12D3456", "score": 0.93}]})
        )
        sessions = iter((obsolete, replacement))
        errors = []

        def create_session():
            session = next(sessions)
            if session is obsolete:
                creating_first.set()
                release_first.wait(0.5)
            return session

        def recognise_first():
            try:
                client.recognise(self.path)
            except Exception as error:
                errors.append(error)

        client = PlateRecognizerClient("token")
        with patch.object(client, "_create_session", side_effect=create_session):
            first = Thread(target=recognise_first, daemon=True)
            first.start()
            self.assertTrue(creating_first.wait(0.5))
            client.abandon_in_flight()
            client.recognise(self.path)
            release_first.set()
            first.join(0.5)

        self.assertFalse(first.is_alive())
        self.assertIs(client._session, replacement)
        self.assertEqual(obsolete.calls, [])
        self.assertTrue(obsolete.closed)
        self.assertEqual(len(replacement.calls), 1)
        self.assertEqual(len(errors), 1)

    def test_rejects_confidence_outside_the_closed_zero_to_one_range(self):
        for score in (-0.01, 1.01, nan, inf, -inf):
            with self.subTest(score=score):
                client = PlateRecognizerClient(
                    "token",
                    session=FakeSession(response=FakeResponse(
                        payload={"results": [{"plate": "12D3456", "score": score}]}
                    )),
                )

                with self.assertRaisesRegex(OcrResponseError, "confidence"):
                    client.recognise(self.path)


class OcrUploadDownscaleTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "frame.jpg"
        Image.new("RGB", (3840, 2160), color="gray").save(self.path, format="JPEG")

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def recording_session():
        # The client closes the upload after posting, so capture its bytes
        # while the request is in flight.
        session = FakeSession(response=FakeResponse(payload={"results": []}))
        session.uploaded = []
        original_post = session.post

        def post(*args, **kwargs):
            session.uploaded.append(kwargs["files"]["upload"][1].read())
            return original_post(*args, **kwargs)

        session.post = post
        return session

    def test_uploads_the_file_unchanged_by_default(self):
        session = FakeSession(response=FakeResponse(payload={"results": []}))
        client = PlateRecognizerClient("token", session=session)

        client.recognise(self.path)

        (_args, kwargs), = session.calls
        self.assertEqual(kwargs["files"]["upload"][0], "frame.jpg")
        self.assertTrue(kwargs["files"]["upload"][1].closed)

    def test_downscales_wide_frames_before_upload_and_leaves_the_file_alone(self):
        from PIL import Image
        session = self.recording_session()
        client = PlateRecognizerClient("token", session=session, max_upload_width=1920)
        original = self.path.read_bytes()

        client.recognise(self.path)

        (_args, kwargs), = session.calls
        upload = kwargs["files"]["upload"][1]
        self.assertTrue(upload.closed)
        with Image.open(self.path) as image:
            self.assertEqual(image.size, (3840, 2160))
        self.assertEqual(self.path.read_bytes(), original)
        uploaded, = session.uploaded
        self.assertLess(len(uploaded), len(original))
        with Image.open(io.BytesIO(uploaded)) as image:
            self.assertEqual(image.size, (1920, 1080))
            self.assertEqual(image.format, "JPEG")

    def test_narrow_frames_and_undecodable_files_upload_unchanged(self):
        from PIL import Image
        narrow = Path(self.temporary.name) / "narrow.jpg"
        Image.new("RGB", (640, 360), color="gray").save(narrow, format="JPEG")
        broken = Path(self.temporary.name) / "broken.jpg"
        broken.write_bytes(b"not a jpeg")
        for path in (narrow, broken):
            with self.subTest(path=path.name):
                session = FakeSession(response=FakeResponse(payload={"results": []}))
                client = PlateRecognizerClient("token", session=session, max_upload_width=1920)
                client.recognise(path)
                (_args, kwargs), = session.calls
                self.assertEqual(kwargs["files"]["upload"][0], path.name)

    def test_abandonment_during_upload_preparation_never_posts(self):
        session = FakeSession(response=FakeResponse(payload={"results": []}))
        client = PlateRecognizerClient("token", session=session, max_upload_width=1920)
        original_open = client._open_upload

        def open_then_abandon(path):
            upload = original_open(path)
            client.abandon_in_flight()
            return upload

        client._open_upload = open_then_abandon

        with self.assertRaises(OcrResponseError) as raised:
            client.recognise(self.path)

        self.assertEqual(raised.exception.failure_cause, "request_abandoned")
        self.assertEqual(session.calls, [])

    def test_rejects_widths_outside_the_safe_range(self):
        for width in (1, 639, 3841, True, 1.5):
            with self.subTest(width=width):
                with self.assertRaises(ValueError):
                    PlateRecognizerClient("token", session=FakeSession(), max_upload_width=width)


class OcrFailureCauseTests(unittest.TestCase):
    """Each rejected response is labelled with a precise, bounded cause."""

    def setUp(self):
        self.image = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        self.image.write(b"test image")
        self.image.close()
        self.path = Path(self.image.name)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def _cause_for(self, response):
        client = PlateRecognizerClient("token", session=FakeSession(response=response))
        with self.assertRaises(OcrResponseError) as caught:
            client.recognise(self.path)
        return caught.exception.failure_cause

    def test_labels_each_rejected_response_shape_with_its_own_cause(self):
        cases = (
            (FakeResponse(status_code=429), "http_429"),
            (FakeResponse(status_code=503), "http_503"),
            (FakeResponse(status_code=500), "http_500"),
            (FakeResponse(json_error=ValueError("no json")), "invalid_json"),
            (FakeResponse(payload=["not", "a", "map"]), "invalid_payload"),
            (FakeResponse(payload={"results": {}}), "invalid_results"),
            (FakeResponse(payload={"results": ["nope"]}), "invalid_result_entry"),
            (FakeResponse(payload={"results": [{"plate": "!!", "score": 0.9}]}),
             "no_usable_plate"),
            (FakeResponse(payload={"results": [{"plate": "12D3456", "score": 2}]}),
             "invalid_confidence"),
        )
        for response, expected in cases:
            with self.subTest(cause=expected):
                self.assertEqual(self._cause_for(response), expected)

    def test_reports_the_status_code_only_for_every_non_success_response(self):
        for status in (400, 401, 404, 429, 500, 502, 503):
            with self.subTest(status=status):
                cause = self._cause_for(FakeResponse(status_code=status))
                self.assertEqual(cause, f"http_{status}")

    def test_bounds_implausible_status_codes_instead_of_echoing_them(self):
        for status in (None, True, "429", 99, 600, 1_000_000, 2.5):
            with self.subTest(status=status):
                self.assertEqual(http_failure_cause(status), "http_invalid_status")

    def test_rejects_unbounded_or_malformed_cause_labels(self):
        for value in (None, 42, "", "Has Spaces", "UPPER", "a" * 33, "9leading"):
            with self.subTest(value=value):
                self.assertEqual(bounded_failure_cause(value), "unclassified")
        self.assertEqual(bounded_failure_cause("read_timeout"), "read_timeout")
        self.assertEqual(bounded_failure_cause("http_429"), "http_429")

    def test_labels_an_abandoned_request_without_changing_its_category(self):
        client = PlateRecognizerClient("token")

        def create_session():
            client.abandon_in_flight()
            return FakeSession(response=FakeResponse(payload={"results": []}))

        with patch.object(client, "_create_session", side_effect=create_session):
            with self.assertRaises(OcrResponseError) as caught:
                client.recognise(self.path)

        self.assertEqual(caught.exception.failure_cause, "request_abandoned")

    def test_labels_a_closed_client_without_changing_its_category(self):
        client = PlateRecognizerClient("token", session=FakeSession())
        client.close()

        with self.assertRaises(RuntimeError) as caught:
            client.recognise(self.path)

        self.assertNotIsInstance(caught.exception, OcrResponseError)
        self.assertEqual(classify_failure_cause(caught.exception), "client_closed")

    def test_separates_network_faults_from_api_faults(self):
        from requests import exceptions

        cases = (
            (exceptions.ConnectTimeout("connect"), "connect_timeout"),
            (exceptions.ReadTimeout("read"), "read_timeout"),
            (exceptions.Timeout("timeout"), "request_timeout"),
            (exceptions.SSLError("tls"), "tls_error"),
            (exceptions.ProxyError("proxy"), "connection_error"),
            (exceptions.ConnectionError("connect"), "connection_error"),
            (exceptions.TooManyRedirects("redirects"), "request_error"),
            (TimeoutError("plain"), "request_timeout"),
            (ConnectionRefusedError("refused"), "connection_error"),
            (ValueError("unrelated"), "unclassified"),
        )
        for error, expected in cases:
            with self.subTest(cause=expected):
                self.assertEqual(classify_failure_cause(error), expected)

    def test_transport_failures_propagate_unchanged_after_being_labelled(self):
        from requests import exceptions

        error = exceptions.ReadTimeout("upstream read timed out")
        client = PlateRecognizerClient("token", session=FakeSession(error=error))

        with self.assertLogs("gate_controller.ocr", level="WARNING") as logs:
            with self.assertRaises(exceptions.ReadTimeout) as caught:
                client.recognise(self.path)

        self.assertIs(caught.exception, error)
        self.assertIn("cause=read_timeout", "\n".join(logs.output))

    def test_a_failing_classifier_never_masks_the_original_transport_error(self):
        from requests import exceptions

        error = exceptions.ConnectTimeout("unreachable")
        client = PlateRecognizerClient("token", session=FakeSession(error=error))

        def explode(_error):
            raise RuntimeError("classifier unavailable")

        with patch.object(ocr_module, "classify_failure_cause", explode):
            with self.assertRaises(exceptions.ConnectTimeout) as caught:
                client.recognise(self.path)

        self.assertIs(caught.exception, error)

    def test_journals_the_cause_without_the_response_body_or_credentials(self):
        secret = "tok_live_abcdef0123456789"
        body = "PLATE 12D3456 owner Jane Doe SENSITIVE_BODY_MARKER"
        client = PlateRecognizerClient(
            secret,
            session=FakeSession(response=FakeResponse(
                status_code=429, payload={"error": body}, text=body,
            )),
        )

        with self.assertLogs("gate_controller.ocr", level="WARNING") as logs:
            with self.assertRaises(OcrResponseError):
                client.recognise(self.path)

        combined = "\n".join(logs.output)
        self.assertIn("cause=http_429", combined)
        self.assertNotIn(secret, combined)
        self.assertNotIn("SENSITIVE_BODY_MARKER", combined)
        self.assertNotIn("12D3456", combined)
        self.assertNotIn(str(self.path), combined)
        self.assertNotIn("platerecognizer.com", combined)

    def test_never_journals_a_credential_for_any_rejected_response(self):
        secret = "tok_live_abcdef0123456789"
        responses = (
            FakeResponse(status_code=500, text=secret),
            FakeResponse(json_error=ValueError(secret)),
            FakeResponse(payload={"results": [{"plate": "!!", "score": 0.9}]}),
        )
        for response in responses:
            with self.subTest(status=response.status_code):
                client = PlateRecognizerClient(
                    secret, session=FakeSession(response=response)
                )
                with self.assertLogs("gate_controller.ocr", level="WARNING") as logs:
                    with self.assertRaises(OcrResponseError):
                        client.recognise(self.path)
                combined = "\n".join(logs.output)
                self.assertNotIn(secret, combined)
                self.assertRegex(combined, r"cause=[a-z][a-z0-9_]{0,31}$")

class OcrPlateRegionTests(unittest.TestCase):
    """Cropping to the plate band and journaling where plates were read."""

    def setUp(self):
        from PIL import Image
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "frame.jpg"
        Image.new("RGB", (3840, 2160), color=(20, 120, 200)).save(self.path, format="JPEG")

    @staticmethod
    def recording_session(payload):
        session = FakeSession(response=FakeResponse(payload=payload))
        session.uploaded = []
        original_post = session.post

        def post(*args, **kwargs):
            session.uploaded.append(kwargs["files"]["upload"][1].read())
            return original_post(*args, **kwargs)

        session.post = post
        return session

    def test_crops_the_region_at_native_detail_then_shrinks_only_if_still_wide(self):
        from PIL import Image
        from gate_controller.plate_region import PlateRegion
        session = self.recording_session({"results": []})
        client = PlateRecognizerClient(
            "token", session=session, max_upload_width=1920,
            plate_region=PlateRegion(0.1, 0.4, 0.8, 0.6),
        )

        with self.assertLogs("gate_controller.ocr", level="INFO") as logs:
            client.recognise(self.path)

        uploaded, = session.uploaded
        with Image.open(io.BytesIO(uploaded)) as image:
            self.assertEqual(image.size, (1920, 810))
        self.assertIn("crop=384,864,3456,2160", "\n".join(logs.output))

        narrow = PlateRecognizerClient(
            "token", session=self.recording_session({"results": []}), max_upload_width=1920,
            plate_region=PlateRegion(0.3, 0.5, 0.4, 0.5),
        )
        narrow.recognise(self.path)
        with Image.open(io.BytesIO(narrow._session.uploaded[0])) as image:
            self.assertEqual(image.size, (1536, 1080), "a crop already narrower than the limit is not upscaled")

    def test_frames_from_the_capture_directory_are_not_cropped_twice(self):
        from PIL import Image
        from gate_controller.plate_region import PlateRegion
        captures = self.root / "trigger-capture"
        captures.mkdir()
        precropped = captures / "keyframe.jpg"
        Image.new("RGB", (3072, 1296), color="white").save(precropped, format="JPEG")
        session = self.recording_session({"results": []})
        client = PlateRecognizerClient(
            "token", session=session, max_upload_width=1920,
            plate_region=PlateRegion(0.1, 0.4, 0.8, 0.6), precropped_directory=captures,
        )

        client.recognise(precropped)

        with Image.open(io.BytesIO(session.uploaded[0])) as image:
            self.assertEqual(image.size, (1920, 810))

    def test_journals_the_plate_box_as_fractions_of_the_whole_frame(self):
        from gate_controller.plate_region import PlateRegion
        box = {"xmin": 960, "ymin": 405, "xmax": 1152, "ymax": 486}
        payload = {"results": [{"plate": "12D3456", "score": 0.93, "box": box}]}
        region = PlateRegion(0.1, 0.4, 0.8, 0.6)

        cropped = PlateRecognizerClient(
            "token", session=self.recording_session(payload), max_upload_width=1920, plate_region=region,
        )
        with self.assertLogs("gate_controller.ocr", level="INFO") as logs:
            cropped.recognise(self.path)
        self.assertIn("gate_ocr plate_box=0.500,0.700,0.080,0.060 frame=cropped", "\n".join(logs.output))

        captures = self.root / "trigger-capture"
        captures.mkdir()
        from PIL import Image
        precropped = captures / "keyframe.jpg"
        Image.new("RGB", (3072, 1296), color="white").save(precropped, format="JPEG")
        from_region = PlateRecognizerClient(
            "token", session=self.recording_session(payload), max_upload_width=1920,
            plate_region=region, precropped_directory=captures,
        )
        with self.assertLogs("gate_controller.ocr", level="INFO") as logs:
            from_region.recognise(precropped)
        self.assertIn("gate_ocr plate_box=0.500,0.700,0.080,0.060 frame=region", "\n".join(logs.output))

        full = PlateRecognizerClient("token", session=self.recording_session(payload), max_upload_width=1920)
        with self.assertLogs("gate_controller.ocr", level="INFO") as logs:
            full.recognise(self.path)
        # The uncropped upload is 1920x1080, so the same pixel box is a shorter
        # fraction of the frame than it was of the 810-high cropped upload.
        self.assertIn("gate_ocr plate_box=0.500,0.375,0.100,0.075 frame=full", "\n".join(logs.output))

    def test_a_malformed_box_is_ignored(self):
        payload = {"results": [{"plate": "12D3456", "score": 0.93, "box": {"xmin": "a"}}]}
        client = PlateRecognizerClient("token", session=self.recording_session(payload), max_upload_width=1920)
        with self.assertLogs("gate_controller.ocr", level="INFO") as logs:
            observation = client.recognise(self.path)
        self.assertEqual(observation.plate, "12D3456")
        self.assertNotIn("plate_box=", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()


class FakeClock:
    def __init__(self, start=100.0):
        self.now = start
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


class SequenceSession:
    """Answer successive posts with the given responses or errors."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.closed = False

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self):
        self.closed = True


class OcrPacingAndRetryTests(unittest.TestCase):
    """Plate Recognizer throttles at one request per second; the client
    paces itself from the previous response and retries once."""

    payload = {"results": [{"plate": "12D3456", "score": 0.93}]}

    def setUp(self):
        self.image = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        self.image.write(b"test image")
        self.image.close()
        self.path = Path(self.image.name)
        self.clock = FakeClock()

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def client(self, session):
        return PlateRecognizerClient(
            "token", session=session, clock=self.clock, sleep=self.clock.sleep,
        )

    def test_back_to_back_requests_wait_out_the_throttle_window(self):
        session = SequenceSession([FakeResponse(payload=self.payload)] * 3)
        client = self.client(session)

        client.recognise(self.path)
        self.clock.now += 0.3
        client.recognise(self.path)
        self.clock.now += 5.0
        client.recognise(self.path)

        self.assertEqual(len(session.calls), 3)
        # Only the second call was too close to the previous response.
        self.assertEqual(self.clock.sleeps, [0.75])

    def test_a_throttled_response_is_retried_once_after_retry_after(self):
        throttled = FakeResponse(status_code=429, payload={"detail": "throttled"})
        throttled.headers = {"Retry-After": "1"}
        session = SequenceSession([throttled, FakeResponse(payload=self.payload)])
        client = self.client(session)

        with self.assertLogs(ocr_module._LOGGER, level="INFO") as logs:
            observation = client.recognise(self.path)

        self.assertEqual(observation.plate, "12D3456")
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(self.clock.sleeps, [1.05])
        combined = "\n".join(logs.output)
        self.assertIn("gate_ocr stage=attempt_failed cause=http_429", combined)
        self.assertIn("gate_ocr stage=retry cause=http_429", combined)
        self.assertNotIn("throttled", combined)

    def test_a_second_throttled_response_fails_with_its_cause(self):
        throttled = FakeResponse(status_code=429, payload={})
        session = SequenceSession([throttled, throttled])
        client = self.client(session)

        with self.assertRaises(OcrResponseError) as raised:
            client.recognise(self.path)

        self.assertEqual(raised.exception.failure_cause, "http_429")
        self.assertEqual(len(session.calls), 2)

    def test_retry_after_is_bounded(self):
        throttled = FakeResponse(status_code=429, payload={})
        throttled.headers = {"Retry-After": "600"}
        session = SequenceSession([throttled, FakeResponse(payload=self.payload)])

        self.client(session).recognise(self.path)

        self.assertEqual(self.clock.sleeps, [2.0])

    def test_a_connection_error_is_retried_once_on_a_fresh_session(self):
        from requests import exceptions as requests_exceptions
        stale = SequenceSession([requests_exceptions.ConnectionError("reset")])
        fresh = SequenceSession([FakeResponse(payload=self.payload)])
        client = self.client(stale)

        with patch.object(client, "_create_session", return_value=fresh):
            observation = client.recognise(self.path)

        self.assertEqual(observation.plate, "12D3456")
        self.assertTrue(stale.closed)
        self.assertEqual(len(stale.calls), 1)
        self.assertEqual(len(fresh.calls), 1)

    def test_timeouts_are_never_retried(self):
        from requests import exceptions as requests_exceptions
        session = SequenceSession([requests_exceptions.ReadTimeout("slow")])
        client = self.client(session)

        with self.assertRaises(requests_exceptions.ReadTimeout):
            client.recognise(self.path)

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_an_idle_session_is_recycled_before_the_next_request(self):
        first = SequenceSession([FakeResponse(payload=self.payload)])
        second = SequenceSession([FakeResponse(payload=self.payload)])
        client = self.client(first)

        client.recognise(self.path)
        self.clock.now += ocr_module.SESSION_IDLE_RECYCLE_SECONDS + 1
        with patch.object(client, "_create_session", return_value=second):
            client.recognise(self.path)

        self.assertTrue(first.closed)
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(len(second.calls), 1)

    def test_abandonment_during_the_throttle_wait_never_posts(self):
        session = SequenceSession([FakeResponse(payload=self.payload)] * 2)
        client = self.client(session)
        client.recognise(self.path)

        def abandon_while_sleeping(seconds):
            self.clock.now += seconds
            client.abandon_in_flight()

        client._sleep = abandon_while_sleeping
        with self.assertRaises(OcrResponseError) as raised:
            client.recognise(self.path)

        self.assertEqual(raised.exception.failure_cause, "request_abandoned")
        self.assertEqual(len(session.calls), 1)

    def test_a_retry_never_outlives_an_abandoned_event(self):
        from requests import exceptions as requests_exceptions
        stale = SequenceSession([requests_exceptions.ConnectionError("reset")])
        fresh = SequenceSession([FakeResponse(payload=self.payload)])
        client = self.client(stale)

        def abandon_while_sleeping(seconds):
            self.clock.now += seconds
            client.abandon_in_flight()

        client._sleep = abandon_while_sleeping
        with patch.object(client, "_create_session", return_value=fresh):
            with self.assertRaises(OcrResponseError) as raised:
                client.recognise(self.path)

        self.assertEqual(raised.exception.failure_cause, "request_abandoned")
        self.assertEqual(fresh.calls, [])
