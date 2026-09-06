import subprocess
import unittest
from io import BytesIO
from threading import Event

from PIL import Image

from gate_controller.clear_stream import HevcPacketRing
from gate_controller.clear_stream_source import ClearStreamSource
from gate_controller.scene import SceneBaseline


def jpeg(color=(20, 120, 200), size=(64, 32)):
    output = BytesIO()
    Image.new("RGB", size, color=color).save(output, format="JPEG")
    return output.getvalue()


def nal(kind, payload=b"\x01", first_slice=True):
    return bytes([(kind << 1) & 0x7E, 0x01, 0x80 if first_slice else 0x00]) + payload


SC = b"\x00\x00\x01"
STREAM = b"".join(SC + u for u in (nal(32), nal(33), nal(34), nal(19, b"key"), nal(1, b"p1"), nal(1, b"p2"))) + SC


class Stdout:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def read(self, _size):
        return self.chunks.pop(0) if self.chunks else b""


class FakeProcess:
    """A child that streams the given stdout chunks, or answers communicate()."""

    def __init__(self, chunks=(), output=b"", hang=False):
        self.stdout = Stdout(chunks)
        self.output = output
        self.hang = hang
        self.terminated = False
        self.killed = False
        self.input = None

    def communicate(self, input=None, timeout=None):
        self.input = input
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        return (b"" if self.killed else self.output), b""

    def poll(self):
        return 0 if (self.terminated or self.killed) else None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


class RecordingPopen:
    def __init__(self, factory):
        self.factory = factory
        self.calls = []

    def __call__(self, command, **kwargs):
        process = self.factory(command)
        self.calls.append((command, process))
        return process


class ClearStreamSourceTests(unittest.TestCase):
    def setUp(self):
        self.clock = [100.0]

    def source(self, factory, **kwargs):
        popen = RecordingPopen(factory)
        source = ClearStreamSource(
            "rtsp://127.0.0.1:8554/clear", decoder_arguments=("-hwaccel", "drm"),
            filters=("hwdownload", "format=nv12", "scale=w='min(iw,1920)':h=-2"),
            popen=popen, clock=lambda: self.clock[0], baseline_seconds=30.0, **kwargs,
        )
        return source, popen

    def test_recording_feeds_the_ring_without_decoding_and_refreshes_the_baseline_when_idle(self):
        stop = Event()
        decoded = jpeg()

        def factory(command):
            if "copy" in command:
                return FakeProcess(chunks=[STREAM[:20], STREAM[20:]])
            return FakeProcess(output=decoded)

        source, popen = self.source(factory)
        original_add = source.scene.observe
        observed = []
        source.scene.observe = lambda frame, now=None: observed.append(frame) or original_add(frame, now)
        # The recording loop restarts when the child ends; stop after the first pass.
        source_run = source.run_forever

        def run_once():
            class OneShot:
                def __init__(self):
                    self.polls = 0

                def is_set(self):
                    self.polls += 1
                    return self.polls > 3

                def wait(self, _seconds):
                    return True

            source_run(OneShot())

        run_once()
        record_calls = [c for c, _ in popen.calls if "copy" in c]
        self.assertEqual(len(record_calls), 1)
        self.assertNotIn("-vf", record_calls[0], "recording never decodes")
        self.assertEqual(source.ring.status()["frames"], 2)
        self.assertEqual(observed, [decoded], "one keyframe decoded for the scene baseline")
        self.assertTrue(source.scene.status()["available"])
        decode_call = next(c for c, _ in popen.calls if "pipe:0" in c)
        self.assertEqual(decode_call[decode_call.index("-frames:v") + 1], "1")
        self.assertIn("-hwaccel", decode_call)

    def test_latest_decodes_the_newest_keyframe_on_demand_and_respects_after(self):
        decoded = jpeg()
        source, popen = self.source(lambda command: FakeProcess(output=decoded))
        source.ring.feed(STREAM)
        self.assertIsNone(source.latest(after=None) if source.ring.latest_keyframe() is None else None)

        frame, captured_at = source.latest()
        self.assertEqual(frame, decoded)
        self.assertEqual(captured_at, 100.0)
        self.assertIsNone(source.latest(after=100.0), "a frame no newer than `after` is not handed out twice")
        self.assertEqual(source.status()["decodes"], 2)
        self.assertEqual(source.status()["mode"], "compressed_ring")

    def test_session_decodes_live_frames_and_stillest_picks_the_stable_one(self):
        still_a, still_b = jpeg((100, 100, 100)), jpeg((101, 101, 101))
        moving = jpeg((200, 50, 50))
        frames = [moving, still_a, still_b]
        first_frame_seen = Event()

        class SessionStdout:
            def __init__(self):
                self.index = 0

            def read(self, _size):
                if self.index >= len(frames):
                    first_frame_seen.wait(1)
                    return b""
                frame = frames[self.index]
                self.index += 1
                self.clock_tick()
                return frame

            def clock_tick(self):
                pass

        session_process = FakeProcess()
        session_process.stdout = SessionStdout()

        def factory(command):
            return session_process if "-t" in command else FakeProcess(output=jpeg())

        source, popen = self.source(factory)
        self.assertTrue(source.start_session())
        self.assertFalse(source.start_session(), "one session at a time")
        session_cmd = next(c for c, _ in popen.calls if "-t" in c)
        self.assertEqual(session_cmd[session_cmd.index("-t") + 1], "45")
        self.assertEqual(session_cmd[session_cmd.index("-vf") + 1], "fps=5,hwdownload,format=nv12,scale=w='min(iw,1920)':h=-2")

        for _ in range(50):
            if source.status()["session"]["frames"] >= 3:
                break
            import time
            time.sleep(0.02)
        self.assertEqual(source.status()["session"]["frames"], 3)
        picked = source.stillest(after=None, window_seconds=5.0)
        self.assertIsNotNone(picked)
        frame, _at, stillness = picked
        self.assertEqual(frame, still_b, "the frame least different from its predecessor")
        self.assertIsNotNone(stillness)
        self.assertLess(stillness, 0.02)

        source.stop_session("test")
        first_frame_seen.set()
        self.assertFalse(source.session_active())
        self.assertTrue(session_process.terminated or session_process.killed)
        # After the session, stillest falls back to a decoded keyframe with no stillness score.
        source.ring.feed(STREAM)
        fallback = source.stillest(after=None)
        self.assertIsNotNone(fallback)
        self.assertIsNone(fallback[2])

    def test_arguments_are_validated_and_close_stops_everything(self):
        with self.assertRaises(ValueError):
            ClearStreamSource("rtsp://127.0.0.1:8554/clear", session_fps=0)
        with self.assertRaises(ValueError):
            ClearStreamSource("rtsp://127.0.0.1:8554/clear", session_seconds=0)
        source, popen = self.source(lambda command: FakeProcess())
        source.close()
        self.assertFalse(source.start_session(), "a closed source starts no session")
