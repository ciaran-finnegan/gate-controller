"""Validate effective gateway settings before replacing this process with MediaMTX."""

import os
import sys

from gate_media_config import (
    MediaConfigError,
    relevant_gateway_environment,
    validate_gateway_environment,
)


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2 or any(not os.path.isabs(value) for value in arguments):
        print("gate media gateway: absolute binary and config paths are required", file=sys.stderr)
        return 1
    binary, config = arguments
    try:
        validate_gateway_environment(relevant_gateway_environment(os.environ))
        os.execve(binary, [binary, config], dict(os.environ))
    except (MediaConfigError, OSError, TypeError, ValueError) as error:
        print(f"gate media gateway: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
