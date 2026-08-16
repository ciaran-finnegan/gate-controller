"""Strict, stdlib-only validation for isolated gate media configuration."""

import argparse
import ipaddress
import os
import re
import stat
import sys
from collections.abc import Mapping
from urllib.parse import urlsplit


PINNED_MEDIAMTX_VERSION = "1.19.3"
_MAX_CONFIG_BYTES = 16 * 1024
_MAX_CHECKSUM_BYTES = 64 * 1024
_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
_TURN_URL = re.compile(
    r"^(turn|turns):(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+):([0-9]{1,5})"
    r"(?:\?transport=(udp|tcp))?$"
)
_AUTH_KEYS = frozenset({
    "GATE_MEDIA_HMAC_SECRET",
    "GATE_MEDIA_VIDEO_CONFIGURED",
    "GATE_MEDIA_VIDEO_VERIFIED",
    "GATE_MEDIA_LISTEN_CONFIGURED",
    "GATE_MEDIA_LISTEN_VERIFIED",
    "GATE_MEDIA_TALKBACK_CONFIGURED",
})
_GATEWAY_KEYS = frozenset({
    "MTX_PATHS_GATE_SOURCE",
    "MTX_WEBRTCLOCALUDPADDRESS",
    "MTX_WEBRTCLOCALTCPADDRESS",
    "MTX_WEBRTCADDITIONALHOSTS_0",
    "MTX_WEBRTCICESERVERS2_0_URL",
    "MTX_WEBRTCICESERVERS2_0_USERNAME",
    "MTX_WEBRTCICESERVERS2_0_PASSWORD",
    "MTX_WEBRTCICESERVERS2_0_CLIENTONLY",
})
_BOOLEAN_AUTH_KEYS = _AUTH_KEYS - {"GATE_MEDIA_HMAC_SECRET"}


class MediaConfigError(ValueError):
    """Raised for invalid media configuration without including secret values."""


def lookup_trusted_checksum(path, version: str, architecture: str) -> str:
    """Open, validate, and parse a trusted checksum map through one descriptor."""
    if version != PINNED_MEDIAMTX_VERSION:
        raise MediaConfigError("MediaMTX version is not pinned")
    descriptor = _open_trusted_file(path, _MAX_CHECKSUM_BYTES)
    try:
        return lookup_checksum_from_fd(descriptor, version, architecture)
    finally:
        os.close(descriptor)


def lookup_checksum_from_fd(descriptor: int, version: str, architecture: str) -> str:
    """Parse a checksum from the descriptor already selected by the caller."""
    if version != PINNED_MEDIAMTX_VERSION or architecture not in {"arm64", "armv7"}:
        raise MediaConfigError("unsupported MediaMTX release target")
    body = _read_regular_file(descriptor, _MAX_CHECKSUM_BYTES)
    try:
        text = body.decode("ascii")
    except UnicodeDecodeError as error:
        raise MediaConfigError("checksum map must be ASCII") from error

    matches = []
    seen = set()
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split(" ")
        if len(fields) != 3 or any(not field for field in fields):
            raise MediaConfigError("checksum map has an invalid row")
        row_version, row_architecture, checksum = fields
        if row_version != PINNED_MEDIAMTX_VERSION:
            raise MediaConfigError("checksum map contains an unpinned version")
        if row_architecture not in {"arm64", "armv7"}:
            raise MediaConfigError("checksum map has an invalid architecture")
        if not re.fullmatch(r"[a-f0-9]{64}", checksum):
            raise MediaConfigError("checksum map has an invalid SHA-256")
        target = (row_version, row_architecture)
        if target in seen:
            raise MediaConfigError("checksum map has a duplicate target")
        seen.add(target)
        if target == (version, architecture):
            matches.append(checksum)
    if len(matches) != 1:
        raise MediaConfigError("checksum map has no exact approved target")
    return matches[0]


def parse_trusted_environment(path) -> dict[str, str]:
    descriptor = _open_trusted_file(path, _MAX_CONFIG_BYTES)
    try:
        body = _read_regular_file(descriptor, _MAX_CONFIG_BYTES)
    finally:
        os.close(descriptor)
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MediaConfigError("environment must be UTF-8") from error
    if "\r" in text or not text.endswith("\n"):
        raise MediaConfigError("environment must use newline-terminated assignments")

    values = {}
    for line in text.splitlines():
        if not line or any(character.isspace() for character in line):
            raise MediaConfigError("environment has whitespace or an empty line")
        key, separator, value = line.partition("=")
        if separator != "=" or not _KEY.fullmatch(key) or not value:
            raise MediaConfigError("environment has an invalid assignment")
        if key in values:
            raise MediaConfigError("environment has a duplicate assignment")
        values[key] = value
    return values


def validate_auth_environment(values: Mapping[str, str]) -> dict[str, str]:
    selected = dict(values)
    _validate_effective_values(selected)
    if set(selected) != _AUTH_KEYS:
        raise MediaConfigError("auth environment has missing or forbidden keys")
    secret = selected["GATE_MEDIA_HMAC_SECRET"]
    if not 32 <= len(secret.encode("utf-8")) <= 256:
        raise MediaConfigError("HMAC secret must be 32 to 256 bytes")
    if any(selected[key] not in {"true", "false"} for key in _BOOLEAN_AUTH_KEYS):
        raise MediaConfigError("capability flags must be exact booleans")
    if (selected["GATE_MEDIA_VIDEO_VERIFIED"] == "true"
            and selected["GATE_MEDIA_VIDEO_CONFIGURED"] != "true"):
        raise MediaConfigError("verified video must be configured")
    if (selected["GATE_MEDIA_LISTEN_VERIFIED"] == "true"
            and selected["GATE_MEDIA_LISTEN_CONFIGURED"] != "true"):
        raise MediaConfigError("verified listen must be configured")
    return selected


def validate_gateway_environment(values: Mapping[str, str]) -> dict[str, str]:
    selected = dict(values)
    _validate_effective_values(selected)
    if set(selected) != _GATEWAY_KEYS:
        raise MediaConfigError("gateway environment has missing or forbidden keys")
    _validate_rtsp_source(selected["MTX_PATHS_GATE_SOURCE"])
    udp_host, _ = _parse_ice_bind(selected["MTX_WEBRTCLOCALUDPADDRESS"])
    tcp_host, _ = _parse_ice_bind(selected["MTX_WEBRTCLOCALTCPADDRESS"])
    if udp_host != tcp_host:
        raise MediaConfigError("ICE listeners must use the same reachable address")
    if selected["MTX_WEBRTCADDITIONALHOSTS_0"] != udp_host:
        raise MediaConfigError("advertised ICE host must match the listener address")
    _validate_turn_url(selected["MTX_WEBRTCICESERVERS2_0_URL"])
    for key in (
        "MTX_WEBRTCICESERVERS2_0_USERNAME",
        "MTX_WEBRTCICESERVERS2_0_PASSWORD",
    ):
        if not 1 <= len(selected[key].encode("utf-8")) <= 256:
            raise MediaConfigError("TURN credentials must be 1 to 256 bytes")
    if selected["MTX_WEBRTCICESERVERS2_0_CLIENTONLY"] != "false":
        raise MediaConfigError("TURN must be available to MediaMTX and clients")
    return selected


def relevant_auth_environment(environment: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value for key, value in environment.items()
        if key.startswith("GATE_MEDIA_") or key.startswith("MTX_")
    }


def relevant_gateway_environment(environment: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value for key, value in environment.items()
        if key.startswith("MTX_") or key.startswith("GATE_MEDIA_")
    }


def _validate_effective_values(values: Mapping[str, str]) -> None:
    if any(
        not isinstance(key, str) or not isinstance(value, str) or not value
        or any(character.isspace() for character in key)
        or any(character.isspace() for character in value)
        for key, value in values.items()
    ):
        raise MediaConfigError("effective environment values are invalid")


def _open_trusted_file(path, maximum_bytes: int) -> int:
    path = os.path.abspath(os.fspath(path))
    parent, name = os.path.split(path)
    if not name:
        raise MediaConfigError("trusted file path is invalid")
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_descriptor = os.open(parent, directory_flags)
    except OSError as error:
        raise MediaConfigError("trusted file directory is inaccessible") from error
    try:
        parent_metadata = os.fstat(parent_descriptor)
        if (not stat.S_ISDIR(parent_metadata.st_mode)
                or parent_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(parent_metadata.st_mode) & 0o022):
            raise MediaConfigError("trusted file directory is not owner-controlled")
        file_flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, file_flags, dir_fd=parent_descriptor)
        except OSError as error:
            raise MediaConfigError("trusted file is inaccessible") from error
    finally:
        os.close(parent_descriptor)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size <= 0
                or metadata.st_size > maximum_bytes):
            raise MediaConfigError("trusted file must be owner-only and bounded")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_regular_file(descriptor: int, maximum_bytes: int) -> bytes:
    metadata = os.fstat(descriptor)
    if (not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > maximum_bytes):
        raise MediaConfigError("trusted file is not a bounded regular file")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = []
    remaining = metadata.st_size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 4096))
        if not chunk:
            raise MediaConfigError("trusted file changed while reading")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise MediaConfigError("trusted file changed while reading")
    return b"".join(chunks)


def _validate_rtsp_source(value: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise MediaConfigError("gateway source is not a valid RTSP URL") from error
    if (parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname
            or parsed.fragment or not parsed.path or parsed.path == "/"
            or port is not None and not 1 <= port <= 65535):
        raise MediaConfigError("gateway source is not a valid RTSP URL")
    _validate_host(parsed.hostname, allow_private=True)


def _parse_ice_bind(value: str):
    if value.startswith("["):
        match = re.fullmatch(r"\[([^]]+)]:(\d{1,5})", value)
        if not match:
            raise MediaConfigError("ICE bind must be an exact IP and port")
        host, port_text = match.groups()
    else:
        host, separator, port_text = value.rpartition(":")
        if not separator or ":" in host:
            raise MediaConfigError("ICE bind must be an exact IP and port")
    try:
        address = ipaddress.ip_address(host)
        port = int(port_text)
    except ValueError as error:
        raise MediaConfigError("ICE bind must be an exact IP and port") from error
    if (not 1 <= port <= 65535 or address.is_unspecified or address.is_loopback
            or address.is_multicast or address.is_link_local or address.is_reserved):
        raise MediaConfigError("ICE bind is not remotely reachable")
    return str(address), port


def _validate_turn_url(value: str) -> None:
    match = _TURN_URL.fullmatch(value)
    if not match:
        raise MediaConfigError("TURN URL is invalid")
    _scheme, raw_host, port_text, _transport = match.groups()
    host = raw_host[1:-1] if raw_host.startswith("[") else raw_host
    if not 1 <= int(port_text) <= 65535:
        raise MediaConfigError("TURN URL is invalid")
    _validate_host(host, allow_private=True)


def _validate_host(host: str, *, allow_private: bool) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if (host.lower() == "localhost" or len(host) > 253
                or any(not label or len(label) > 63
                       or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
                       for label in host.split("."))):
            raise MediaConfigError("network host is invalid")
        return
    if (address.is_unspecified or address.is_loopback or address.is_multicast
            or address.is_link_local or address.is_reserved
            or not allow_private and address.is_private):
        raise MediaConfigError("network host is invalid")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Validate gate media configuration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    checksum = subparsers.add_parser("checksum")
    checksum.add_argument("--map", required=True)
    checksum.add_argument("--version", required=True)
    checksum.add_argument("--architecture", required=True)
    environment = subparsers.add_parser("environment")
    environment.add_argument("--auth", required=True)
    environment.add_argument("--gateway", required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "checksum":
            print(lookup_trusted_checksum(
                arguments.map, arguments.version, arguments.architecture
            ))
        else:
            validate_auth_environment(parse_trusted_environment(arguments.auth))
            validate_gateway_environment(parse_trusted_environment(arguments.gateway))
    except (MediaConfigError, OSError, TypeError, ValueError) as error:
        print(f"gate media configuration: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
