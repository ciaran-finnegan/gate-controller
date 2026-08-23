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


if __name__ == "__main__":
    unittest.main()
