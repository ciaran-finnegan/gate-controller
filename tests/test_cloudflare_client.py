import unittest

from gate_controller.cloudflare_client import CloudflareServiceClient


class RecordingResponse:
    def __init__(self, payload=None, status_error=None, json_error=None):
        self.payload = payload
        self.status_error = status_error
        self.json_error = json_error
        self.raise_for_status_called = False

    def raise_for_status(self):
        self.raise_for_status_called = True
        if self.status_error:
            raise self.status_error

    def json(self):
        if self.json_error:
            raise self.json_error
        return self.payload


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
        self.assertEqual(session.requests[0].timeout, (1, 2))

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

    def test_cloudflare_client_rejects_relative_request_paths(self):
        client = CloudflareServiceClient("https://gate.example.com", "id", "secret")

        with self.assertRaisesRegex(ValueError, "absolute path"):
            client.get_json("api/controller/plates")

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
