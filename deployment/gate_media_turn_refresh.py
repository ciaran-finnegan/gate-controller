"""Atomically rotate the isolated MediaMTX Cloudflare TURN credentials."""

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from gate_media_config import (
    MediaConfigError,
    parse_trusted_environment,
    validate_auth_environment,
    validate_split_gateway_environment,
    validate_turn_environment,
)


AUTH_ENVIRONMENT = Path("/etc/gate-media-auth.env")
GATEWAY_ENVIRONMENT = Path("/etc/gate-media-gateway.env")
TURN_ENVIRONMENT = Path("/etc/gate-media-turn.env")
RUNTIME_TURN_ENVIRONMENT = Path("/var/lib/gate-media/turn.env")
GATEWAY_SERVICE = "gate-media-gateway.service"
CLOUDFLARE_TURN_ENDPOINT = (
    "https://rtc.live.cloudflare.com/v1/turn/keys/{key_id}/credentials/"
    "generate-ice-servers"
)
REQUEST_TIMEOUT_SECONDS = 15
USER_AGENT = "gate-mate-turn-refresh/1.0"
SYSTEMCTL_TIMEOUT_SECONDS = 10
GATEWAY_HEALTH_CHECK_ATTEMPTS = 3
GATEWAY_HEALTH_CHECK_INTERVAL_SECONDS = 2
MAX_RESPONSE_BYTES = 64 * 1024
_TURN_URL = re.compile(
    r"^(turn|turns):(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+):(\d{1,5})"
    r"\?transport=(udp|tcp)$"
)
_REPLACED_GATEWAY_KEYS = frozenset({
    "MTX_WEBRTCICESERVERS2_0_URL",
    "MTX_WEBRTCICESERVERS2_0_USERNAME",
    "MTX_WEBRTCICESERVERS2_0_PASSWORD",
})


class TurnRefreshError(RuntimeError):
    """Raised without including long-lived or short-lived secret values."""


class _NoRedirect(HTTPRedirectHandler):
    """Do not forward the long-term bearer token to a redirected URL."""

    def redirect_request(self, _request, _fp, _code, _message, _headers, _newurl):
        return None


class SystemdGatewayService:
    """Small systemctl boundary kept outside the credential transaction."""

    def is_active(self) -> bool:
        returncode = self._run("is-active", "--quiet")
        if returncode == 0:
            return True
        if returncode == 3:
            return False
        raise TurnRefreshError("gateway service state could not be determined")

    def restart(self) -> bool:
        return self._run("restart") == 0

    def stop(self) -> bool:
        return self._run("stop") == 0

    @staticmethod
    def _run(*command: str):
        try:
            completed = subprocess.run(
                ["/usr/bin/systemctl", *command, GATEWAY_SERVICE],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=SYSTEMCTL_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return completed.returncode


def fetch_ice_servers(key_id: str, api_token: str):
    """Request the documented Cloudflare 24-hour ICE-server response."""
    request = Request(
        CLOUDFLARE_TURN_ENDPOINT.format(key_id=key_id),
        data=json.dumps({"ttl": 86400, "customIdentifier": "gate-mate-pi"}).encode("ascii"),
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            if response.status != 201:
                raise TurnRefreshError("TURN credential request was not created")
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, OSError, TimeoutError) as error:
        raise TurnRefreshError("TURN credential request failed") from error
    if len(body) > MAX_RESPONSE_BYTES:
        raise TurnRefreshError("TURN credential response is too large")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TurnRefreshError("TURN credential response is not JSON") from error


def select_turn_credentials(payload) -> tuple[str, str, str]:
    """Choose one documented, authenticated endpoint for MediaMTX."""
    if not isinstance(payload, dict) or set(payload) != {"iceServers"}:
        raise TurnRefreshError("TURN credential response has an invalid top-level shape")
    servers = payload["iceServers"]
    if not isinstance(servers, list) or not servers:
        raise TurnRefreshError("TURN credential response has no ICE servers")

    candidates = []
    for server in servers:
        if not isinstance(server, dict):
            raise TurnRefreshError("TURN credential response has an invalid ICE server")
        urls = server.get("urls")
        if not isinstance(urls, list) or not urls or any(not isinstance(url, str) for url in urls):
            raise TurnRefreshError("TURN credential response has invalid ICE URLs")
        username = server.get("username")
        credential = server.get("credential")
        if username is None and credential is None:
            continue
        if (not isinstance(username, str) or not isinstance(credential, str)
                or not _valid_credential_value(username)
                or not _valid_credential_value(credential)):
            raise TurnRefreshError("TURN credential response has invalid authentication")
        for url in urls:
            parsed = _parse_turn_url(url)
            if parsed is not None:
                candidates.append((_turn_preference(*parsed, url), url, username, credential))

    if not candidates:
        raise TurnRefreshError("TURN credential response has no usable authenticated relay")
    _preference, url, username, credential = min(candidates)
    return url, username, credential


def refresh_turn_credentials(
    *,
    turn_environment=TURN_ENVIRONMENT,
    auth_environment=AUTH_ENVIRONMENT,
    gateway_environment=GATEWAY_ENVIRONMENT,
    runtime_turn_environment=RUNTIME_TURN_ENVIRONMENT,
    fetch_ice_servers=fetch_ice_servers,
    service=None,
    sleep=time.sleep,
) -> None:
    """Fetch, validate, atomically activate, and roll back TURN credentials."""
    turn_values = _load_turn_environment(turn_environment)
    _validate_complete_media_environment(
        auth_environment, gateway_environment, runtime_turn_environment
    )
    previous_runtime_turn = _read_environment_bytes(runtime_turn_environment)
    payload = fetch_ice_servers(
        turn_values["TURN_KEY_ID"], turn_values["TURN_KEY_API_TOKEN"]
    )
    url, username, password = select_turn_credentials(payload)
    staged_runtime_turn = _replace_turn_values(
        previous_runtime_turn, url, username, password
    )
    _validate_staged_media_environment(
        auth_environment,
        gateway_environment,
        runtime_turn_environment,
        staged_runtime_turn,
    )

    service = service or SystemdGatewayService()
    was_active = service.is_active()
    _atomic_write_environment(runtime_turn_environment, staged_runtime_turn)
    if not was_active:
        return
    if service.restart() and _gateway_is_healthy(service, sleep):
        return

    _atomic_write_environment(runtime_turn_environment, previous_runtime_turn)
    if not service.restart() or not _gateway_is_healthy(service, sleep):
        raise TurnRefreshError("new TURN configuration failed and gateway rollback did not restart")
    raise TurnRefreshError("new TURN configuration failed; previous configuration was restored")


def _load_turn_environment(path) -> dict[str, str]:
    try:
        return validate_turn_environment(parse_trusted_environment(path))
    except (MediaConfigError, OSError, TypeError, ValueError) as error:
        raise TurnRefreshError("TURN secret environment is invalid") from error


def _validate_complete_media_environment(auth_path, gateway_path, runtime_turn_path) -> None:
    try:
        validate_auth_environment(parse_trusted_environment(auth_path))
        validate_split_gateway_environment(
            parse_trusted_environment(gateway_path),
            parse_trusted_environment(runtime_turn_path),
        )
    except (MediaConfigError, OSError, TypeError, ValueError) as error:
        raise TurnRefreshError("current media environments are invalid") from error


def _validate_staged_media_environment(
    auth_path, gateway_path, runtime_turn_path, staged_runtime_turn: bytes
) -> None:
    runtime_turn_path = Path(runtime_turn_path)
    staged_path = runtime_turn_path.with_name(
        f".{runtime_turn_path.name}.validate.{os.getpid()}.{secrets.token_hex(8)}"
    )
    try:
        _atomic_write_environment(staged_path, staged_runtime_turn)
        _validate_complete_media_environment(auth_path, gateway_path, staged_path)
    except (OSError, TurnRefreshError) as error:
        raise TurnRefreshError("staged media environments are invalid") from error
    finally:
        try:
            staged_path.unlink()
        except FileNotFoundError:
            pass


def _read_environment_bytes(path) -> bytes:
    path = Path(path)
    try:
        body = path.read_bytes()
        body.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise TurnRefreshError("runtime TURN environment cannot be read") from error
    return body


def _replace_turn_values(previous: bytes, url: str, username: str, password: str) -> bytes:
    values = {
        "MTX_WEBRTCICESERVERS2_0_URL": url,
        "MTX_WEBRTCICESERVERS2_0_USERNAME": username,
        "MTX_WEBRTCICESERVERS2_0_PASSWORD": password,
    }
    try:
        lines = previous.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError as error:
        raise TurnRefreshError("runtime TURN environment is not UTF-8") from error
    replaced = set()
    output = []
    for line in lines:
        key, separator, _value = line.partition("=")
        if separator and key in _REPLACED_GATEWAY_KEYS:
            output.append(f"{key}={values[key]}\n")
            replaced.add(key)
        else:
            output.append(line)
    if replaced != _REPLACED_GATEWAY_KEYS:
        raise TurnRefreshError("runtime TURN environment has incomplete values")
    return "".join(output).encode("utf-8")


def _gateway_is_healthy(service, sleep) -> bool:
    for attempt in range(GATEWAY_HEALTH_CHECK_ATTEMPTS):
        try:
            active = service.is_active()
        except TurnRefreshError:
            return False
        if not active:
            return False
        if attempt + 1 < GATEWAY_HEALTH_CHECK_ATTEMPTS:
            sleep(GATEWAY_HEALTH_CHECK_INTERVAL_SECONDS)
    return True


def _atomic_write_environment(path, body: bytes) -> None:
    path = Path(path)
    parent = path.parent
    temporary = f".{path.name}.new.{os.getpid()}.{secrets.token_hex(8)}"
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    directory_descriptor = None
    try:
        directory_descriptor = os.open(parent, directory_flags)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, body)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path.name, src_dir_fd=directory_descriptor, dst_dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
    except OSError as error:
        raise TurnRefreshError("runtime TURN environment could not be atomically updated") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_descriptor is not None:
            try:
                os.unlink(temporary, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass
            os.close(directory_descriptor)


def _write_all(descriptor: int, body: bytes) -> None:
    remaining = memoryview(body)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("could not write runtime TURN environment")
        remaining = remaining[written:]


def _valid_credential_value(value: str) -> bool:
    return 1 <= len(value.encode("utf-8")) <= 256 and not any(character.isspace() for character in value)


def _parse_turn_url(url: str):
    match = _TURN_URL.fullmatch(url)
    if not match:
        return None
    scheme, _host, port_text, transport = match.groups()
    port = int(port_text)
    if not 1 <= port <= 65535 or port == 53 or (scheme == "turns" and transport != "tcp"):
        return None
    return scheme, port, transport


def _turn_preference(scheme: str, port: int, transport: str, url: str):
    if scheme == "turns" and port == 5349 and transport == "tcp":
        return 0, url
    if scheme == "turn" and port == 3478 and transport == "udp":
        return 1, url
    if scheme == "turn" and port == 3478 and transport == "tcp":
        return 2, url
    return 3, url


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Refresh isolated MediaMTX TURN credentials")
    parser.add_argument("--validate-turn-environment", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        if arguments.validate_turn_environment:
            _load_turn_environment(TURN_ENVIRONMENT)
        else:
            refresh_turn_credentials()
    except TurnRefreshError as error:
        print(f"gate media TURN refresh: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
