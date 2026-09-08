"""Refuse to let a unit test point a child process at a live video stream.

On 2026-09-07 the on-device release verifier ran this suite on the Pi and six
``ffmpeg`` children belonging to the unprivileged build user reached 3.4-3.7 GB
of anonymous memory before the kernel killed them. On a 4 GB board with no
swap that starved everything else: ``sshd`` could not fork and the webhook
listener accepted TCP without ever answering, so a camera event would not have
opened the gate. The board was unusable for an hour and three quarters.

A single decode of the camera's 4K H.265 main stream is enough to do that, and
every default in this repository points at exactly that stream:
``rtsp://127.0.0.1:8554/clear`` for the capture path,
``rtsp://127.0.0.1:8554/camera`` for the fluent path and the transcoder. A test
that builds one of these objects and forgets to inject a fake ``popen`` gets
the live camera, and on a developer machine nothing happens -- there is no
media server on 8554, so the child fails in milliseconds and the test still
passes. The gap between "passes here" and "takes the gate down there" is the
whole problem, and it cannot be closed by review alone.

So this guard closes it structurally, in the same spirit as
``no_live_state_access()`` in ``tests/test_main.py``: **any** command this
suite constructs whose arguments mention an RTSP URL or the loopback media
server raises ``AssertionError`` before the child is spawned. A test that
genuinely needs one says so with ``allow_live_stream``, which is grep-able and
reviewable; everything else fails loudly.

Two implementation notes, both of them traps that would make this silently
inert:

1. ``subprocess.Popen`` is captured *by value* all over ``gate_controller`` --
   ``def __init__(self, *, popen=subprocess.Popen)`` binds the class object at
   import time. Rebinding ``subprocess.Popen`` to a wrapper would therefore do
   nothing at all for the very code paths that matter. The guard patches
   ``subprocess.Popen.__init__`` instead, so the class object stays the same
   one those defaults already hold. That also covers ``subprocess.run``,
   ``call``, ``check_call``, ``check_output`` and ``asyncio``'s subprocess
   transports, all of which funnel through ``Popen``. It is the same shape of
   trap as the ``pathlib`` accessor that made the live-state guard a no-op on
   Python 3.10 (#113), and it is checked by a self-test on every version.

   A test that replaces ``subprocess.Popen`` outright with a mock is not
   guarded while that patch is in place, and does not need to be: a mock does
   not spawn anything. What the guard protects against is the opposite case --
   the test that never replaced it.

2. ``unittest discover -s tests`` -- the exact command the updater runs on the
   device -- makes ``tests`` the top-level directory and never imports
   ``tests/__init__.py``. Installing the guard only from the package would be
   inert precisely on the Pi. ``tests/test_stream_guard.py`` therefore installs
   it at import time as well, and discovery imports every test module before it
   runs any test, so the guard is in place for the whole suite either way.
"""

import os
import subprocess
import sys
import threading

__all__ = [
    "FORBIDDEN_FRAGMENTS",
    "LiveStreamAccess",
    "acknowledge_blocked",
    "allow_live_stream",
    "blocked_commands",
    "install_command_guard",
    "is_installed",
]

# Anything that would reach the camera or the loopback media server. The
# scheme fragments catch a URL wherever it appears -- an argument of its own,
# an ``-i`` value, or a shell string -- and the host:port fragments catch a
# command that reaches the same server by another protocol.
FORBIDDEN_FRAGMENTS = (
    "rtsp://",
    "rtsps://",
    "127.0.0.1:8554",
    "localhost:8554",
)


class LiveStreamAccess(AssertionError):
    """A test tried to run a command against a live stream."""


_lock = threading.Lock()
_allowances: list[str] = []
_blocked: list[str] = []
_installed = False


def is_installed() -> bool:
    """Whether the guard is currently patched in."""
    return _installed


def blocked_commands() -> tuple[str, ...]:
    """Every command blocked since the last test started."""
    with _lock:
        return tuple(_blocked)


def acknowledge_blocked() -> tuple[str, ...]:
    """Take the blocked commands off the record, and return them.

    Only a test whose subject *is* the guard has any business calling this: it
    is how ``tests/test_stream_guard.py`` provokes the guard on purpose without
    the end-of-test backstop then failing it for having done so. Any other test
    that finds itself needing this is a test that should stop building the
    command in the first place.
    """
    with _lock:
        attempted = tuple(_blocked)
        _blocked.clear()
    return attempted


def _reset_blocked() -> None:
    with _lock:
        _blocked.clear()


class allow_live_stream:
    """Opt one integration test out of the guard, with its reason recorded.

    Usable as a decorator on a test method or class, or as a context manager
    around the narrowest possible piece of the test::

        @allow_live_stream("reads one bounded frame from a stream it started")
        def test_...(self):

    The reason is not decoration: it is what a reviewer reads when deciding
    whether this test may run on a device that is also a working gate. Opting
    out is a promise that the command is bounded in time, in frames and in
    address space -- see ``tests/test_clear_stream_ffmpeg.py`` for the shape.
    """

    def __init__(self, reason: str):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("allow_live_stream requires a written reason")
        self.reason = reason.strip()

    def __enter__(self):
        with _lock:
            _allowances.append(self.reason)
        return self

    def __exit__(self, *_exception):
        with _lock:
            if _allowances:
                _allowances.pop()
        return False

    def __call__(self, target):
        if isinstance(target, type):
            for name in list(vars(target)):
                if name.startswith("test"):
                    attribute = getattr(target, name)
                    if callable(attribute):
                        setattr(target, name, self(attribute))
            return target

        reason = self.reason

        def wrapper(*args, **kwargs):
            with allow_live_stream(reason):
                return target(*args, **kwargs)

        wrapper.__name__ = getattr(target, "__name__", "wrapper")
        wrapper.__doc__ = target.__doc__
        wrapper.__wrapped__ = target
        wrapper.__live_stream_reason__ = reason
        return wrapper


def _allowed() -> bool:
    with _lock:
        return bool(_allowances)


def _words(arguments) -> list[str]:
    """Every argument as text, whatever shape the caller passed."""
    if arguments is None:
        return []
    if isinstance(arguments, (str, bytes, os.PathLike)):
        return [os.fsdecode(arguments)]
    words = []
    try:
        for argument in arguments:
            try:
                words.append(os.fsdecode(argument))
            except TypeError:
                words.append(repr(argument))
    except TypeError:
        words.append(repr(arguments))
    return words


def _offending(words) -> str | None:
    for word in words:
        lowered = word.lower()
        for fragment in FORBIDDEN_FRAGMENTS:
            if fragment in lowered:
                return fragment
    return None


def _check(call: str, arguments) -> None:
    words = _words(arguments)
    fragment = _offending(words)
    if fragment is None:
        return
    rendered = " ".join(words)
    if _allowed():
        return
    with _lock:
        _blocked.append(f"{call}: {rendered}")
    raise LiveStreamAccess(
        f"{call}() was about to run a command containing {fragment!r}:\n"
        f"    {rendered[:2000]}\n"
        "No unit test may point a child process at a live stream. On this "
        "machine there is nothing on 8554 and the child would fail in "
        "milliseconds; on the Pi it decodes the camera's 4K H.265 main stream "
        "and has already reached 3.6 GB and been OOM-killed, taking the gate "
        "down with it. Inject a fake process factory instead -- every class on "
        "this path takes one (`popen=`), and `tests/test_trigger_capture.py` "
        "has `FakePopen`. If this test genuinely must run a real stream, "
        "bound it in time, frames and address space and mark it with "
        "`tests.stream_guard.allow_live_stream(\"why\")`."
    )


_real_popen_init = None
_real_test_run = None
_real_os_calls: dict[str, object] = {}


def _install_subprocess() -> None:
    """Guard every child ``subprocess`` can start.

    ``Popen.__init__`` is the single choke point: ``run``, ``call``,
    ``check_call``, ``check_output`` and ``asyncio``'s Unix subprocess
    transport all construct one. Patching the method rather than rebinding the
    name is what makes this work for code holding ``popen=subprocess.Popen``
    from import time.
    """
    global _real_popen_init
    _real_popen_init = subprocess.Popen.__init__

    def guarded(self, args, *rest, **kwargs):
        _check("subprocess.Popen", args)
        return _real_popen_init(self, args, *rest, **kwargs)

    guarded.__gate_stream_guard__ = True
    subprocess.Popen.__init__ = guarded


def _guard_os(name: str, argument_index: int) -> None:
    original = getattr(os, name, None)
    if original is None:  # pragma: no cover - platform without this call
        return
    _real_os_calls[name] = original

    def guarded(*args, **kwargs):
        if len(args) > argument_index:
            _check(f"os.{name}", args[argument_index])
        elif args:
            _check(f"os.{name}", args[0])
        return original(*args, **kwargs)

    guarded.__name__ = name
    guarded.__gate_stream_guard__ = True
    setattr(os, name, guarded)


def _install_os() -> None:
    """Guard the ways a process can be replaced or started without ``Popen``.

    ``gate_media_transcoder`` and ``gate_media_gateway`` both ``os.execve``
    into their binary, and the transcoder's fixed command names two RTSP URLs,
    so a test that stops mocking ``execve`` would turn the test runner itself
    into an unbounded ffmpeg. ``execl*`` funnel into ``execv``/``execve``, so
    guarding the vector forms covers them.
    """
    for name in ("execv", "execve", "execvp", "execvpe"):
        _guard_os(name, 1)
    for name in ("posix_spawn", "posix_spawnp"):
        _guard_os(name, 1)
    for name in ("spawnv", "spawnve", "spawnvp", "spawnvpe"):
        _guard_os(name, 2)
    for name in ("system", "popen"):
        _guard_os(name, 0)


def _install_unittest_backstop() -> None:
    """Fail a test that swallowed the guard's exception.

    The capture, hot-stream and audio paths all wrap their spawn in
    ``except (OSError, ValueError)``, which does not catch this, and that is
    deliberate. But a broad ``except Exception`` somewhere else must not be
    able to turn a blocked live-stream command into a passing test, so the
    attempt is recorded and re-raised against the test that made it.
    """
    global _real_test_run
    import unittest

    _real_test_run = unittest.TestCase.run

    def run(self, result=None):
        _reset_blocked()
        outcome = _real_test_run(self, result)
        attempted = blocked_commands()
        _reset_blocked()
        if attempted:
            _report(self, outcome if outcome is not None else result, attempted)
        return outcome

    run.__gate_stream_guard__ = True
    unittest.TestCase.run = run


def _report(case, result, attempted) -> None:
    message = (
        "this test constructed a command against a live stream and something "
        "swallowed the resulting AssertionError:\n  "
        + "\n  ".join(attempted)
    )
    try:
        try:
            raise LiveStreamAccess(message)
        except LiveStreamAccess:
            result.addFailure(case, sys.exc_info())
    except Exception:  # pragma: no cover - a result object we cannot fail
        sys.stderr.write(f"gate stream guard: {case} -- {message}\n")


def install_command_guard() -> bool:
    """Install the guard for the whole process. Idempotent; returns whether it
    did the work this time."""
    global _installed
    if _installed:
        return False
    _install_subprocess()
    _install_os()
    _install_unittest_backstop()
    _installed = True
    return True
