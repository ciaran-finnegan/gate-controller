"""One pass over the session decode path with a real ffmpeg and a real bitstream.

Every other test of this path fakes the child process, so all of them agree
with each other about bytes no decoder has ever seen. This one synthesises an
HEVC elementary stream, joins it in the middle of a GOP the way a media
server hands a stream to a reader that connects mid-flight, and takes it
through the same start gate and the same ``decode_command`` the live session
uses, to a JPEG that is then measured.

What it pins down is that the pipeline yields a *picture* from that join. It
cannot reproduce the original fault, which needs the Pi's ``drm`` hardware
decoder: software libavcodec drops leading inter pictures rather than
decoding them against a reference it never received, and that is worth
knowing on its own - the gate is a bound on what the decoder is asked to do,
not the only thing standing between the camera and a green frame.

Skipped when ffmpeg is not on PATH, or when the ffmpeg that is there cannot
encode HEVC -- including the case where it is installed but broken, which a
Homebrew ffmpeg with a stale libx265 is, and which shows up here as an encoder
that will not start rather than as a crash in the test.

Every child this file starts is bounded four ways over, because this is the
only file in the suite that starts a real ffmpeg and the on-device verifier
runs the suite as an unprivileged user on the live gate: a fixed input
duration, an explicit ``-t`` and frame cap, single-threaded decoding, and an
``RLIMIT_AS`` in the child, with a kill deadline on the parent side. On
2026-09-07 an unbounded ffmpeg belonging to that user reached 3.6 GB and was
OOM-killed six times, and the gate did not answer a webhook for an hour and
three quarters. Nothing here may be able to do that even when it is wrong.
"""
import json
import os
import resource
import shutil
import subprocess
import sys
import unittest

from gate_controller.clear_stream import (
    IRAP_TYPES, NAL_PPS, NAL_SPS, NAL_VPS, START_CODE, AnnexBSplitter, IrapStartGate,
    decode_command, decode_frames, nal_type,
)
from gate_controller.images import measure_flat_fraction
from gate_controller.trigger_capture import DEFAULT_MAX_FLAT_FRACTION

FFMPEG = shutil.which("ffmpeg")
SOURCE_FPS = 10
SESSION_FPS = 5
KEYFRAME_INTERVAL = 10
SYNTHESIS_SECONDS = 3
# The session reads the copy in 64 KB blocks, so the gate sees the stream the
# same way here.
CHUNK = 64 * 1024
HEVC_ENCODERS = ("libx265", "hevc_videotoolbox", "hevc_nvenc", "hevc_vaapi", "hevc_v4l2m2m")

# The same shape of bound the audio capture puts on its own child
# (`gate_controller.audio_capture._limit_child`), loosened for a codec: that
# one remuxes and needs 128 MiB, this one runs an encoder and a decoder. A
# gibibyte is far above what 480x270 needs and far below the 3.4-3.7 GB that
# emptied the board, so a runaway dies here instead of taking the gate with
# it. `-threads 1` and a capped arena count are what make it predictable:
# RLIMIT_AS caps *virtual* address space, and per-thread stacks and glibc's
# per-thread malloc arenas would otherwise scale it with the core count.
CHILD_ADDRESS_SPACE_BYTES = 1024 * 1024 * 1024
CHILD_CPU_SECONDS = 60
CHILD_ENVIRONMENT = {"LANG": "C", "LC_ALL": "C", "MALLOC_ARENA_MAX": "2"}
# The parent's kill deadline. Encoding and decoding three seconds of 480x270
# is well under a second on every machine this runs on, including the Pi.
KILL_AFTER_SECONDS = 30


# In preference order. Linux -- where the gate runs, and where CI runs --
# honours RLIMIT_AS, which is the one that matters. macOS refuses to set any
# memory limit at all ("current limit exceeds maximum limit" for every one of
# these), so there the CPU limit is what bounds a runaway; that is fine,
# because no Mac is a gate.
MEMORY_LIMITS = ("RLIMIT_AS", "RLIMIT_DATA", "RLIMIT_RSS")


def _limit_child() -> None:  # pragma: no cover - runs in the child
    """Cap the child's memory and CPU, and drop it to lowest priority.

    RLIMIT_AS makes the allocation fail rather than the board fill; RLIMIT_CPU
    ends a child that spins even if the parent has stopped watching it, which
    a killed or crashed test runner has. A refused limit must not stop the
    child from starting -- a guard that turned this test into a permanent skip
    would be worse than no guard -- so each is attempted in turn and
    ``ChildBoundsTests`` asserts that what this platform does support is
    actually in force.
    """
    for name in MEMORY_LIMITS:
        limit = getattr(resource, name, None)
        if limit is None:
            continue
        try:
            resource.setrlimit(
                limit, (CHILD_ADDRESS_SPACE_BYTES, CHILD_ADDRESS_SPACE_BYTES),
            )
            break
        except (OSError, ValueError):
            continue
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (CHILD_CPU_SECONDS, CHILD_CPU_SECONDS))
    except (OSError, ValueError):
        pass
    try:
        os.nice(19)
    except OSError:
        pass


def _child_options(**overrides) -> dict:
    options = {
        "env": dict(CHILD_ENVIRONMENT),
        "close_fds": True,
        **overrides,
    }
    if os.name == "posix":
        options["preexec_fn"] = _limit_child
    return options


def bounded_child_options(**requested) -> dict:
    """What `_bounded_popen` will actually hand `Popen`, so a test can read it."""
    return {
        **requested,
        **_child_options(env={**(requested.get("env") or {}), **CHILD_ENVIRONMENT}),
    }


def _bounded_popen(command, **kwargs):
    """Process factory for `decode_frames`: same limits, whatever it asks for."""
    return subprocess.Popen(command, **bounded_child_options(**kwargs))


def _synthesise_hevc() -> bytes | None:
    """Three seconds of busy 10 fps HEVC, or None if nothing here can encode it."""
    for encoder in HEVC_ENCODERS:
        try:
            result = subprocess.run(
                (
                    FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-threads", "1",
                    "-f", "lavfi", "-i",
                    f"testsrc2=size=480x270:rate={SOURCE_FPS}:duration={SYNTHESIS_SECONDS}",
                    "-c:v", encoder, "-g", str(KEYFRAME_INTERVAL),
                    # The lavfi source already ends itself, so these two are
                    # belt and braces: a source that did not would still stop
                    # at three seconds, and at thirty pictures.
                    "-t", str(SYNTHESIS_SECONDS),
                    "-frames:v", str(SOURCE_FPS * SYNTHESIS_SECONDS),
                    "-f", "hevc", "pipe:1",
                ),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=KILL_AFTER_SECONDS, **_child_options(),
            )
        except (OSError, subprocess.SubprocessError):
            # Absent, unrunnable (a broken library link exits before main), or
            # past the deadline: try the next encoder, then skip.
            continue
        if result.returncode == 0 and len(result.stdout) > 4096:
            return result.stdout
    return None


def _joined_mid_gop(stream: bytes) -> bytes:
    """The stream as a reader that connected mid-GOP receives it.

    RTSP carries the codec parameters in the SDP, so they are present, and
    starts the reader wherever the GOP happens to be, so the reference
    pictures for what follows are not.
    """
    units = AnnexBSplitter().feed(stream + START_CODE)
    kinds = [nal_type(unit) for unit in units]
    parameter_sets = b""
    for unit, kind in zip(units, kinds):
        if kind in (NAL_VPS, NAL_SPS, NAL_PPS):
            parameter_sets += START_CODE + unit
        elif kind is not None and kind <= 31:
            break  # the first picture: the parameter sets are all in hand
    first_picture = next(index for index, kind in enumerate(kinds) if kind in IRAP_TYPES)
    return parameter_sets + b"".join(
        START_CODE + unit for unit in units[first_picture + 2:]
    )


@unittest.skipIf(FFMPEG is None, "ffmpeg is not on PATH")
class ClearStreamFfmpegTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stream = _synthesise_hevc()
        if cls.stream is None:
            raise unittest.SkipTest(
                "no usable HEVC encoder in this ffmpeg (absent, broken or "
                "without one of " + ", ".join(HEVC_ENCODERS) + ")"
            )

    def test_a_mid_gop_join_decodes_to_a_picture_and_not_a_flat_frame(self):
        joined = _joined_mid_gop(self.stream)
        gate = IrapStartGate()
        forwarded = b"".join(
            gate.feed(joined[at:at + CHUNK]) for at in range(0, len(joined), CHUNK)
        )
        self.assertTrue(gate.started, "the join contains a later keyframe to start at")
        self.assertLess(
            len(forwarded), len(joined),
            "the pictures whose references went past were not forwarded",
        )

        frames = decode_frames(
            forwarded,
            decode_command(
                ffmpeg=FFMPEG,
                # Ahead of -i, which is where the decoder's own options go.
                decoder_arguments=("-threads", "1"),
                filters=(f"fps={SESSION_FPS}",),
                input_framerate=float(SOURCE_FPS),
                # Only the first frame is measured, and the input is three
                # seconds long, so nothing here justifies decoding more.
                frames=SESSION_FPS * SYNTHESIS_SECONDS,
            ),
            popen=_bounded_popen,
            timeout=KILL_AFTER_SECONDS,
        )
        self.assertTrue(
            frames,
            "the gated stream decodes -- an empty result here can also mean "
            f"the child hit its {CHILD_ADDRESS_SPACE_BYTES // (1024 * 1024)} MiB "
            "address-space limit or its kill deadline",
        )
        flat = measure_flat_fraction(frames[0])
        self.assertIsNotNone(flat, "a decoded frame is measurable")
        self.assertLess(
            flat, DEFAULT_MAX_FLAT_FRACTION,
            "the first frame off a mid-GOP join is a picture, not one flat colour",
        )


class ChildBoundsTests(unittest.TestCase):
    """The bounds this file puts on its children are actually in force.

    Needs no ffmpeg, so it runs everywhere and cannot be skipped away. It is
    what stops ``_limit_child`` from quietly becoming a no-op: that function
    tolerates a refused limit so a platform quirk cannot turn the decode test
    into a permanent skip, and that tolerance is exactly the failure mode that
    would otherwise go unnoticed until the next OOM kill.
    """

    def child_limits(self) -> dict:
        program = (
            "import json, resource, sys;"
            "print(json.dumps({name: resource.getrlimit(getattr(resource, name))[0]"
            " for name in sys.argv[1:] if hasattr(resource, name)}))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", program, *MEMORY_LIMITS, "RLIMIT_CPU"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=KILL_AFTER_SECONDS, **_child_options(env={
                **CHILD_ENVIRONMENT, "PATH": os.environ.get("PATH", ""),
            }),
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def test_the_child_cpu_deadline_is_always_in_force(self):
        # The one limit every platform here accepts, and the backstop when the
        # memory limits are refused: a child that spins dies on its own even
        # if the parent has stopped watching it.
        self.assertEqual(CHILD_CPU_SECONDS, self.child_limits()["RLIMIT_CPU"])

    def test_a_memory_limit_is_in_force_wherever_the_platform_allows_one(self):
        limits = self.child_limits()
        capped = [
            name for name in MEMORY_LIMITS
            if limits.get(name) == CHILD_ADDRESS_SPACE_BYTES
        ]
        if sys.platform.startswith("linux"):
            # The gate and CI are both Linux, and Linux honours RLIMIT_AS.
            self.assertEqual(["RLIMIT_AS"], capped)
        elif not capped:
            self.skipTest(
                f"{sys.platform} refuses every memory rlimit; the CPU deadline "
                "is the bound here"
            )

    def test_the_bounded_factory_limits_what_decode_frames_starts(self):
        # decode_frames passes an env of its own and does not know about these
        # bounds, so the factory has to merge them in rather than defer.
        options = bounded_child_options(
            stdin=subprocess.PIPE, env={"LANG": "C", "LC_ALL": "C"},
        )

        self.assertIs(_limit_child, options["preexec_fn"])
        self.assertEqual("2", options["env"]["MALLOC_ARENA_MAX"])
        self.assertEqual("C", options["env"]["LANG"])
        self.assertTrue(options["close_fds"])
        self.assertEqual(subprocess.PIPE, options["stdin"])

    def test_the_bounded_factory_is_what_the_decode_actually_uses(self):
        started = []

        def record(command, **kwargs):
            started.append(kwargs)
            return _bounded_popen(command, **kwargs)

        decode_frames(b"", (sys.executable, "-c", "pass"), popen=record, timeout=5)

        self.assertEqual(1, len(started))
