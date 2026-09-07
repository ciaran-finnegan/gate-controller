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
encode HEVC.
"""
import shutil
import subprocess
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
# The session reads the copy in 64 KB blocks, so the gate sees the stream the
# same way here.
CHUNK = 64 * 1024
HEVC_ENCODERS = ("libx265", "hevc_videotoolbox", "hevc_nvenc", "hevc_vaapi", "hevc_v4l2m2m")


def _synthesise_hevc() -> bytes | None:
    """Three seconds of busy 10 fps HEVC, or None if nothing here can encode it."""
    for encoder in HEVC_ENCODERS:
        try:
            result = subprocess.run(
                (
                    FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i",
                    f"testsrc2=size=480x270:rate={SOURCE_FPS}:duration=3",
                    "-c:v", encoder, "-g", str(KEYFRAME_INTERVAL),
                    "-f", "hevc", "pipe:1",
                ),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
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
            raise unittest.SkipTest("no usable HEVC encoder in this ffmpeg")

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
                ffmpeg=FFMPEG, filters=(f"fps={SESSION_FPS}",),
                input_framerate=float(SOURCE_FPS),
            ),
            timeout=60,
        )
        self.assertTrue(frames, "the gated stream decodes")
        flat = measure_flat_fraction(frames[0])
        self.assertIsNotNone(flat, "a decoded frame is measurable")
        self.assertLess(
            flat, DEFAULT_MAX_FLAT_FRACTION,
            "the first frame off a mid-GOP join is a picture, not one flat colour",
        )
