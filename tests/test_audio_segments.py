"""The continuous recorder: what it writes, what it keeps, and what it cuts."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import unittest

from gate_controller.audio_segments import (
    DEFAULT_MIN_FREE_BYTES, SegmentRecorder, SegmentStore, extract_window,
    iter_adts_frames, load_segment_config, segment_command, segment_started_at,
)


def adts_frame(payload_bytes: int = 57, *, rate_index: int = 8) -> bytes:
    """One ADTS frame with a real header: AAC-LC, MPEG-4, no CRC.

    ``rate_index`` 8 is 16 kHz, the camera's rate, so one frame is 64 ms.
    """
    length = 7 + payload_bytes
    header = bytearray(7)
    header[0] = 0xFF
    header[1] = 0xF1
    header[2] = 0x40 | (rate_index << 2)
    header[3] = 0x40 | ((length >> 11) & 0x03)
    header[4] = (length >> 3) & 0xFF
    header[5] = ((length & 0x07) << 5) | 0x1F
    header[6] = 0xFC
    return bytes(header) + b"\x00" * payload_bytes


def stream(frames: int) -> bytes:
    return b"".join(adts_frame() for _ in range(frames))


class FakeProcess:
    def __init__(self, *, runs_for: int = 2, stderr: bytes = b""):
        self._polls = 0
        self._runs_for = runs_for
        self._stderr = stderr
        self.terminated = False
        self.killed = False

    def poll(self):
        self._polls += 1
        return None if self._polls <= self._runs_for else 1

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def communicate(self, timeout=None):
        return b"", self._stderr


class AdtsParsingTests(unittest.TestCase):
    def test_a_frame_reports_its_own_length_and_duration(self):
        frames = list(iter_adts_frames(adts_frame()))
        self.assertEqual(len(frames), 1)
        offset, length, seconds = frames[0]
        self.assertEqual((offset, length), (0, 64))
        self.assertAlmostEqual(seconds, 1024 / 16000, places=6)

    def test_frames_are_walked_in_order_without_a_decoder(self):
        frames = list(iter_adts_frames(stream(5)))
        self.assertEqual([offset for offset, _, _ in frames], [0, 64, 128, 192, 256])

    def test_a_truncated_tail_ends_the_walk_rather_than_raising(self):
        # A segment interrupted mid-write ends in a partial frame. The frames
        # before it are still good and are still offered.
        data = stream(3)[:-20]
        self.assertEqual(len(list(iter_adts_frames(data))), 2)

    def test_bytes_that_are_not_a_frame_header_stop_the_walk(self):
        self.assertEqual(list(iter_adts_frames(b"not audio at all")), [])


class SegmentNamingTests(unittest.TestCase):
    def test_the_name_carries_the_start_instant_in_utc(self):
        moment = segment_started_at(Path("gate-20260916T123000Z.aac"))
        self.assertEqual(moment, datetime(2026, 9, 16, 12, 30, tzinfo=timezone.utc))

    def test_a_name_from_anything_else_is_not_a_segment(self):
        for name in ("notes.txt", "gate-nonsense.aac", "gate-20260916T993000Z.aac"):
            self.assertIsNone(segment_started_at(Path(name)), name)

    def test_the_command_never_decodes_and_cuts_on_the_clock(self):
        command = segment_command("rtsp://127.0.0.1:8554/clear", Path("/tmp/seg"), seconds=1800)
        self.assertIn("-vn", command)
        self.assertIn("copy", command)
        self.assertNotIn("-c:a", command[command.index("copy"):][1:])
        self.assertIn("-segment_atclocktime", command)
        self.assertIn("-strftime", command)
        self.assertTrue(command[-1].endswith("gate-%Y%m%dT%H%M%SZ.aac"))


class SegmentStoreTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        self.now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)

    def store(self, **overrides) -> SegmentStore:
        settings = {"retention_hours": 48, "clock": lambda: self.now}
        settings.update(overrides)
        return SegmentStore(self.directory, **settings)

    def write(self, started_at: datetime, frames: int = 10) -> Path:
        path = self.directory / f"gate-{started_at.strftime('%Y%m%dT%H%M%S')}Z.aac"
        path.write_bytes(stream(frames))
        return path

    def test_segments_are_listed_oldest_first(self):
        self.write(self.now - timedelta(hours=1))
        self.write(self.now - timedelta(hours=3))
        self.write(self.now - timedelta(hours=2))
        started = [segment.started_at for segment in self.store().segments()]
        self.assertEqual(started, sorted(started))

    def test_a_file_this_does_not_recognise_is_ignored_not_deleted(self):
        stray = self.directory / "somebody-elses-file.aac"
        stray.write_bytes(b"leave me alone")
        self.write(self.now - timedelta(hours=1))
        self.assertEqual(len(self.store().segments()), 1)
        self.store().prune()
        self.assertTrue(stray.exists())

    def test_segments_past_the_horizon_are_pruned(self):
        old = self.write(self.now - timedelta(hours=50))
        kept = self.write(self.now - timedelta(hours=10))
        report = self.store().prune()
        self.assertEqual(report["expired"], 1)
        self.assertFalse(old.exists())
        self.assertTrue(kept.exists())

    def test_the_floor_takes_the_oldest_first_when_space_is_short(self):
        oldest = self.write(self.now - timedelta(hours=3))
        middle = self.write(self.now - timedelta(hours=2))
        newest = self.write(self.now - timedelta(hours=1))
        store = self.store()
        # Never enough room, so the floor takes everything it is allowed to.
        store.free_bytes = lambda: 0
        report = store.prune()
        self.assertEqual(report["for_space"], 2)
        self.assertFalse(oldest.exists())
        self.assertFalse(middle.exists())
        # The newest is the one being written; deleting it frees nothing the
        # recorder is not about to use again.
        self.assertTrue(newest.exists())

    def test_a_healthy_disk_prunes_nothing_for_space(self):
        self.write(self.now - timedelta(hours=1))
        store = self.store()
        store.free_bytes = lambda: DEFAULT_MIN_FREE_BYTES * 2
        self.assertEqual(store.prune()["for_space"], 0)

    def test_covering_finds_only_the_segments_that_overlap(self):
        # Each segment here is 10 frames, so 0.64 s of audio.
        first = self.write(self.now - timedelta(seconds=10))
        second = self.write(self.now - timedelta(seconds=5))
        self.write(self.now - timedelta(seconds=100))
        covering = self.store().covering(
            self.now - timedelta(seconds=10), self.now - timedelta(seconds=4))
        self.assertEqual([segment.path for segment in covering], [first, second])

    def test_a_segment_span_is_measured_from_its_frames_not_assumed(self):
        # A short segment -- the one before a restart -- must not be treated as
        # a full one, or a window after it would claim audio that is not there.
        short = self.write(self.now - timedelta(seconds=30), frames=2)
        segment = next(s for s in self.store().segments() if s.path == short)
        self.assertAlmostEqual(segment.duration(), 2 * 1024 / 16000, places=6)


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        self.now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
        self.store = SegmentStore(self.directory, clock=lambda: self.now)

    def write(self, started_at: datetime, frames: int) -> Path:
        path = self.directory / f"gate-{started_at.strftime('%Y%m%dT%H%M%S')}Z.aac"
        path.write_bytes(stream(frames))
        return path

    def test_a_window_is_cut_on_frame_boundaries_and_stays_playable(self):
        start = self.now - timedelta(seconds=60)
        self.write(start, 100)  # 6.4 s of audio
        cut = extract_window(self.store, start + timedelta(seconds=1),
                             start + timedelta(seconds=2))
        self.assertGreater(len(cut), 0)
        # Every byte kept is whole frames: walking the result consumes it all.
        walked = sum(length for _, length, _ in iter_adts_frames(cut))
        self.assertEqual(walked, len(cut))

    def test_a_window_spanning_two_segments_is_joined(self):
        start = self.now - timedelta(seconds=20)
        self.write(start, 100)
        self.write(start + timedelta(seconds=6.4), 100)
        cut = extract_window(self.store, start + timedelta(seconds=5),
                             start + timedelta(seconds=8))
        frames = list(iter_adts_frames(cut))
        self.assertGreater(len(frames), 20)
        self.assertEqual(sum(length for _, length, _ in frames), len(cut))

    def test_a_gap_in_the_recording_yields_a_shorter_clip_not_silence(self):
        # The recorder was down between these two, so the window cannot be
        # filled. Padding it would put silence the microphone never heard into
        # a training set.
        start = self.now - timedelta(seconds=60)
        self.write(start, 50)
        self.write(start + timedelta(seconds=30), 50)
        cut = extract_window(self.store, start, start + timedelta(seconds=40))
        seconds = sum(seconds for _, _, seconds in iter_adts_frames(cut))
        self.assertAlmostEqual(seconds, 100 * 1024 / 16000, places=3)

    def test_an_empty_or_backwards_window_returns_nothing(self):
        self.write(self.now - timedelta(seconds=10), 50)
        self.assertEqual(extract_window(self.store, self.now, self.now), b"")
        self.assertEqual(
            extract_window(self.store, self.now, self.now - timedelta(seconds=5)), b"")

    def test_a_window_no_segment_covers_returns_nothing(self):
        self.write(self.now - timedelta(hours=5), 50)
        self.assertEqual(extract_window(self.store, self.now - timedelta(minutes=1),
                                        self.now), b"")


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        self.store = SegmentStore(self.directory)

    def test_the_child_is_told_to_name_its_files_in_utc(self):
        recorder = SegmentRecorder(self.store)
        self.assertEqual(recorder.child_environment["TZ"], "UTC")

    def test_the_child_is_spawned_without_a_shell_and_keeps_its_stderr(self):
        seen = {}

        def popen(command, **kwargs):
            seen["command"] = command
            seen["kwargs"] = kwargs
            return FakeProcess(runs_for=0)

        recorder = SegmentRecorder(self.store, popen=popen,
                                   waiter=lambda _event, _seconds: None)
        stop = threading.Event()
        recorder._run_once(stop)
        self.assertIsInstance(seen["command"], tuple)
        self.assertNotIn("shell", seen["kwargs"])
        self.assertIsNotNone(seen["kwargs"]["stderr"])

    def test_run_forever_returns_when_the_stop_event_is_set(self):
        stop = threading.Event()
        stop.set()
        recorder = SegmentRecorder(self.store, popen=lambda *a, **k: FakeProcess())
        recorder.run_forever(stop)
        self.assertEqual(recorder.status()["starts"], 0)

    def test_a_child_that_dies_is_restarted_and_counted(self):
        stop = threading.Event()
        attempts = []

        def popen(command, **kwargs):
            attempts.append(command)
            if len(attempts) >= 3:
                stop.set()
            return FakeProcess(runs_for=0, stderr=b"connection refused")

        recorder = SegmentRecorder(self.store, popen=popen, monotonic=lambda: 0.0,
                                   waiter=lambda _event, _seconds: None)
        recorder.run_forever(stop)
        self.assertGreaterEqual(len(attempts), 2)
        self.assertGreaterEqual(recorder.status()["restarts"], 1)
        self.assertIn("connection refused", recorder.status()["last_error"])

    def test_recording_stops_rather_than_taking_the_last_of_the_card(self):
        stop = threading.Event()
        spawned = []
        self.store.free_bytes = lambda: 0

        def popen(command, **kwargs):
            spawned.append(command)
            return FakeProcess(runs_for=0)

        class OneShot(threading.Event):
            def wait(self, timeout=None):
                self.set()
                return True

        recorder = SegmentRecorder(self.store, popen=popen, monotonic=lambda: 0.0)
        recorder.run_forever(OneShot())
        self.assertEqual(spawned, [])
        self.assertEqual(recorder.status()["low_disk_refusals"], 1)

    def test_status_reports_what_is_on_the_card(self):
        started = datetime(2026, 9, 16, 11, 30, tzinfo=timezone.utc)
        (self.directory / "gate-20260916T113000Z.aac").write_bytes(stream(10))
        recorder = SegmentRecorder(self.store)
        status = recorder.status()
        self.assertEqual(status["segments"], 1)
        self.assertEqual(status["oldest"], started.isoformat())
        self.assertGreater(status["bytes"], 0)


class ConfigurationTests(unittest.TestCase):
    def test_is_off_unless_switched_on(self):
        self.assertFalse(load_segment_config({})["enabled"])

    def test_enabling_without_anywhere_to_write_is_refused(self):
        with self.assertRaises(ValueError):
            load_segment_config({"GATE_AUDIO_SEGMENTS_ENABLED": "true"})

    def test_a_relative_directory_is_refused(self):
        with self.assertRaises(ValueError):
            load_segment_config({"GATE_AUDIO_SEGMENTS_ENABLED": "true",
                                 "GATE_AUDIO_SEGMENTS_DIR": "segments"})

    def test_a_non_loopback_source_is_refused(self):
        with self.assertRaises(ValueError):
            load_segment_config({"GATE_AUDIO_SEGMENTS_SOURCE": "rtsp://192.168.0.54:554/h264"})

    def test_credentials_in_the_source_are_refused(self):
        with self.assertRaises(ValueError):
            load_segment_config(
                {"GATE_AUDIO_SEGMENTS_SOURCE": "rtsp://user:pass@127.0.0.1:8554/clear"})

    def test_settings_outside_their_bounds_are_refused(self):
        for name, value in (("GATE_AUDIO_SEGMENTS_SECONDS", "4"),
                            ("GATE_AUDIO_SEGMENTS_SECONDS", "99999"),
                            ("GATE_AUDIO_SEGMENTS_RETENTION_HOURS", "0"),
                            ("GATE_AUDIO_SEGMENTS_MIN_FREE_BYTES", "1024")):
            with self.assertRaises(ValueError, msg=f"{name}={value}"):
                load_segment_config({name: value})

    def test_the_defaults_are_the_measured_ones(self):
        config = load_segment_config({})
        # Five minutes, set by the corpus contract's 4 MiB payload cap rather
        # than by taste -- see KeepEverythingTests.
        self.assertEqual(config["segment_seconds"], 300)
        self.assertEqual(config["retention_hours"], 48)
        self.assertEqual(config["source_url"], "rtsp://127.0.0.1:8554/clear")


if __name__ == "__main__":
    unittest.main()


class WiringTests(unittest.TestCase):
    """That the recorder is actually reachable from the controller.

    The first version of this work shipped with the module, the tests and the
    documentation all present and the four lines that construct the recorder
    missing from ``__main__``. Everything passed, the release deployed, and
    nothing recorded: there was no test that the wiring existed, because every
    test addressed the module directly. These are those tests.
    """

    #: Importing the entry point pulls in the controller's whole dependency
    #: tree. Where that is not installed the import-based checks skip, but the
    #: source check below does not: it is the one that catches the regression
    #: and it must run everywhere, including on a bare checkout.
    ENTRY_POINT = Path(__file__).resolve().parent.parent / "gate_controller" / "__main__.py"

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    @staticmethod
    def _entry_point():
        try:
            from gate_controller.__main__ import _audio_segment_recorder
        except Exception as error:      # dependency missing in this environment
            raise unittest.SkipTest(f"controller entry point not importable: {error}")
        return _audio_segment_recorder

    def _environment(self, **overrides):
        settings = {
            "GATE_AUDIO_SEGMENTS_ENABLED": "true",
            "GATE_AUDIO_SEGMENTS_DIR": str(self.directory),
        }
        settings.update(overrides)
        return settings

    def test_the_controller_builds_a_recorder_when_it_is_switched_on(self):
        recorder = self._entry_point()(self._environment())
        self.assertIsInstance(recorder, SegmentRecorder)
        self.assertEqual(recorder.store.directory, self.directory)

    def test_the_controller_builds_nothing_when_it_is_off(self):
        self.assertIsNone(self._entry_point()({}))

    def test_the_recorder_carries_the_configured_bounds(self):
        recorder = self._entry_point()(self._environment(
            GATE_AUDIO_SEGMENTS_SECONDS="600",
            GATE_AUDIO_SEGMENTS_RETENTION_HOURS="12",
        ))
        self.assertEqual(recorder.segment_seconds, 600)
        self.assertEqual(recorder.store.retention_hours, 12)

    def test_a_bad_setting_is_refused_before_the_relay_is_claimed(self):
        build = self._entry_point()
        with self.assertRaises(ValueError):
            build(self._environment(GATE_AUDIO_SEGMENTS_DIR="relative/path"))

    def test_the_recorder_satisfies_the_background_worker_protocol(self):
        # `run_worker` starts each background worker as
        # `Thread(target=..., args=(worker.run_forever, (stop_event,)))`.
        # A recorder missing that method would be built, added, and then fail
        # in a supervised thread rather than at startup.
        recorder = self._entry_point()(self._environment())
        self.assertTrue(callable(getattr(recorder, "run_forever", None)))
        self.assertTrue(callable(getattr(recorder, "status", None)))

    def test_the_wiring_is_present_in_the_controller_entry_point(self):
        # The one assertion that would have caught the shipped regression:
        # the construction and the append both have to be there.
        source = self.ENTRY_POINT.read_text(encoding="utf-8")
        self.assertIn("audio_segments = _audio_segment_recorder(", source)
        self.assertIn("background_workers += (audio_segments,)", source)


class KeepEverythingTests(unittest.TestCase):
    """Releasing finished segments to the corpus uploader.

    "Keep everything" is not a new uploader. ``TrainingCorpus.pending`` offers
    only complete pairs, so a segment with no sidecar is invisible to the one
    that already exists; writing the sidecar *is* the release.
    """

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        self.now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
        self.store = SegmentStore(self.directory, clock=lambda: self.now)

    def write(self, minutes_ago: int, frames: int = 100) -> Path:
        started = self.now - timedelta(minutes=minutes_ago)
        path = self.directory / f"gate-{started.strftime('%Y%m%dT%H%M%S')}Z.aac"
        path.write_bytes(stream(frames))
        return path

    def test_a_finished_segment_is_released_to_the_uploader(self):
        from gate_controller.audio_segments import write_ready_sidecars

        finished = self.write(10)
        self.write(5)
        self.assertEqual(write_ready_sidecars(self.store, source_url="rtsp://x"), 1)
        sidecar = finished.with_suffix(".json")
        self.assertTrue(sidecar.exists())
        self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)

    def test_the_segment_still_being_written_is_never_released(self):
        # ffmpeg still has it open; shipping it would upload a truncated file
        # and then delete the local copy that was about to grow.
        from gate_controller.audio_segments import write_ready_sidecars

        self.write(10)
        newest = self.write(1)
        write_ready_sidecars(self.store, source_url="rtsp://x")
        self.assertFalse(newest.with_suffix(".json").exists())

    def test_a_lone_segment_is_never_released(self):
        from gate_controller.audio_segments import write_ready_sidecars

        only = self.write(2)
        self.assertEqual(write_ready_sidecars(self.store, source_url="rtsp://x"), 0)
        self.assertFalse(only.with_suffix(".json").exists())

    def test_a_segment_is_not_released_twice(self):
        from gate_controller.audio_segments import write_ready_sidecars

        self.write(10)
        self.write(5)
        write_ready_sidecars(self.store, source_url="rtsp://x")
        # The uploader deletes both halves on success; offering the pair again
        # would re-upload bytes R2 already holds.
        self.assertEqual(write_ready_sidecars(self.store, source_url="rtsp://x"), 0)

    def test_an_undecodable_segment_is_left_for_the_pruner(self):
        from gate_controller.audio_segments import write_ready_sidecars

        broken = self.directory / "gate-20260917T114000Z.aac"
        broken.write_bytes(b"not audio")
        self.write(1)
        self.assertEqual(write_ready_sidecars(self.store, source_url="rtsp://x"), 0)
        self.assertFalse(broken.with_suffix(".json").exists())

    def test_the_sidecar_routes_the_segment_through_the_existing_uploader(self):
        from gate_controller.audio_segments import write_ready_sidecars
        import json as _json

        finished = self.write(10)
        self.write(5)
        write_ready_sidecars(self.store, source_url="rtsp://127.0.0.1:8554/clear")
        document = _json.loads(finished.with_suffix(".json").read_text())
        # `.aac` already maps to kind=audio in the uploader's media-type table.
        self.assertEqual(document["artefact"], {"kind": "audio", "media_type": "audio/aac"})
        self.assertTrue(document["segment"]["continuous"])
        self.assertFalse(document["audio"]["decoded"])

    def test_a_whole_segment_fits_the_corpus_payload_cap(self):
        # The contract caps an artefact payload at 4 MiB. At the measured
        # 8.1 KB/s the default segment length has to stay under that, or every
        # upload is refused and the card fills with audio nobody can ship.
        from gate_controller.audio_segments import DEFAULT_SEGMENT_SECONDS

        measured_bytes_per_second = 8095
        self.assertLess(DEFAULT_SEGMENT_SECONDS * measured_bytes_per_second,
                        4 * 1024 * 1024)

    def test_pruning_a_segment_that_never_shipped_is_reported_as_loss(self):
        from gate_controller.audio_segments import write_ready_sidecars

        stale = self.write(minutes_ago=60 * 60)      # far past the horizon
        self.write(5)
        write_ready_sidecars(self.store, source_url="rtsp://x")
        self.assertTrue(stale.with_suffix(".json").exists())
        with self.assertLogs("gate_controller.audio_segments", level="WARNING") as logs:
            self.store.prune()
        self.assertTrue(any("pruned_unshipped" in line for line in logs.output))
        self.assertFalse(stale.exists())
        self.assertFalse(stale.with_suffix(".json").exists())

    def test_keep_everything_is_off_unless_asked_for(self):
        self.assertFalse(load_segment_config({})["keep_everything"])
        self.assertTrue(load_segment_config(
            {"GATE_AUDIO_SEGMENTS_KEEP_EVERYTHING": "true"})["keep_everything"])


class RetentionHoldTests(unittest.TestCase):
    """The horizon is the clock while the uploader drains, and the disk while it cannot.

    On 2026-09-22 the router dropped most packets for a day, the corpus
    uploader deferred behind seven undeliverable telemetry items, and 51
    unshipped segments -- the only copy of that audio -- sat waiting for a
    48-hour horizon that would have taken them unheard.
    """

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        self.now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
        self.hold = [None]

    def store(self, **overrides) -> SegmentStore:
        settings = {"retention_hours": 48, "clock": lambda: self.now,
                    "hold_unshipped": lambda: self.hold[0]}
        settings.update(overrides)
        store = SegmentStore(self.directory, **settings)
        store.free_bytes = lambda: DEFAULT_MIN_FREE_BYTES * 2
        return store

    def write(self, hours_ago: float, *, state="fresh") -> Path:
        started = self.now - timedelta(hours=hours_ago)
        path = self.directory / f"gate-{started.strftime('%Y%m%dT%H%M%S')}Z.aac"
        path.write_bytes(stream(10))
        if state == "unshipped":
            path.with_suffix(".json").write_text("{}")
        elif state == "shipped":
            path.with_suffix(".shipped").write_bytes(b"")
        return path

    def test_unshipped_segments_outlive_the_horizon_while_the_uploader_is_held_off(self):
        unshipped = self.write(50, state="unshipped")
        shipped = self.write(49, state="shipped")
        never_released = self.write(49.5)
        recent = self.write(10, state="unshipped")
        self.write(0.1)
        self.hold[0] = "network_down"
        store = self.store()

        with self.assertLogs("gate_controller.audio_segments", level="INFO") as logs:
            report = store.prune()

        self.assertEqual(report["held"], 1)
        self.assertEqual(report["expired"], 2, "shipped and never-released still go")
        self.assertTrue(unshipped.exists())
        self.assertTrue(unshipped.with_suffix(".json").exists(), "still offered to the uploader")
        self.assertFalse(shipped.exists())
        self.assertFalse(never_released.exists())
        self.assertTrue(recent.exists())
        held_lines = [line for line in logs.output if "stage=retention_extended" in line]
        self.assertEqual(len(held_lines), 1)
        self.assertIn("reason=network_down held=1", held_lines[0])
        self.assertIn("oldest=2026-09-20T10:00:00+00:00", held_lines[0])
        self.assertIn("detail=unshipped_kept_until_disk_demands", held_lines[0])
        self.assertFalse(any("pruned_unshipped" in line for line in logs.output))
        self.assertEqual(store.held, 1)

    def test_the_hold_is_journalled_when_it_changes_and_hourly_otherwise(self):
        self.write(50, state="unshipped")
        self.write(0.1)
        self.hold[0] = "event_delivery"
        store = self.store()
        with self.assertLogs("gate_controller.audio_segments", level="INFO") as logs:
            store.prune()
            self.now += timedelta(minutes=5)
            store.prune()
        self.assertEqual(sum("retention_extended" in line for line in logs.output), 1)

        self.hold[0] = "network_down"
        with self.assertLogs("gate_controller.audio_segments", level="INFO") as logs:
            store.prune()
        self.assertIn("reason=network_down", logs.output[0])

        self.now += timedelta(hours=1, minutes=1)
        with self.assertLogs("gate_controller.audio_segments", level="INFO") as logs:
            store.prune()
        self.assertIn("retention_extended", logs.output[0])

    def test_the_hold_ends_when_the_uploader_is_draining_again(self):
        stale = self.write(50, state="unshipped")
        self.write(0.1)
        store = self.store()
        self.hold[0] = "network_down"
        store.prune()
        self.assertTrue(stale.exists())

        self.hold[0] = None
        with self.assertLogs("gate_controller.audio_segments", level="WARNING") as logs:
            report = store.prune()
        self.assertEqual(report["held"], 0)
        self.assertTrue(any("pruned_unshipped" in line for line in logs.output))
        self.assertFalse(stale.exists())
        self.assertEqual(store.held, 0)

    def test_a_hold_that_cannot_be_read_is_no_hold(self):
        stale = self.write(50, state="unshipped")
        self.write(0.1)
        broken = self.store(hold_unshipped=lambda: (_ for _ in ()).throw(RuntimeError("gone")))
        with self.assertLogs("gate_controller.audio_segments", level="WARNING"):
            broken.prune()
        self.assertFalse(stale.exists())

        absent = self.store(hold_unshipped=None)
        self.write(51, state="unshipped")
        with self.assertLogs("gate_controller.audio_segments", level="WARNING"):
            absent.prune()

    def test_the_floor_takes_what_the_cloud_already_has_before_the_only_copy(self):
        oldest_unshipped = self.write(3, state="unshipped")
        shipped = self.write(2.5, state="shipped")
        never_released = self.write(2)
        newer_unshipped = self.write(1.5, state="unshipped")
        newest = self.write(0.1)
        self.hold[0] = "network_down"
        store = self.store()
        free = [0]
        store.free_bytes = lambda: free[0]
        removed = []
        original = store._remove

        def recording(segment):
            removed.append(segment.path)
            if len(removed) == 2:
                free[0] = DEFAULT_MIN_FREE_BYTES * 2
            return original(segment)

        store._remove = recording

        report = store.prune()

        self.assertEqual(report["for_space"], 2)
        self.assertEqual(removed, [shipped, never_released],
                         "shipped and never-released first, oldest first")
        self.assertTrue(oldest_unshipped.exists())
        self.assertTrue(newer_unshipped.exists())
        self.assertTrue(newest.exists())

        # When the disk still demands it, the only copies go too -- oldest
        # first, and never quietly.
        free[0] = 0
        store._remove = original
        with self.assertLogs("gate_controller.audio_segments", level="WARNING") as logs:
            report = store.prune()
        self.assertEqual(report["for_space"], 2)
        self.assertFalse(oldest_unshipped.exists())
        self.assertFalse(newer_unshipped.exists())
        self.assertTrue(newest.exists(), "the one being written is never a candidate")
        self.assertEqual(sum("pruned_unshipped" in line for line in logs.output), 2)

    def test_a_held_segment_past_the_horizon_still_goes_when_the_floor_demands(self):
        held = self.write(50, state="unshipped")
        self.write(0.1)
        self.hold[0] = "network_down"
        store = self.store()
        store.free_bytes = lambda: 0
        with self.assertLogs("gate_controller.audio_segments", level="WARNING") as logs:
            report = store.prune()
        self.assertEqual((report["held"], report["for_space"]), (0, 1))
        self.assertFalse(held.exists())
        self.assertTrue(any("pruned_unshipped" in line for line in logs.output))

    def test_the_recorder_reports_what_it_is_holding(self):
        from gate_controller.audio_segments import SegmentRecorder

        self.write(50, state="unshipped")
        self.write(0.1)
        self.hold[0] = "network_down"
        store = self.store()
        recorder = SegmentRecorder(store, source_url="rtsp://127.0.0.1:8554/clear")
        self.assertEqual(recorder.status()["held_unshipped"], 0)
        store.prune()
        self.assertEqual(recorder.status()["held_unshipped"], 1)

