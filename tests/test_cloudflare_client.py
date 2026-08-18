import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from gate_controller.cloudflare_client import CloudflareServiceClient


class RecordingResponse:
    def __init__(self, payload=None, status_error=None, json_error=None, status_code=200,
                 content=None):
        self.payload = payload
        self.status_error = status_error
        self.json_error = json_error
        self.status_code = status_code
        self.content = content if content is not None else json.dumps(payload).encode("utf-8")
        self.raise_for_status_called = False

    def raise_for_status(self):
        self.raise_for_status_called = True
        if self.status_error:
            raise self.status_error

    def json(self):
        if self.json_error:
            raise self.json_error
        return self.payload

    def iter_content(self, chunk_size=1):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset + chunk_size]


class RecordingSession:
    def __init__(self, response=None):
        self.response = response or RecordingResponse({})
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append(Request("GET", url, kwargs))
        return self.response

    def post(self, url, **kwargs):
        self.requests.append(Request("POST", url, kwargs))
        return self.response


class Request:
    def __init__(self, method, url, kwargs):
        self.method = method
        self.url = url
        self.headers = kwargs["headers"]
        self.timeout = kwargs["timeout"]
        self.json = kwargs.get("json")
        self.allow_redirects = kwargs["allow_redirects"]
        self.stream = kwargs.get("stream", False)


class CloudflareServiceClientTests(unittest.TestCase):
    def test_cloudflare_client_uses_access_service_headers_and_bounded_timeout(self):
        session = RecordingSession(RecordingResponse({"plates": []}))
        client = CloudflareServiceClient(
            "https://gate.example.com", "client-id", "client-secret",
            session=session, timeout=(1, 2),
        )

        result = client.get_json("/api/controller/plates")

        self.assertEqual(result, {"plates": []})
        self.assertEqual(session.requests[0].headers["CF-Access-Client-Id"], "client-id")
        self.assertEqual(session.requests[0].headers["CF-Access-Client-Secret"], "client-secret")
        self.assertEqual(session.requests[0].headers["User-Agent"], "gate-controller/1")
        self.assertEqual(session.requests[0].timeout, (1, 2))
        self.assertFalse(session.requests[0].allow_redirects)

    def test_cloudflare_client_joins_absolute_paths_to_the_base_url(self):
        session = RecordingSession(RecordingResponse({"ok": True}))
        client = CloudflareServiceClient(
            "https://gate.example.com/", "id", "secret", session=session,
        )

        client.post_json("/api/controller/status", {"online": True})

        request = session.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url, "https://gate.example.com/api/controller/status")
        self.assertEqual(request.json, {"online": True})
        self.assertFalse(request.allow_redirects)

    def test_cloudflare_client_rejects_every_redirect_status(self):
        for status_code in range(300, 400):
            with self.subTest(status_code=status_code):
                session = RecordingSession(RecordingResponse(status_code=status_code))
                client = CloudflareServiceClient(
                    "https://gate.example.com", "id", "secret", session=session,
                )

                with self.assertRaisesRegex(requests.HTTPError, "redirect"):
                    client.get_json("/api/controller/plates")

    def test_get_cross_origin_redirect_never_forwards_access_headers(self):
        self._assert_cross_origin_redirect_is_not_followed("GET")

    def test_post_cross_origin_redirect_never_forwards_access_headers(self):
        self._assert_cross_origin_redirect_is_not_followed("POST")

    def test_write_only_post_accepts_empty_204_and_empty_200(self):
        for status_code in (200, 204):
            with self.subTest(status_code=status_code):
                response = RecordingResponse(
                    status_code=status_code, content=b"",
                    json_error=AssertionError("write response JSON must not be parsed"),
                )
                client = CloudflareServiceClient(
                    "https://gate.example.com", "id", "secret",
                    session=RecordingSession(response),
                )

                self.assertIsNone(client.post_json("/api/controller/status", {"ok": True}))

    def test_post_json_can_require_a_bounded_json_acknowledgement(self):
        response = RecordingResponse({"eventId": 42, "inserted": True})
        session = RecordingSession(response)
        client = CloudflareServiceClient(
            "https://gate.example.com", "id", "secret", session=session,
        )

        result = client.post_json(
            "/api/controller/events", {"event_id": 42},
            expect_json=True, max_response_bytes=1024,
        )

        self.assertEqual(result, {"eventId": 42, "inserted": True})
        self.assertTrue(session.requests[0].stream)

    def test_required_post_json_ack_is_bounded_before_decode(self):
        response = RecordingResponse(content=b'{"eventId":42,"inserted":true}' + b" " * 32)
        client = CloudflareServiceClient(
            "https://gate.example.com", "id", "secret",
            session=RecordingSession(response),
        )

        with self.assertRaisesRegex(ValueError, "response size"):
            client.post_json(
                "/api/controller/events", {"event_id": 42},
                expect_json=True, max_response_bytes=16,
            )

    def test_bounded_get_rejects_response_before_json_decode(self):
        response = RecordingResponse(content=b'{"plates":[]}' + b" " * 32)
        client = CloudflareServiceClient(
            "https://gate.example.com", "id", "secret",
            session=RecordingSession(response),
        )

        with self.assertRaisesRegex(ValueError, "response size"):
            client.get_json("/api/controller/plates", max_response_bytes=16)

    def test_cloudflare_client_rejects_relative_request_paths(self):
        client = CloudflareServiceClient("https://gate.example.com", "id", "secret")

        with self.assertRaisesRegex(ValueError, "absolute path"):
            client.get_json("api/controller/plates")

    def test_cloudflare_client_keeps_scheme_like_paths_on_the_configured_service_origin(self):
        session = RecordingSession()
        client = CloudflareServiceClient(
            "https://gate.example.com", "id", "secret", session=session,
        )

        client.get_json("/https://evil.example/api/controller/plates")

        self.assertEqual(
            session.requests[0].url,
            "https://gate.example.com/https://evil.example/api/controller/plates",
        )

    def test_cloudflare_client_checks_http_status_before_returning_json(self):
        response = RecordingResponse(status_error=RuntimeError("service unavailable"))
        session = RecordingSession(response)
        client = CloudflareServiceClient("https://gate.example.com", "id", "secret", session=session)

        with self.assertRaisesRegex(RuntimeError, "service unavailable"):
            client.get_json("/api/controller/plates")

        self.assertTrue(response.raise_for_status_called)

    def test_cloudflare_client_returns_json_parse_errors(self):
        response = RecordingResponse(json_error=ValueError("invalid JSON"))
        session = RecordingSession(response)
        client = CloudflareServiceClient("https://gate.example.com", "id", "secret", session=session)

        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            client.get_json("/api/controller/plates")

    def test_cloudflare_client_rejects_plain_http_except_loopback(self):
        with self.assertRaisesRegex(ValueError, "GATE_CLOUDFLARE_API_URL"):
            CloudflareServiceClient("http://gate.example.com", "id", "secret")
        CloudflareServiceClient("http://127.0.0.1:8787", "id", "secret")

    def test_cloudflare_client_rejects_unbounded_or_invalid_timeouts(self):
        for timeout in (None, (1, None), (0, 1), (1, 0), (float("inf"), 1), (1,), "1,2"):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(ValueError, "timeout"):
                    CloudflareServiceClient(
                        "https://gate.example.com", "id", "secret", timeout=timeout,
                    )

    def _assert_cross_origin_redirect_is_not_followed(self, method):
        received = []

        class Receiver(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(dict(self.headers))
                self.send_response(204)
                self.end_headers()

            do_POST = do_GET

            def log_message(self, format, *args):
                return

        receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        receiver_thread = threading.Thread(target=receiver.serve_forever)
        receiver_thread.start()
        self.addCleanup(receiver.server_close)
        self.addCleanup(lambda: receiver_thread.join(timeout=2))
        self.addCleanup(receiver.shutdown)

        location = f"http://127.0.0.1:{receiver.server_port}/capture"

        class Redirector(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(307)
                self.send_header("Location", location)
                self.end_headers()

            do_POST = do_GET

            def log_message(self, format, *args):
                return

        redirector = ThreadingHTTPServer(("127.0.0.1", 0), Redirector)
        redirector_thread = threading.Thread(target=redirector.serve_forever)
        redirector_thread.start()
        self.addCleanup(redirector.server_close)
        self.addCleanup(lambda: redirector_thread.join(timeout=2))
        self.addCleanup(redirector.shutdown)

        client = CloudflareServiceClient(
            f"http://127.0.0.1:{redirector.server_port}", "client-id", "client-secret",
        )
        with self.assertRaisesRegex(requests.HTTPError, "redirect"):
            if method == "GET":
                client.get_json("/redirect")
            else:
                client.post_json("/redirect", {"command": "open_gate"})

        self.assertEqual(received, [])
