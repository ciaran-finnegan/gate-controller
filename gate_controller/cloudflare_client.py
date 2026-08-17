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

    def get_json(self, path):
        response = self.session.get(
            self._service_url(path), headers=self._headers(), timeout=self.timeout
        )
        response.raise_for_status()
        return response.json()

    def post_json(self, path, payload):
        response = self.session.post(
            self._service_url(path),
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

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

    def _headers(self):
        return {
            "CF-Access-Client-Id": self.client_id,
            "CF-Access-Client-Secret": self.client_secret,
        }
