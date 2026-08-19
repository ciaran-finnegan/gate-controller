import tempfile
import unittest
from math import inf, nan
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from gate_controller.ocr import OcrResponseError, PlateRecognizerClient


class FakeResponse:
    def __init__(self, status_code=200, payload=None, json_error=None):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

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


if __name__ == "__main__":
    unittest.main()
