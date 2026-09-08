"""Unit tests for the gate controller."""

# No test may point a child process at the live camera stream; see
# tests/stream_guard.py for what happened on the Pi when one did. This covers
# `python -m unittest tests.test_x` and `discover -t .`; discovery started at
# `tests` never imports this package, so `tests/test_stream_guard.py` installs
# the guard at import time too.
from .stream_guard import install_command_guard

install_command_guard()
