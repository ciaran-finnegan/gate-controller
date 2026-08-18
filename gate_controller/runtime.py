import sys
from urllib.parse import urlparse


MINIMUM_PYTHON = (3, 10)


def require_python_version(version_info=None) -> None:
    version_info = sys.version_info if version_info is None else version_info
    if tuple(version_info[:2]) < MINIMUM_PYTHON:
        raise RuntimeError("Gate Controller requires Python 3.10 or newer")


def require_https_service_url(url: str, name: str) -> str:
    parsed = urlparse(url) if isinstance(url, str) else None
    if parsed is None or parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError(f"{name} must be an absolute HTTPS URL")
    return url.rstrip("/")


def require_https_or_loopback_service_url(url: str, name: str) -> str:
    parsed = urlparse(url) if isinstance(url, str) else None
    if parsed is None or not parsed.hostname:
        raise ValueError(f"{name} must be an absolute HTTPS or loopback HTTP URL")

    scheme = parsed.scheme.lower()
    is_loopback = parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    if scheme != "https" and not (scheme == "http" and is_loopback):
        raise ValueError(f"{name} must be an absolute HTTPS or loopback HTTP URL")
    return url.rstrip("/")
