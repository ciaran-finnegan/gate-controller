import subprocess
import unittest
from io import BytesIO

from PIL import Image

from gate_controller.clear_stream import (
    AnnexBSplitter, HevcPacketRing, decode_command, decode_frames, nal_type, record_command,
)


def nal(kind: int, payload: bytes = b"\x01", *, first_slice: bool = True) -> bytes:
    """A synthetic NAL unit: two-byte header, then a slice header byte."""
    header = bytes([(kind << 1) & 0x7E, 0x01])
    body = bytes([0x80 if first_slice else 0x00]) + payload
    return header + body


SC = b"\x00\x00\x01"
VPS, SPS, PPS, IDR, P = nal(32), nal(33), nal(34), nal(19, b"key"), nal(1, b"pfr")


def stream(*units: bytes, four_byte: bool = False) -> bytes:
    prefix = b"\x00" + SC if four_byte else SC
    return b"".join(prefix + unit for unit in units)


class AnnexBSplitterTests(unittest.TestCase):
    def test_splits_units_across_chunk_boundaries_and_both_start_code_lengths(self):
        data = stream(VPS, SPS, four_byte=True) + stream(PPS, IDR, P)
        splitter = AnnexBSplitter()
        units = []
        for i in range(0, len(data), 5):
            units.extend(splitter.feed(data[i:i + 5]))
        units.extend(splitter.feed(SC))  # a trailing start code flushes the last unit
        self.assertEqual(units, [VPS, SPS, PPS, IDR, P])
        self.assertEqual([nal_type(u) for u in units], [32, 33, 34, 19, 1])

    def test_a_runaway_unit_is_dropped_instead_of_growing_forever(self):
        splitter = AnnexBSplitter(max_nal_bytes=64)
        self.assertEqual(splitter.feed(SC + b"\x02\x01" + b"x" * 200), [])
        self.assertEqual(splitter.feed(SC + IDR + SC), [IDR])


class HevcPacketRingTests(unittest.TestCase):
    def test_groups_frames_into_gops_with_their_parameter_sets(self):
        clock = [10.0]
        ring = HevcPacketRing(clock=lambda: clock[0])
        # A frame only completes when the next picture starts, and the splitter
        # only emits a unit once the following start code has arrived.
        self.assertEqual(ring.feed(stream(VPS, SPS, PPS, IDR, P, P)), 1)
        clock[0] = 11.0
        self.assertEqual(ring.feed(stream(IDR, P) + SC), 3)

        keyframe, at = ring.latest_keyframe()
        self.assertEqual(keyframe, stream(VPS, SPS, PPS) + SC + IDR)
        self.assertEqual(at, 11.0)
        gop, times = ring.latest_gop()
        self.assertEqual(gop, stream(VPS, SPS, PPS, IDR), "the trailing P frame is still open")
        self.assertEqual(times, [11.0])
        status = ring.status(now=12.0)
        self.assertEqual((status["gops"], status["frames"], status["dropped_gops"]), (2, 4, 0))
        self.assertEqual(status["newest_age_seconds"], 1.0)

    def test_frames_before_the_first_keyframe_are_discarded(self):
        ring = HevcPacketRing(clock=lambda: 0.0)
        self.assertEqual(ring.feed(stream(P, P, VPS, SPS, PPS, IDR, P) + SC), 1)
        gop, _ = ring.latest_gop()
        self.assertEqual(gop, stream(VPS, SPS, PPS, IDR))
        self.assertEqual(ring.status()["frames"], 1)

    def test_ring_is_bounded_by_gop_count_and_bytes(self):
        ring = HevcPacketRing(max_gops=2, clock=lambda: 0.0)
        for _ in range(4):
            ring.feed(stream(VPS, SPS, PPS, IDR, P))
        ring.feed(SC)
        self.assertEqual(ring.status()["gops"], 2)
        self.assertEqual(ring.status()["dropped_gops"], 2)

        tiny = HevcPacketRing(max_bytes=len(stream(VPS, SPS, PPS, IDR, P)) + 4, clock=lambda: 0.0)
        for _ in range(3):
            tiny.feed(stream(VPS, SPS, PPS, IDR, P))
        tiny.feed(SC)
        self.assertEqual(tiny.status()["gops"], 1, "only the newest GOP fits the byte bound")

    def test_multi_slice_pictures_stay_one_frame(self):
        ring = HevcPacketRing(clock=lambda: 0.0)
        second_slice = nal(1, b"s2", first_slice=False)
        self.assertEqual(ring.feed(stream(VPS, SPS, PPS, IDR, nal(1, b"s1"), second_slice, P) + SC), 2)
        gop, _ = ring.latest_gop()
        self.assertEqual(gop.count(SC + nal(1, b"s1")), 1)
        self.assertIn(SC + second_slice, gop)
        self.assertEqual(ring.status()["frames"], 2)

    def test_bounds_are_validated(self):
        with self.assertRaises(ValueError):
            HevcPacketRing(max_bytes=0)
        with self.assertRaises(ValueError):
            HevcPacketRing(max_gops=0)


class DecodeTests(unittest.TestCase):
    def test_commands_copy_without_decoding_and_decode_from_stdin(self):
        record = record_command("rtsp://127.0.0.1:8554/clear")
        self.assertIn("copy", record)
        self.assertNotIn("-vf", record)
        decode = decode_command(decoder_arguments=("-hwaccel", "drm"), filters=("hwdownload", "fps=5"), frames=1)
        self.assertEqual(decode[decode.index("-i") + 1], "pipe:0")
        self.assertEqual(decode[decode.index("-vf") + 1], "hwdownload,fps=5")
        self.assertEqual(decode[decode.index("-frames:v") + 1], "1")

    def test_decode_frames_returns_the_jpegs_the_child_wrote_and_bounds_a_hang(self):
        output = BytesIO()
        Image.new("RGB", (16, 8), color="red").save(output, format="JPEG")
        jpeg = output.getvalue()

        class Process:
            def __init__(self, hang=False):
                self.hang, self.killed, self.stdin = hang, False, None

            def communicate(self, input=None, timeout=None):
                if self.hang and not self.killed:
                    raise subprocess.TimeoutExpired("ffmpeg", timeout)
                return (b"" if self.killed else jpeg + jpeg), b""

            def kill(self):
                self.killed = True

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        frames = decode_frames(b"data", ("ffmpeg",), popen=lambda *a, **k: Process(), timeout=1)
        self.assertEqual(frames, [jpeg, jpeg])
        hung = Process(hang=True)
        with self.assertLogs("gate_controller.clear_stream", level="WARNING") as logs:
            self.assertEqual(decode_frames(b"data", ("ffmpeg",), popen=lambda *a, **k: hung, timeout=0.1), [])
        self.assertTrue(hung.killed)
        self.assertIn("decode=timeout", logs.output[0])

        def failing_popen(*a, **k):
            raise OSError("no ffmpeg")

        with self.assertLogs("gate_controller.clear_stream", level="WARNING"):
            self.assertEqual(decode_frames(b"data", ("ffmpeg",), popen=failing_popen), [])
