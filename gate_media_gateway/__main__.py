"""Validate effective gateway settings before replacing this process with MediaMTX.

With ``--wait-for-address=SECONDS`` the launcher first waits, for at most that
long, until the ICE listener address is actually assigned to this host. After a
power cut the Pi can finish booting before the powerline link has given eth0
its address; MediaMTX then exits at once with "bind: cannot assign requested
address". Waiting here turns that into one clear journal line and a start the
moment the address appears, instead of a restart loop. If the address still is
not there when the wait ends, the launcher exits non-zero and systemd starts it
again (the unit never stops retrying).
"""

import errno
import os
import socket
import sys
import time

from gate_media_config import (
    MediaConfigError,
    relevant_gateway_environment,
    validate_gateway_environment,
)


WAIT_OPTION = "--wait-for-address="
MAX_WAIT_SECONDS = 600
ADDRESS_POLL_SECONDS = 1.0


def address_is_assignable(host: str) -> bool:
    """True when a socket can bind ``host`` on this machine right now.

    Binding port 0 asks for no particular port, so the only reason it can fail
    with EADDRNOTAVAIL is that no interface carries the address yet. Any other
    error is not something waiting will fix, and is raised.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as probe:
        try:
            probe.bind((host, 0))
        except OSError as error:
            if error.errno == errno.EADDRNOTAVAIL:
                return False
            raise
    return True


def wait_for_address(host: str, timeout: float) -> bool:
    """Poll until ``host`` is assignable or ``timeout`` seconds have passed."""
    if address_is_assignable(host):
        return True
    print(
        f"gate media gateway: waiting up to {timeout:g} s for {host} to be "
        "assigned to this host before starting MediaMTX",
        file=sys.stderr, flush=True,
    )
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        time.sleep(ADDRESS_POLL_SECONDS)
        if address_is_assignable(host):
            print(
                f"gate media gateway: {host} is assigned after "
                f"{time.monotonic() - started:.0f} s; starting MediaMTX",
                file=sys.stderr, flush=True,
            )
            return True
    return False


def _wait_seconds(option: str) -> int:
    text = option[len(WAIT_OPTION):]
    if not text.isdigit() or not 1 <= int(text) <= MAX_WAIT_SECONDS:
        raise ValueError(f"{WAIT_OPTION} takes 1 to {MAX_WAIT_SECONDS} whole seconds")
    return int(text)


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    wait_seconds = None
    try:
        if arguments and arguments[0].startswith(WAIT_OPTION):
            wait_seconds = _wait_seconds(arguments.pop(0))
    except ValueError as error:
        print(f"gate media gateway: {error}", file=sys.stderr)
        return 1
    if len(arguments) != 2 or any(not os.path.isabs(value) for value in arguments):
        print("gate media gateway: absolute binary and config paths are required", file=sys.stderr)
        return 1
    binary, config = arguments
    try:
        validated = validate_gateway_environment(relevant_gateway_environment(os.environ))
        # The validator has already required this to be the one exact IP both
        # ICE listeners bind, so it is the address MediaMTX is about to need.
        host = validated["MTX_WEBRTCADDITIONALHOSTS"]
        if wait_seconds is not None and not wait_for_address(host, wait_seconds):
            print(
                f"gate media gateway: {host} is still not assigned to this host "
                f"after {wait_seconds} s; exiting so systemd retries",
                file=sys.stderr,
            )
            return 1
        os.execve(binary, [binary, config], dict(os.environ))
    except (MediaConfigError, OSError, TypeError, ValueError) as error:
        print(f"gate media gateway: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
