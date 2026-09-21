"""The recorder against a stream that goes away, through the path production runs.

Every recorder here is built by ``gate_controller.__main__._audio_segment_recorder``
from an environment, exactly as the controller builds it, and driven through
``run_forever``. The fakes are at the three edges the recorder cannot own: the
child process, MediaMTX's answer to a DESCRIBE, and the clock.

The fake ffmpeg is faithful to the two things measured on the gate:

* it holds what it has written in a buffer -- 32 s of audio, which is what the
  ``file`` protocol's 256 KiB is at this bitrate -- unless the command carries
  ``-fflags +flush_packets``, and a child that is *stopped* loses that buffer.
  Every segment a deploy cut short was an exact multiple of 262144 bytes;
* it names each file for the second it was opened and cuts on the wall clock.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from gate_controller import audio_segments
from gate_controller.audio_segments import (
    GAP_ENDED, GAP_RESTART, GAP_SHORTFALL, GAP_SOURCE, PROBE_SECONDS,
    RETRY_DELAYS, STALL_SECONDS, GapLedger, SegmentRecorder, SegmentStore,
    describe_status, segment_command, segment_coverage,
)
from tests.test_audio_segments import adts_frame

FRAME_SECONDS = 0.064
BUFFER_SECONDS = 32.0
START = datetime(2026, 9, 20, 10, 40, 0, tzinfo=timezone.utc)
ENTRY_POINT = Path(__file__).resolve().parent.parent / "gate_controller" / "__main__.py"


class FakeFfmpeg:
    """A child that writes ADTS frames to the real directory as the clock moves."""

    def __init__(self, world, command):
        self.world = world
        self.flushes = "+flush_packets" in command
        self.directory = Path(command[-1]).parent
        self.segment_seconds = int(command[command.index("-segment_time") + 1])
        self.exit_code = None
        self.terminated = False
        self.killed = False
        self.stderr = b""
        self.path = None
        self.boundary = None
        self.buffer = []            # [(path, frame)] not yet on the card
        self.written_until = world.now
        if world.status_now() != 200:
            self.exit_code = 1
            self.stderr = b"[rtsp @ 0x5555] method DESCRIBE failed: 404 Not Found"

    # -- what the world does to it ----------------------------------------

    def advance(self, until):
        while self.exit_code is None and self.written_until + timedelta(seconds=FRAME_SECONDS) <= until:
            moment = self.written_until
            if self.world.status_at(moment) != 200:
                self._exit(0)       # EOF from MediaMTX: a clean exit, buffer flushed
                return
            self.written_until = moment + timedelta(seconds=FRAME_SECONDS)
            if self.world.stalled_at(moment):
                continue            # connected, nothing arriving
            if self.world.drops_frame(moment):
                continue            # the camera never sent this one
            if self.path is None or moment >= self.boundary:
                self._open(moment)
            self.buffer.append((self.path, adts_frame()))
            if self.flushes:
                self._flush()
            elif len(self.buffer) * FRAME_SECONDS >= BUFFER_SECONDS:
                self._flush()

    def _open(self, moment):
        self.path = self.directory / moment.strftime(audio_segments.SEGMENT_TEMPLATE)
        epoch = int(moment.timestamp())
        self.boundary = datetime.fromtimestamp(
            epoch - epoch % self.segment_seconds + self.segment_seconds, timezone.utc)

    def _flush(self):
        for path, frame in self.buffer:
            with open(path, "ab") as handle:
                handle.write(frame)
        self.buffer = []

    def _exit(self, code):
        self._flush()
        self.exit_code = code

    # -- what the recorder does to it --------------------------------------

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True
        if self.exit_code is None:
            self.buffer = []        # stopped, not finished: the buffer is gone
            self.exit_code = 255

    def kill(self):
        self.killed = True
        self.terminate()

    def communicate(self, timeout=None):
        return b"", self.stderr


class World:
    """The clock, MediaMTX and the children, advanced only by the recorder's waits."""

    def __init__(self, directory, *, until):
        self.directory = directory
        self.now = START
        self.monotonic_now = 5000.0
        self.until = until
        self.outages = []           # [(start, end)] DESCRIBE answers 404
        self.stalls = []            # [(start, end)] connected, nothing arriving
        self.delivered = 1.0        # fraction of frames the camera sends
        self._sent = 0.0
        self.children = []
        self.probes = []
        self.waits = []
        self.stop = threading.Event()

    def status_at(self, moment):
        return 404 if any(a <= moment < b for a, b in self.outages) else 200

    def status_now(self):
        return self.status_at(self.now)

    def stalled_at(self, moment):
        return any(a <= moment < b for a, b in self.stalls)

    def drops_frame(self, moment):
        self._sent += self.delivered
        if self._sent >= 1.0:
            self._sent -= 1.0
            return False
        return True

    def probe(self, _url):
        self.probes.append(self.now)
        return self.status_now()

    def popen(self, command, **_kwargs):
        child = FakeFfmpeg(self, command)
        self.children.append((self.now, child))
        return child

    def wait(self, _event, seconds):
        self.waits.append(seconds)
        self.now += timedelta(seconds=seconds)
        self.monotonic_now += seconds
        for _, child in self.children:
            child.advance(self.now)
        if self.now >= self.until:
            self.stop.set()

    def drive(self, recorder):
        """Swap the three edges for fakes and run the production loop."""
        recorder._popen = self.popen
        recorder._probe = self.probe
        recorder._clock = lambda: self.now
        recorder._monotonic = lambda: self.monotonic_now
        recorder._waiter = self.wait
        recorder.run_forever(self.stop)
        return recorder


def audio_seconds(directory):
    return sum(segment.duration() for segment in SegmentStore(directory).segments())


class RecorderCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def build(self, **overrides):
        """The recorder exactly as the controller builds it."""
        from gate_controller.__main__ import _audio_segment_recorder

        environment = {
            "GATE_AUDIO_SEGMENTS_ENABLED": "true",
            "GATE_AUDIO_SEGMENTS_DIR": str(self.directory),
        }
        environment.update(overrides)
        recorder = _audio_segment_recorder(environment)
        self.assertIsNotNone(recorder)
        return recorder

    def gaps(self):
        return GapLedger(self.directory).read()


class ADescribe404Storm(RecorderCase):
    def test_recording_resumes_within_the_bound_however_many_restarts_there_have_been(self):
        """The defect measured on the gate: by the twelfth restart of a
        process's life every quick failure waited a full minute, with the
        stream back and nobody reading it."""
        world = World(self.directory, until=START + timedelta(minutes=40))
        for index in range(15):
            begins = START + timedelta(seconds=100 + index * 150)
            world.outages.append((begins, begins + timedelta(seconds=70)))
        recorder = world.drive(self.build())

        bound = PROBE_SECONDS + RETRY_DELAYS[0] + 1.0 + 1.0   # probe, retry, first look, slack
        gaps = self.gaps()
        self.assertEqual(len(gaps), 15)
        for (begins, ends), gap in zip(world.outages, gaps):
            self.assertEqual(gap["cause"], GAP_SOURCE)
            self.assertLessEqual((gap["end"] - ends).total_seconds(), bound,
                                 f"resumed {gap['end'] - ends} after the stream returned")
            self.assertLessEqual(abs((gap["start"] - begins).total_seconds()), 1.5)
        self.assertGreaterEqual(recorder.status()["restarts"], 15)

    def test_no_child_is_spawned_into_a_stream_mediamtx_says_is_not_there(self):
        world = World(self.directory, until=START + timedelta(minutes=5))
        world.outages.append((START + timedelta(seconds=60), START + timedelta(seconds=180)))
        recorder = world.drive(self.build())

        during = [at for at, _ in world.children
                  if world.outages[0][0] < at < world.outages[0][1]]
        self.assertEqual(during, [], "an ffmpeg was spawned to be told 404")
        asked = [at for at in world.probes if world.outages[0][0] <= at < world.outages[0][1]]
        self.assertLessEqual(len(asked), 120 / PROBE_SECONDS + 2)
        self.assertGreaterEqual(len(asked), 100)      # and it kept asking, every second
        self.assertGreater(recorder.status()["no_stream_probes"], 0)

    def test_the_whole_outage_costs_the_outage_and_a_moment_more(self):
        world = World(self.directory, until=START + timedelta(minutes=10))
        world.outages.append((START + timedelta(seconds=200), START + timedelta(seconds=270)))
        world.drive(self.build())
        self.assertGreaterEqual(audio_seconds(self.directory), 600 - 70 - 4)

    def test_a_probe_that_gets_no_answer_does_not_hold_a_spawn_back(self):
        """MediaMTX itself restarting refuses the connection. ffmpeg's own
        error is worth more than a guess, and the old tests -- which have no
        MediaMTX to ask -- must still reach their child."""
        world = World(self.directory, until=START + timedelta(seconds=30))
        recorder = self.build()
        world.probe = lambda _url: None
        world.drive(recorder)
        self.assertEqual(len(world.children), 1)


class ASourceDropMidSegment(RecorderCase):
    def test_the_partial_segment_is_kept(self):
        world = World(self.directory, until=START + timedelta(minutes=4))
        world.outages.append((START + timedelta(seconds=137), START + timedelta(seconds=150)))
        world.drive(self.build())
        first = SegmentStore(self.directory).segments()[0]
        self.assertGreaterEqual(first.duration(), 135.0)
        self.assertEqual(self.gaps()[0]["cause"], GAP_SOURCE)

    def test_a_stream_that_stops_arriving_is_noticed_and_the_partial_kept(self):
        """Connected, and nothing coming. The child has to be *stopped*, which
        is exactly when a buffered ffmpeg throws away its last 32 seconds."""
        world = World(self.directory, until=START + timedelta(minutes=5))
        world.stalls.append((START + timedelta(seconds=137), START + timedelta(seconds=1000)))
        recorder = world.drive(self.build())

        first = SegmentStore(self.directory).segments()[0]
        self.assertGreaterEqual(first.duration(), 135.0)
        self.assertGreaterEqual(recorder.status()["stalls"], 1)
        self.assertTrue(world.children[0][1].terminated)
        stopped_at = world.children[1][0]
        self.assertLessEqual((stopped_at - world.stalls[0][0]).total_seconds(),
                             STALL_SECONDS + 3.0)

    def test_without_the_flush_flag_this_same_stall_loses_the_tail(self):
        """The harness models the measured behaviour, so this is what the
        command's ``-fflags +flush_packets`` is for -- and what removing it
        would cost."""
        recorder = self.build()
        recorder.command = tuple(part for part in recorder.command
                                 if part not in ("-fflags", "+flush_packets"))
        world = World(self.directory, until=START + timedelta(minutes=5))
        world.stalls.append((START + timedelta(seconds=137), START + timedelta(seconds=1000)))
        world.drive(recorder)
        first = SegmentStore(self.directory).segments()[0]
        self.assertLess(first.duration(), 137.0 - 5.0)
        # A whole number of buffers, to the nearest: 127.99999999999 is four of
        # them, and `% 32` of it is 31.99999999999.
        buffers = first.duration() / BUFFER_SECONDS
        self.assertGreaterEqual(round(buffers), 1)
        self.assertAlmostEqual(buffers, round(buffers), delta=0.01)

    def test_the_stall_guard_outlasts_an_unflushed_buffer(self):
        """Were it shorter, an ffmpeg that ignored the flush flag would be
        killed before its first write, every time, and nothing would ever be
        recorded."""
        self.assertGreater(STALL_SECONDS, BUFFER_SECONDS * 1.5)
        recorder = self.build()
        recorder.command = tuple(part for part in recorder.command
                                 if part not in ("-fflags", "+flush_packets"))
        world = World(self.directory, until=START + timedelta(minutes=6))
        world.drive(recorder)
        self.assertEqual(len(world.children), 1)
        self.assertEqual(recorder.status()["stalls"], 0)


class Rotation(RecorderCase):
    def test_no_audio_is_lost_across_a_boundary_and_the_child_is_left_alone(self):
        """The newest file changing name and shrinking to nothing at every
        boundary must not look like a stall, and housekeeping on its timer
        must not disturb the child either."""
        world = World(self.directory, until=START + timedelta(minutes=17))
        recorder = world.drive(self.build())

        self.assertEqual(len(world.children), 1)
        self.assertEqual(recorder.status()["restarts"], 0)
        segments = SegmentStore(self.directory).segments()
        self.assertEqual([s.started_at.minute for s in segments], [40, 45, 50, 55])
        self.assertAlmostEqual(audio_seconds(self.directory), 17 * 60, delta=2 * FRAME_SECONDS + 1.0)
        for row in segment_coverage(SegmentStore(self.directory)):
            self.assertLess(row["missing_seconds"], 2 * FRAME_SECONDS, row["segment"])
        self.assertEqual(self.gaps(), [])

    def test_finished_segments_are_released_while_the_child_keeps_running(self):
        """Housekeeping used to run only between children, so on the gate 49
        segments were released at once, every four hours, whenever a TURN
        refresh happened to restart MediaMTX."""
        world = World(self.directory, until=START + timedelta(minutes=17))
        world.drive(self.build(GATE_AUDIO_SEGMENTS_KEEP_EVERYTHING="true"))
        self.assertEqual(len(world.children), 1)
        released = sorted(path.name for path in self.directory.glob("gate-*.json"))
        self.assertGreaterEqual(len(released), 2)


class GapsAsData(RecorderCase):
    def test_a_gap_is_written_down_with_its_times_and_its_cause(self):
        world = World(self.directory, until=START + timedelta(minutes=6))
        outage = (START + timedelta(seconds=90), START + timedelta(seconds=187))
        world.outages.append(outage)
        recorder = world.drive(self.build())

        (gap,) = self.gaps()
        self.assertEqual(gap["cause"], GAP_SOURCE)
        self.assertLessEqual(abs((gap["start"] - outage[0]).total_seconds()), 1.5)
        self.assertGreaterEqual(gap["end"], outage[1])
        self.assertLessEqual((gap["end"] - outage[1]).total_seconds(), 3.5)
        self.assertAlmostEqual(gap["seconds"], 97, delta=4)
        status = recorder.status()
        self.assertEqual(status["gaps"], 1)
        self.assertEqual(status["last_gap"]["cause"], GAP_SOURCE)

    def test_the_ledger_is_private_and_is_neither_a_segment_nor_an_artefact(self):
        world = World(self.directory, until=START + timedelta(minutes=3))
        world.outages.append((START + timedelta(seconds=30), START + timedelta(seconds=60)))
        world.drive(self.build())
        ledger = self.directory / audio_segments.GAP_LEDGER_NAME
        self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(ledger, [s.path for s in SegmentStore(self.directory).segments()])
        from gate_controller.corpus import TrainingCorpus
        corpus = TrainingCorpus(self.directory, max_bytes=64 * 1024 * 1024)
        self.assertEqual([a for a in corpus.pending() if "listening" in a.stem], [])

    def test_a_restart_of_the_controller_is_a_gap_from_the_last_audio_on_the_card(self):
        before = START - timedelta(seconds=72)
        (self.directory / before.strftime(audio_segments.SEGMENT_TEMPLATE)).write_bytes(
            b"".join(adts_frame() for _ in range(int(60 / FRAME_SECONDS))))
        world = World(self.directory, until=START + timedelta(minutes=1))
        world.drive(self.build())

        (gap,) = self.gaps()
        self.assertEqual(gap["cause"], GAP_RESTART)
        self.assertLessEqual(abs((gap["start"] - (START - timedelta(seconds=12))).total_seconds()), 0.1)
        self.assertLessEqual((gap["end"] - START).total_seconds(), 2.5)

    def test_a_child_that_stops_with_the_stream_still_there_is_not_blamed_on_the_source(self):
        world = World(self.directory, until=START + timedelta(minutes=2))
        recorder = self.build()

        def popen(command, **kwargs):
            child = World.popen(world, command, **kwargs)
            if len(world.children) == 1:
                original = child.advance

                def advance(until):
                    original(until)
                    if until >= START + timedelta(seconds=40) and child.exit_code is None:
                        child.stderr = b"Conversion failed!"
                        child._exit(1)
                child.advance = advance
            return child

        world.popen = popen
        world.drive(recorder)
        (gap,) = self.gaps()
        self.assertEqual(gap["cause"], GAP_ENDED)
        self.assertIn("Conversion failed", gap["detail"])

    def test_a_camera_that_sends_half_its_audio_is_reported_against_the_span(self):
        """86% of what was lost on the gate: the recorder up throughout, and
        segments holding 115-210 s of a 300 s span. No restart, no journal
        line, nothing to retry -- so it has to be measured, and said."""
        world = World(self.directory, until=START + timedelta(minutes=16))
        world.delivered = 0.55
        world.drive(self.build())
        self.assertEqual(len(world.children), 1)
        self.assertEqual(self.gaps(), [], "the recorder never stopped; this is not its gap")

        from gate_controller.gate_sound_scan import measure_listening
        store = SegmentStore(self.directory)
        scanned = [(segment.path.name, 0) for segment in store.segments()[:-1]]
        listening = measure_listening(store, scanned)
        self.assertEqual({gap["cause"] for gap in listening.gaps}, {GAP_SHORTFALL})
        whole = listening.coverage[1]
        self.assertAlmostEqual(whole["audio_seconds"], 300 * 0.55, delta=2)
        self.assertAlmostEqual(whole["missing_seconds"], 300 * 0.45, delta=2)
        self.assertEqual((listening.gaps[1]["started_at"], listening.gaps[1]["ended_at"]),
                         (whole["started_at"], whole["ended_at"]))


class TheHeartbeat(RecorderCase):
    def test_the_gate_block_says_how_much_was_heard_and_when_it_was_not(self):
        from gate_controller.gate_audio_detect import MotorRun, movements_from
        from gate_controller.gate_sound_scan import gate_state, measure_listening, record
        from gate_controller.store import LocalStore

        world = World(self.directory, until=START + timedelta(minutes=16))
        outage = (START + timedelta(seconds=427), START + timedelta(seconds=524))
        world.outages.append(outage)
        world.drive(self.build())

        store = SegmentStore(self.directory)
        scanned = [(segment.path.name, 100) for segment in store.segments()[:-1]]
        database = LocalStore(self.directory / "gate.db")
        connection = sqlite3.connect(database.path)
        self.addCleanup(connection.close)
        moves = movements_from([MotorRun(start=START, end=START + timedelta(seconds=20))], [],
                               commanded_at=(START,), initial_state="shut")
        listening = measure_listening(store, scanned)
        record(connection, moves, scanned, listening=listening)
        record(connection, moves, scanned, listening=listening)     # a re-scan adds nothing

        gate = gate_state(connection, now=world.now)
        heard, missed = gate["listening_24h_seconds"], gate["not_listening_24h_seconds"]
        span = (store.segments()[-1].started_at - START).total_seconds()
        self.assertAlmostEqual(heard + missed, span, delta=2)
        self.assertAlmostEqual(missed, 97, delta=5)
        self.assertEqual(gate["listening_gaps_24h"], 1)
        self.assertAlmostEqual(gate["longest_gap_24h_seconds"], 97, delta=5)
        self.assertEqual(gate["last_gap_cause"], GAP_SOURCE)
        self.assertLessEqual(abs((datetime.fromisoformat(gate["last_gap_at"])
                                  - outage[0]).total_seconds()), 1.5)
        # Additive: everything that was there is still there.
        for key in ("state", "confirmed_24h", "unconfirmed_24h", "latches_heard_24h",
                    "state_confirmation", "movements_24h", "segments_scanned"):
            self.assertIn(key, gate)
        # What the dashboard's ingest keeps is non-negative finite numbers.
        for key in ("listening_24h_seconds", "not_listening_24h_seconds",
                    "listening_gaps_24h", "longest_gap_24h_seconds"):
            self.assertIsInstance(gate[key], int)
            self.assertGreaterEqual(gate[key], 0)

    def test_a_database_nothing_has_been_measured_into_adds_nothing(self):
        from gate_controller.gate_audio_detect import MotorRun, movements_from
        from gate_controller.gate_sound_scan import gate_state, listening_state, record
        from gate_controller.store import LocalStore

        connection = sqlite3.connect(LocalStore(self.directory / "gate.db").path)
        self.addCleanup(connection.close)
        self.assertEqual(listening_state(connection), {})
        moves = movements_from([MotorRun(start=START, end=START + timedelta(seconds=20))], [],
                               commanded_at=(START,), initial_state="shut")
        record(connection, moves, [("gate-20260920T104000Z.aac", 10)])   # an older caller
        gate = gate_state(connection, now=START + timedelta(hours=1))
        self.assertEqual(gate["state"], "open")
        self.assertNotIn("listening_24h_seconds", gate)


class Shutdown(RecorderCase):
    def test_a_stop_is_clean_and_loses_nothing_already_heard(self):
        world = World(self.directory, until=START + timedelta(seconds=200))
        recorder = self.build()
        worker = threading.Thread(target=world.drive, args=(recorder,))
        worker.start()
        worker.join(timeout=20)
        self.assertFalse(worker.is_alive(), "run_forever did not return")

        (_, child), = world.children
        self.assertTrue(child.terminated)
        self.assertFalse(child.killed)
        self.assertGreaterEqual(audio_seconds(self.directory), 200 - 2.0)
        self.assertEqual(self.gaps(), [], "a stop is the next start's gap to write, not this one's")

    def test_the_next_start_finds_the_audio_the_stop_left_and_measures_from_it(self):
        world = World(self.directory, until=START + timedelta(seconds=200))
        world.drive(self.build())
        again = World(self.directory, until=START + timedelta(seconds=260))
        again.now = START + timedelta(seconds=207)
        again.drive(self.build())
        (gap,) = self.gaps()
        self.assertEqual(gap["cause"], GAP_RESTART)
        self.assertLessEqual(gap["seconds"], 7 + 3.0)
        self.assertLessEqual(abs((gap["start"] - (START + timedelta(seconds=200))).total_seconds()), 1.5)


class Backoff(RecorderCase):
    def test_a_child_that_never_records_backs_off_and_one_that_did_does_not(self):
        world = World(self.directory, until=START + timedelta(minutes=10))
        outage = (START + timedelta(seconds=300), START + timedelta(seconds=310))
        world.outages.append(outage)
        recorder = self.build()
        barren = {"left": 8}

        def popen(command, **kwargs):
            child = World.popen(world, command, **kwargs)
            if barren["left"]:
                barren["left"] -= 1
                child.exit_code = 1
                child.stderr = b"Invalid data found when processing input"
            return child

        world.popen = popen
        world.drive(recorder)
        # Eight children in a row that wrote nothing: each waits longer, to a ceiling.
        self.assertEqual(tuple(world.waits[:8]), RETRY_DELAYS[1:] + (RETRY_DELAYS[-1],) * 3)
        self.assertLessEqual(max(world.waits), RETRY_DELAYS[-1])
        # The ninth recorded. When its stream went away, none of that history
        # counted against it: it was back within the bound.
        resumed = [gap for gap in self.gaps() if gap["cause"] == GAP_SOURCE]
        self.assertEqual(len(resumed), 1)
        self.assertLessEqual((resumed[0]["end"] - outage[1]).total_seconds(),
                             PROBE_SECONDS + RETRY_DELAYS[0] + 2.0)


class TheCommand(unittest.TestCase):
    def test_every_frame_is_flushed_and_only_the_audio_is_asked_for(self):
        command = segment_command("rtsp://127.0.0.1:8554/clear", Path("/tmp/seg"))
        source = command.index("-i")
        # An input option: it has to be before the URL to stop the video being sent.
        self.assertLess(command.index("-allowed_media_types"), source)
        self.assertEqual(command[command.index("-allowed_media_types") + 1], "audio")
        # An output option, and `-fflags`: `-flush_packets 1` does not reach
        # the muxer the segmenter opens. Measured on the Pi's ffmpeg 5.1.5.
        self.assertGreater(command.index("-fflags"), source)
        self.assertEqual(command[command.index("-fflags") + 1], "+flush_packets")
        self.assertNotIn("-flush_packets", command)


class TheProbe(unittest.TestCase):
    class Channel:
        def __init__(self, reply):
            self.reply, self.sent = reply, b""

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def settimeout(self, _seconds):
            pass

        def sendall(self, data):
            self.sent += data

        def recv(self, _size):
            return self.reply

    def test_it_asks_what_ffmpeg_would_ask_and_reads_the_status(self):
        channel = self.Channel(b"RTSP/1.0 404 Not Found\r\nCSeq: 1\r\n\r\n")
        seen = {}

        def connect(address, timeout=None):
            seen["address"] = address
            return channel

        status = describe_status("rtsp://127.0.0.1:8554/clear", connect=connect)
        self.assertEqual(status, 404)
        self.assertEqual(seen["address"], ("127.0.0.1", 8554))
        self.assertTrue(channel.sent.startswith(b"DESCRIBE rtsp://127.0.0.1:8554/clear RTSP/1.0\r\n"))

    def test_nothing_listening_is_none_not_an_exception(self):
        def connect(address, timeout=None):
            raise ConnectionRefusedError()

        self.assertIsNone(describe_status("rtsp://127.0.0.1:8554/clear", connect=connect))
        self.assertIsNone(describe_status(
            "rtsp://127.0.0.1:8554/clear", connect=lambda *a, **k: self.Channel(b"nonsense")))


class TheRecorderMainWiresUp(unittest.TestCase):
    """Fails if what was changed here is not what the controller runs."""

    def test_the_entry_point_constructs_this_class_from_this_module(self):
        source = ENTRY_POINT.read_text(encoding="utf-8")
        self.assertIn("from .audio_segments import SegmentRecorder, SegmentStore, load_segment_config",
                      source)
        self.assertIn("return SegmentRecorder(", source)
        self.assertIn("audio_segments = _audio_segment_recorder(os.environ)", source)
        self.assertIn("background_workers += (audio_segments,)", source)

    def test_what_it_builds_has_the_loop_the_ledger_and_the_flushing_command(self):
        from gate_controller.__main__ import _audio_segment_recorder

        with tempfile.TemporaryDirectory() as directory:
            recorder = _audio_segment_recorder({
                "GATE_AUDIO_SEGMENTS_ENABLED": "true",
                "GATE_AUDIO_SEGMENTS_DIR": directory,
            })
            self.assertIs(type(recorder), SegmentRecorder)
            self.assertIs(type(recorder).run_forever, audio_segments.SegmentRecorder.run_forever)
            self.assertEqual(type(recorder).run_forever.__module__, "gate_controller.audio_segments")
            self.assertIsInstance(recorder.ledger, GapLedger)
            self.assertEqual(recorder.ledger.path.parent, Path(directory))
            self.assertIn("+flush_packets", recorder.command)
            self.assertIs(recorder._probe, describe_status)

    def test_the_scanner_the_timer_runs_records_what_was_not_heard(self):
        script = ENTRY_POINT.parent.parent / "scripts" / "scan_gate_sound.py"
        source = script.read_text(encoding="utf-8")
        self.assertIn("listening = measure_listening(store, scanned)", source)
        self.assertIn("record(connection, moves, scanned, listening=listening)", source)


if __name__ == "__main__":
    unittest.main()
