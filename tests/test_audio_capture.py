import json
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gate_controller.audio_capture import (
    AudioCaptureConfig, AudioClipRecorder, AudioClipStore, DEFAULT_SOURCE,
    load_audio_capture_config,
)


def adts(payload=b"\x00" * 512):
    """A buffer that begins with a plausible ADTS AAC frame header."""
    return b"\xff\xf1\x50\x80\x00\x1f\xfc" + payload


class FakeProcess:
    """A stand-in ffmpeg that yields bytes once and then reports EOF.

    Backed by a temporary file rather than a pipe so a clip larger than the
    kernel's pipe buffer can be offered without the writer blocking on a
    reader that has not started yet. ``select`` treats a regular file as
    always ready, which is exactly the behaviour the bounded reader needs.
    """

    def __init__(self, output=b"", returncode=0):
        handle = tempfile.TemporaryFile()
        handle.write(output)
        handle.seek(0)
        self.stdout = handle
        self.returncode = returncode
        self.killed = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self.returncode


class Event:
    def __init__(self, event_type="Motion", sequence=7):
        self.event_type = event_type
        self.sequence = sequence
        self.received_at = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


class ConfigurationTests(unittest.TestCase):
    def test_is_off_unless_switched_on(self):
        config = load_audio_capture_config({}, "/var/lib/gate-controller/corpus")

        self.assertFalse(config.enabled)
        self.assertEqual(config.source_url, DEFAULT_SOURCE)

    def test_clips_default_to_a_directory_beside_the_image_corpus(self):
        config = load_audio_capture_config(
            {"GATE_AUDIO_CAPTURE_ENABLED": "true"}, "/var/lib/gate-controller/corpus",
        )

        self.assertTrue(config.enabled)
        self.assertEqual(config.directory, Path("/var/lib/gate-controller/corpus/audio"))

    def test_enabling_without_anywhere_to_write_is_refused(self):
        with self.assertRaises(ValueError):
            load_audio_capture_config({"GATE_AUDIO_CAPTURE_ENABLED": "true"}, None)

    def test_a_non_loopback_source_is_refused(self):
        with self.assertRaises(ValueError):
            load_audio_capture_config(
                {"GATE_AUDIO_CAPTURE_SOURCE": "rtsp://192.168.0.54:554/h264Preview"}, None,
            )

    def test_credentials_in_the_source_are_refused(self):
        with self.assertRaises(ValueError):
            load_audio_capture_config(
                {"GATE_AUDIO_CAPTURE_SOURCE": "rtsp://user:pass@127.0.0.1:8554/clear"}, None,
            )

    def test_a_clip_longer_than_the_ceiling_is_refused(self):
        with self.assertRaises(ValueError):
            load_audio_capture_config({"GATE_AUDIO_CAPTURE_SECONDS": "600"}, None)

    def test_an_unbounded_store_is_refused(self):
        with self.assertRaises(ValueError):
            load_audio_capture_config({"GATE_AUDIO_CAPTURE_MAX_TOTAL_BYTES": "1024"}, None)


class AudioClipStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "audio"
        self.now = [datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)]

    def store(self, **kwargs):
        return AudioClipStore(self.root, clock=lambda: self.now[0], **kwargs)

    def test_writes_the_clip_and_sidecar_privately_as_a_matching_pair(self):
        store = self.store()
        clip = adts()

        path = store.record(clip, {"schema_version": 1, "kind": "gate_audio",
                                   "label": "actuated"})

        self.assertIsNotNone(path)
        self.assertEqual(path.suffix, ".aac")
        self.assertEqual(path.read_bytes(), clip)
        self.assertEqual(oct(path.stat().st_mode & 0o777), oct(0o600))
        self.assertEqual(oct(self.root.stat().st_mode & 0o777), oct(0o700))
        sidecar = json.loads(path.with_suffix(".json").read_text())
        self.assertEqual(sidecar["kind"], "gate_audio")
        self.assertEqual(sidecar["label"], "actuated")
        self.assertEqual(sidecar["audio"]["bytes"], len(clip))
        self.assertEqual(len(sidecar["audio"]["sha256"]), 64)

    def test_refuses_anything_that_is_not_the_stream_we_asked_for(self):
        store = self.store()

        self.assertIsNone(store.record(b"\xff\xd8\xff" + b"0" * 64, {"kind": "gate_audio"}))
        self.assertEqual(store.status()["failures"], 1)
        self.assertFalse(self.root.exists())

    def test_prunes_the_oldest_pairs_once_over_the_byte_cap(self):
        store = self.store(max_bytes=8 * 1024 * 1024)
        clip = adts(b"\x11" * (3 * 1024 * 1024))

        first = store.record(clip, {"kind": "gate_audio"})
        self.now[0] += timedelta(minutes=1)
        store.record(adts(b"\x22" * (3 * 1024 * 1024)), {"kind": "gate_audio"})
        self.now[0] += timedelta(minutes=1)
        third = store.record(adts(b"\x33" * (3 * 1024 * 1024)), {"kind": "gate_audio"})

        self.assertFalse(first.exists())
        self.assertFalse(first.with_suffix(".json").exists())
        self.assertTrue(third.exists())
        self.assertGreaterEqual(store.status()["pruned"], 1)

    def test_prunes_anything_past_the_retention_window(self):
        store = self.store(retention_days=30)
        old = store.record(adts(), {"kind": "gate_audio"})
        self.assertTrue(old.exists())

        self.now[0] += timedelta(days=31)
        recent = store.record(adts(b"\x44" * 64), {"kind": "gate_audio"})

        self.assertFalse(old.exists())
        self.assertFalse(old.with_suffix(".json").exists())
        self.assertTrue(recent.exists())

    def test_an_oversized_sidecar_keeps_the_label_rather_than_dropping_the_clip(self):
        store = self.store()

        path = store.record(adts(), {
            "schema_version": 1, "kind": "gate_audio", "label": "actuated",
            "actuation": {"relay_activated_at": "2026-09-07T12:00:00+00:00"},
            "camera_event": {"junk": "x" * 200_000},
        })

        sidecar = json.loads(path.with_suffix(".json").read_text())
        self.assertTrue(sidecar["truncated"])
        self.assertEqual(sidecar["label"], "actuated")
        self.assertIsNotNone(sidecar["actuation"])


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "audio"
        self.time = [1000.0]
        self.wall = [datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)]
        self.spawned = []
        self.metrics = {"soc_temp_c": 70.0, "load_1m": 0.4,
                        "mem_available_kib": 2_000_000}

    def recorder(self, *, output=None, returncode=0, **overrides):
        config = AudioCaptureConfig(**{
            "enabled": True, "directory": self.root, "clip_seconds": 40.0,
            "min_interval_seconds": 20.0, **overrides,
        })

        def popen(command, **kwargs):
            self.spawned.append(command)
            return FakeProcess(adts() if output is None else output, returncode)

        return AudioClipRecorder(
            config,
            store=AudioClipStore(self.root, clock=lambda: self.wall[0]),
            popen=popen,
            clock=lambda: self.time[0],
            wall_clock=lambda: self.wall[0],
            host_metrics=lambda: dict(self.metrics),
        )

    # -- the command itself ----------------------------------------------

    def test_the_capture_command_never_decodes_anything(self):
        recorder = self.recorder()
        command = recorder.command

        self.assertIn("-vn", command)
        self.assertEqual(command[command.index("-c:a") + 1], "copy")
        self.assertEqual(command[command.index("-map") + 1], "0:a:0")
        self.assertEqual(command[command.index("-t") + 1], "40")
        self.assertEqual(command[command.index("-i") + 1], DEFAULT_SOURCE)
        # Nothing that would put a frame through a decoder or a scaler.
        for forbidden in ("-vf", "-filter:v", "-c:v", "-s", "-r", "-ar", "-ac", "-af"):
            self.assertNotIn(forbidden, command)

    # -- labelling --------------------------------------------------------

    def test_a_camera_event_with_no_actuation_is_stored_as_a_negative(self):
        recorder = self.recorder()

        self.assertEqual(recorder.on_camera_event(Event()), "requested")
        self.assertTrue(recorder.run_once())

        sidecar = self.only_sidecar()
        self.assertEqual(sidecar["label"], "no_actuation")
        self.assertIsNone(sidecar["actuation"])
        self.assertEqual(sidecar["trigger"], "camera_event")
        self.assertEqual(sidecar["camera_event"]["sequence"], 7)

    def test_an_actuation_during_the_clip_labels_it_and_records_the_pre_roll(self):
        recorder = self.recorder()
        recorder.on_camera_event(Event())
        # The camera fires seconds before the controller decides, so the
        # actuation lands partway into a clip that is already recording.
        self.time[0] += 4.0
        self.assertEqual(
            recorder.note_actuation(
                activated_at=self.wall[0], source="plate", reason="authorised",
                idempotency_key="abc", observed_plate="11wh2571",
                authorised_plate="11WH2571",
            ),
            "attached_pending",
        )
        self.assertTrue(recorder.run_once())

        sidecar = self.only_sidecar()
        self.assertEqual(sidecar["label"], "actuated")
        self.assertEqual(sidecar["actuation"]["idempotency_key"], "abc")
        self.assertEqual(sidecar["actuation"]["authorised_plate"], "11WH2571")
        self.assertEqual(sidecar["actuation"]["offset_seconds"], 4.0)

    def test_an_actuation_with_nothing_recording_starts_its_own_clip(self):
        recorder = self.recorder()

        self.assertEqual(
            recorder.note_actuation(source="command", idempotency_key="remote-1"),
            "requested",
        )
        self.assertTrue(recorder.run_once())

        sidecar = self.only_sidecar()
        self.assertEqual(sidecar["trigger"], "actuation")
        self.assertEqual(sidecar["label"], "actuated")
        self.assertEqual(sidecar["actuation"]["source"], "command")

    def test_the_outcome_lands_on_the_actuation_already_attached(self):
        recorder = self.recorder()
        recorder.on_camera_event(Event())
        recorder.note_actuation(source="plate", idempotency_key="abc")

        self.assertEqual(
            recorder.note_actuation_outcome(
                idempotency_key="abc", status="completed", detail=None, event_id=42,
                reason="authorised",
            ),
            "updated",
        )
        recorder.run_once()

        sidecar = self.only_sidecar()
        self.assertEqual(sidecar["actuation"]["status"], "completed")
        self.assertEqual(sidecar["actuation"]["event_id"], 42)
        # One record, not two: the outcome patches the actuation rather than
        # adding a second one.
        self.assertIsNone(sidecar["actuations"])

    def test_an_outcome_for_an_unknown_actuation_is_reported_not_invented(self):
        recorder = self.recorder()

        self.assertEqual(
            recorder.note_actuation_outcome(idempotency_key="gone", status="completed"),
            "unmatched",
        )

    def test_the_sidecar_says_the_audio_was_copied_and_not_decoded(self):
        recorder = self.recorder()
        recorder.on_camera_event(Event())
        recorder.run_once()

        audio = self.only_sidecar()["audio"]
        self.assertTrue(audio["raw_copy"])
        self.assertFalse(audio["decoded"])
        self.assertEqual(audio["codec"], "aac_lc")
        self.assertEqual(audio["sample_rate_hz"], 16000)
        self.assertEqual(audio["requested_seconds"], 40.0)

    # -- the bounds -------------------------------------------------------

    def test_only_one_capture_runs_at_a_time_and_the_rest_are_coalesced(self):
        recorder = self.recorder()

        self.assertEqual(recorder.on_camera_event(Event()), "requested")
        self.assertEqual(recorder.on_camera_event(Event()), "coalesced")
        self.assertEqual(recorder.on_camera_event(Event()), "coalesced")
        recorder.run_once()

        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(recorder.status()["coalesced"], 2)

    def test_a_second_passage_inside_the_minimum_interval_is_skipped(self):
        recorder = self.recorder()
        recorder.on_camera_event(Event())
        recorder.run_once()

        self.time[0] += 5.0
        self.assertEqual(recorder.on_camera_event(Event()), "skipped_min_interval")
        self.assertFalse(recorder.run_once())
        self.assertEqual(len(self.spawned), 1)

    def test_a_hot_board_skips_the_capture_and_says_why(self):
        recorder = self.recorder()
        self.metrics["soc_temp_c"] = 81.0

        recorder.on_camera_event(Event())

        self.assertFalse(recorder.run_once())
        self.assertEqual(len(self.spawned), 0)
        self.assertEqual(recorder.status()["last_skip_reason"], "hot")

    def test_a_loaded_board_skips_the_capture(self):
        recorder = self.recorder()
        self.metrics["load_1m"] = 3.5

        recorder.on_camera_event(Event())

        self.assertFalse(recorder.run_once())
        self.assertEqual(recorder.status()["last_skip_reason"], "loaded")

    def test_a_board_short_of_memory_skips_the_capture(self):
        recorder = self.recorder()
        self.metrics["mem_available_kib"] = 100_000

        recorder.on_camera_event(Event())

        self.assertFalse(recorder.run_once())
        self.assertEqual(recorder.status()["last_skip_reason"], "low_memory")

    def test_a_skipped_capture_is_never_retried_in_a_loop(self):
        recorder = self.recorder()
        self.metrics["soc_temp_c"] = 81.0
        recorder.on_camera_event(Event())
        recorder.run_once()

        # The request is consumed by the skip, so a second pass does nothing.
        self.assertFalse(recorder.run_once())
        self.assertFalse(recorder.run_once())
        self.assertEqual(len(self.spawned), 0)

    def test_a_skipped_capture_carries_its_label_to_the_next_clip(self):
        recorder = self.recorder(min_interval_seconds=0.0)
        self.metrics["soc_temp_c"] = 81.0
        recorder.note_actuation(source="plate", idempotency_key="abc")
        recorder.run_once()

        self.metrics["soc_temp_c"] = 70.0
        self.time[0] += 1.0
        recorder.on_camera_event(Event())
        self.assertTrue(recorder.run_once())

        sidecar = self.only_sidecar()
        self.assertEqual(sidecar["label"], "actuated")
        self.assertEqual(sidecar["actuation"]["idempotency_key"], "abc")

    def test_a_stale_carried_actuation_is_counted_rather_than_mislabelling(self):
        recorder = self.recorder(min_interval_seconds=0.0)
        self.metrics["soc_temp_c"] = 81.0
        recorder.note_actuation(source="plate", idempotency_key="abc")
        recorder.run_once()

        self.metrics["soc_temp_c"] = 70.0
        self.time[0] += 600.0
        recorder.on_camera_event(Event())
        self.assertTrue(recorder.run_once())

        sidecar = self.only_sidecar()
        self.assertEqual(sidecar["label"], "no_actuation")
        self.assertEqual(recorder.status()["actuations_unmatched"], 1)

    def test_a_clip_over_the_byte_cap_is_dropped_not_stored(self):
        recorder = self.recorder(
            output=adts(b"\x00" * 400_000), max_clip_bytes=64 * 1024,
        )
        recorder.on_camera_event(Event())

        self.assertFalse(recorder.run_once())
        self.assertEqual(recorder.status()["failed"], 1)
        self.assertEqual(list(self.root.glob("*.aac")) if self.root.exists() else [], [])

    def test_a_failing_ffmpeg_is_journalled_and_stores_nothing(self):
        recorder = self.recorder(output=b"", returncode=1)
        recorder.on_camera_event(Event())

        self.assertFalse(recorder.run_once())
        self.assertEqual(recorder.status()["failed"], 1)

    def test_output_that_is_not_the_expected_stream_is_refused(self):
        recorder = self.recorder(output=b"\x00\x00\x00\x18ftypmp42" + b"0" * 64)
        recorder.on_camera_event(Event())

        self.assertFalse(recorder.run_once())
        self.assertEqual(recorder.status()["failed"], 1)

    def test_heartbeat_events_never_start_a_capture(self):
        recorder = self.recorder()

        self.assertEqual(recorder.on_camera_event(Event("Heartbeat")), "skipped_type")
        self.assertFalse(recorder.run_once())
        self.assertEqual(len(self.spawned), 0)

    def test_a_closed_recorder_starts_nothing_further(self):
        recorder = self.recorder()
        recorder.close()

        self.assertEqual(recorder.on_camera_event(Event()), "closed")
        self.assertEqual(recorder.note_actuation(source="plate"), "closed")
        self.assertFalse(recorder.run_once())
        self.assertEqual(len(self.spawned), 0)

    def test_run_forever_returns_when_the_stop_event_is_set(self):
        recorder = self.recorder()
        stop = threading.Event()
        stop.set()

        recorder.run_forever(stop)

        self.assertEqual(len(self.spawned), 0)

    def test_the_child_is_spawned_without_a_shell_and_with_stderr_discarded(self):
        recorder = self.recorder()
        captured = {}

        def popen(command, **kwargs):
            captured.update(kwargs)
            captured["command"] = command
            return FakeProcess(adts())

        recorder._popen = popen
        recorder.on_camera_event(Event())
        recorder.run_once()

        self.assertIsInstance(captured["command"], tuple)
        self.assertEqual(captured["stderr"], subprocess.DEVNULL)
        self.assertEqual(captured["stdin"], subprocess.DEVNULL)
        self.assertTrue(captured["close_fds"])
        self.assertIsNotNone(captured["preexec_fn"])

    def test_a_disabled_recorder_does_nothing_at_either_entry_point(self):
        recorder = AudioClipRecorder(
            AudioCaptureConfig(enabled=False, directory=self.root),
            popen=lambda *a, **k: self.fail("must not spawn"),
        )

        self.assertEqual(recorder.on_camera_event(Event()), "disabled")
        self.assertEqual(recorder.note_actuation(source="plate"), "disabled")
        self.assertFalse(recorder.run_once())

    # -- helpers ----------------------------------------------------------

    def only_sidecar(self) -> dict:
        sidecars = sorted(self.root.glob("*.json"))
        self.assertEqual(len(sidecars), 1, sidecars)
        return json.loads(sidecars[0].read_text())


if __name__ == "__main__":
    unittest.main()
