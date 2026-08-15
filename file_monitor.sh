#!/bin/sh

"/usr/bin/python3" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else "gate-controller requires Python 3.10 or newer")' || exit $?
exec /usr/bin/python3 -m gate_controller "$@"
