import json
import math

import requests

from .runtime import require_https_or_loopback_service_url


class CloudflareServiceClient:
    def __init__(
        self,
        base_url,
        client_id,
        client_secret,
        session=None,
        timeout=(2, 4),
    ):
        self.base_url = require_https_or_loopback_service_url(
            base_url, "GATE_CLOUDFLARE_API_URL"
        )
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = session or requests.Session()
        self.timeout = self._require_bounded_timeout(timeout)

    def get_json(self, path, *, max_response_bytes=None):
        if (
            max_response_bytes is not None
            and (isinstance(max_response_bytes, bool)
                 or not isinstance(max_response_bytes, int)
                 or max_response_bytes <= 0)
        ):
            raise ValueError("Cloudflare response size limit must be a positive integer")
        response = self.session.get(
            self._service_url(path), headers=self._headers(), timeout=self.timeout,
            allow_redirects=False, stream=max_response_bytes is not None,
        )
        try:
            self._raise_for_redirect(response)
            response.raise_for_status()
            if max_response_bytes is None:
                return response.json()
            return json.loads(self._read_bounded(response, max_response_bytes))
        finally:
            if max_response_bytes is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    close()

    def post_json(
        self,
        path,
        payload,
        *,
        headers=None,
        expect_json=False,
        max_response_bytes=64 * 1024,
    ):
        if expect_json and (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes <= 0
        ):
            raise ValueError("Cloudflare response size limit must be a positive integer")
        response = self.session.post(
            self._service_url(path), headers=self._headers(headers),
            json=payload,
            timeout=self.timeout,
            allow_redirects=False,
            stream=expect_json,
        )
        try:
            self._raise_for_redirect(response)
            response.raise_for_status()
            if not expect_json:
                return None
            return json.loads(self._read_bounded(response, max_response_bytes))
        finally:
            if expect_json:
                close = getattr(response, "close", None)
                if callable(close):
                    close()

    @staticmethod
    def _raise_for_redirect(response):
        if 300 <= response.status_code < 400:
            raise requests.HTTPError(
                f"Cloudflare service returned redirect HTTP {response.status_code}",
                response=response,
            )

    @staticmethod
    def _read_bounded(response, max_response_bytes):
        content = bytearray()
        for chunk in response.iter_content(chunk_size=min(64 * 1024, max_response_bytes + 1)):
            if not chunk:
                continue
            content.extend(chunk)
            if len(content) > max_response_bytes:
                raise ValueError("Cloudflare response size exceeded the configured limit")
        return bytes(content)

    def _service_url(self, path):
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("Cloudflare service request path must be an absolute path")
        return f"{self.base_url}{path}"

    @staticmethod
    def _require_bounded_timeout(timeout):
        if (
            not isinstance(timeout, tuple)
            or len(timeout) != 2
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
                for value in timeout
            )
        ):
            raise ValueError("Cloudflare service timeout must be a finite positive (connect, read) tuple")
        return timeout

    def _headers(self, additional_headers=None):
        headers = {
            "CF-Access-Client-Id": self.client_id,
            "CF-Access-Client-Secret": self.client_secret,
            "User-Agent": "gate-controller/1",
        }
        if additional_headers:
            headers.update(additional_headers)
        return headers


class CloudflareStatusReporter:
    def __init__(self, client, controller_id):
        self.client = client
        self._controller_id = controller_id

    def heartbeat(self, status) -> None:
        self.client.post_json(
            "/api/controller/status", {**status, "controller_id": self._controller_id}
        )
