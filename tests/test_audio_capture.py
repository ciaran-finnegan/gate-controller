import json
import os
import resource
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from gate_controller import audio_capture, net_probe
from gate_controller.audio_capture import (
    AudioCaptureConfig, AudioClipRecorder, AudioClipStore,
    DEFAULT_CHILD_ADDRESS_SPACE_BYTES, DEFAULT_RING_DIRECTORY, DEFAULT_SOURCE,
    RING_RESTART_BACKOFF_SECONDS, load_audio_capture_config,
)

# One AAC-LC frame is 1024 samples, so at the camera's 16 kHz it is 64 ms and
# a 5 s ring segment is 78 of them.
FRAME_SECONDS = 1024 / 16000
SEGMENT_FRAMES = 78
FRAME_BYTES = 64


def adts(payload=b"\x00" * 512):
    """A buffer that begins with a plausible ADTS AAC frame header."""
    return b"\xff\xf1\x50\x80\x00\x1f\xfc" + payload


def adts_frame(fill=b"\x00", *, payload_bytes=FRAME_BYTES - 7, rate_index=8):
    """One real ADTS frame: a header that states its own length, and a payload.

    Deliberately a *valid* frame rather than the plausible header above,
    because the pre-roll walks the length field to find frame boundaries.
    ``rate_index`` 8 is the camera's 16 kHz.
    """
    length = 7 + payload_bytes
    header = bytes((
        0xFF,
        0xF1,                                    # MPEG-4, layer 0, no CRC
        0x40 | (rate_index << 2),                # AAC-LC, rate index, mono
        0x40 | ((length >> 11) & 0x03),
        (length >> 3) & 0xFF,
        ((length & 0x07) << 5) | 0x1F,
        0xFC,                                    # one raw data block
    ))
    return header + fill * payload_bytes


def adts_stream(frames, fill=b"\x00"):
    return adts_frame(fill) * frames


class FakeRingProcess:
    """A stand-in for the always-on ring child: alive until told otherwise."""

    def __init__(self, errors=b""):
        self.stdout = None
        self.stderr = tempfile.TemporaryFile()
        self.stderr.write(errors)
        self.stderr.seek(0)
        self.returncode = None
        self.killed = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode

    def exit(self, code=1):
        """The child died on its own, the way a dropped RTSP session would."""
        self.returncode = code


class FakeProcess:
    """A stand-in ffmpeg that yields bytes once and then reports EOF.

    Backed by a temporary file rather than a pipe so a clip larger than the
    kernel's pipe buffer can be offered without the writer blocking on a
    reader that has not started yet. ``select`` treats a regular file as
    always ready, which is exactly the behaviour the bounded reader needs.
    """

    def __init__(self, output=b"", returncode=0, errors=None):
        handle = tempfile.TemporaryFile()
        handle.write(output)
        handle.seek(0)
        self.stdout = handle
        self.stderr = None
        if errors is not None:
            self.stderr = tempfile.TemporaryFile()
            self.stderr.write(errors)
            self.stderr.seek(0)
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

    def test_a_pre_roll_is_kept_by_default_and_lives_in_ram(self):
        config = load_audio_capture_config({}, None)

        self.assertEqual(config.preroll_seconds, 20.0)
        self.assertEqual(config.ring_directory, DEFAULT_RING_DIRECTORY)
        self.assertEqual(config.ring_directory.parts[:2], ("/", "dev"))

    def test_the_pre_roll_can_be_switched_off_without_switching_capture_off(self):
        config = load_audio_capture_config({"GATE_AUDIO_CAPTURE_PREROLL_SECONDS": "0"}, None)

        self.assertEqual(config.preroll_seconds, 0.0)

    def test_a_pre_roll_longer_than_the_ceiling_is_refused(self):
        with self.assertRaises(ValueError):
            load_audio_capture_config({"GATE_AUDIO_CAPTURE_PREROLL_SECONDS": "600"}, None)
        with self.assertRaises(ValueError):
            load_audio_capture_config({"GATE_AUDIO_CAPTURE_PREROLL_SECONDS": "soon"}, None)
        with self.assertRaises(ValueError):
            load_audio_capture_config({"GATE_AUDIO_CAPTURE_PREROLL_SECONDS": "-5"}, None)

    def test_a_ring_directory_that_is_not_a_directory_of_its_own_is_refused(self):
        with self.assertRaises(ValueError):
            load_audio_capture_config({"GATE_AUDIO_CAPTURE_RING_DIR": "gate-audio-ring"}, None)
        with self.assertRaises(ValueError):
            load_audio_capture_config({"GATE_AUDIO_CAPTURE_RING_DIR": "/"}, None)
        self.assertEqual(
            load_audio_capture_config(
                {"GATE_AUDIO_CAPTURE_RING_DIR": "/run/gate-audio"}, None,
            ).ring_directory,
            Path("/run/gate-audio"),
        )


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


class RecorderHarness:
    """A recorder with every clock, child and metric under the test's control."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "audio"
        self.ring_root = Path(self.temporary.name) / "ring"
        self.time = [1000.0]
        self.wall = [datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)]
        self.spawned = []
        self.ring_spawned = []
        self.ring_processes = []
        self.ring_errors = b""
        self.metrics = {"soc_temp_c": 70.0, "load_1m": 0.4,
                        "mem_available_kib": 2_000_000}

    def recorder(self, *, output=None, returncode=0, errors=None, **overrides):
        config = AudioCaptureConfig(**{
            "enabled": True, "directory": self.root, "clip_seconds": 40.0,
            "min_interval_seconds": 20.0,
            # Never the real /dev/shm from a test, whatever else is overridden.
            "ring_directory": self.ring_root, **overrides,
        })

        def popen(command, **kwargs):
            if "segment" in command:
                self.ring_spawned.append(command)
                self.ring_processes.append(FakeRingProcess(self.ring_errors))
                return self.ring_processes[-1]
            self.spawned.append(command)
            return FakeProcess(
                adts() if output is None else output, returncode, errors=errors,
            )

        return AudioClipRecorder(
            config,
            store=AudioClipStore(self.root, clock=lambda: self.wall[0]),
            popen=popen,
            clock=lambda: self.time[0],
            wall_clock=lambda: self.wall[0],
            host_metrics=lambda: dict(self.metrics),
        )

    # -- helpers ----------------------------------------------------------

    def spawn_kwargs(self, recorder) -> dict:
        """Run one capture and return what the child would have been spawned with."""
        captured = {}

        def popen(command, **kwargs):
            captured.update(kwargs)
            captured["command"] = command
            return FakeProcess(adts())

        recorder._popen = popen
        recorder.on_camera_event(Event())
        recorder.run_once()
        return captured

    def applied_limits(self, preexec) -> list:
        """The RLIMIT_AS the hook would apply, without applying it here.

        The hook runs between fork and exec on the Pi; running it for real in
        the test process would cap this interpreter's own address space and
        renice it.
        """
        limits = []

        def setrlimit(which, limit):
            self.assertEqual(which, resource.RLIMIT_AS)
            limits.append(limit)

        with mock.patch.object(resource, "setrlimit", setrlimit), \
                mock.patch.object(os, "nice", lambda value: 0):
            preexec()
        return limits

    def only_sidecar(self) -> dict:
        sidecars = sorted(self.root.glob("*.json"))
        self.assertEqual(len(sidecars), 1, sidecars)
        return json.loads(sidecars[0].read_text())


class RecorderTests(RecorderHarness, unittest.TestCase):
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

    def test_the_child_is_spawned_without_a_shell_and_with_its_stderr_kept(self):
        recorder = self.recorder()
        captured = self.spawn_kwargs(recorder)

        self.assertIsInstance(captured["command"], tuple)
        # Kept, not discarded: the tail of it is what makes a failure
        # diagnosable from the journal.
        self.assertEqual(captured["stderr"], subprocess.PIPE)
        self.assertEqual(captured["stdin"], subprocess.DEVNULL)
        self.assertTrue(captured["close_fds"])
        self.assertIsNotNone(captured["preexec_fn"])

    # -- the child's bounds -----------------------------------------------

    def test_the_audio_child_gets_an_address_space_ffmpeg_can_load_into(self):
        """128 MiB could not even map ffmpeg's shared libraries (Pi, 2026-09-08)."""
        recorder = self.recorder()

        limits = self.applied_limits(self.spawn_kwargs(recorder)["preexec_fn"])

        self.assertEqual(limits, [(1024 * 1024 * 1024, 1024 * 1024 * 1024)])

    def test_the_probe_children_keep_their_own_much_smaller_limit(self):
        """Only the audio child is raised; ping needs nothing like this."""
        self.assertEqual(net_probe.CHILD_ADDRESS_SPACE_BYTES, 64 * 1024 * 1024)
        self.assertLess(
            net_probe.CHILD_ADDRESS_SPACE_BYTES, DEFAULT_CHILD_ADDRESS_SPACE_BYTES,
        )

    def test_the_address_space_limit_is_tunable_but_never_unbounded(self):
        config = load_audio_capture_config(
            {"GATE_AUDIO_CAPTURE_MAX_ADDRESS_SPACE_BYTES": str(512 * 1024 * 1024)}, None,
        )
        self.assertEqual(config.child_address_space_bytes, 512 * 1024 * 1024)

        recorder = AudioClipRecorder(
            AudioCaptureConfig(enabled=True, directory=self.root,
                               child_address_space_bytes=512 * 1024 * 1024),
            store=AudioClipStore(self.root),
            popen=lambda command, **kwargs: FakeProcess(adts()),
            clock=lambda: self.time[0], wall_clock=lambda: self.wall[0],
            host_metrics=lambda: dict(self.metrics),
        )
        self.assertEqual(
            self.applied_limits(self.spawn_kwargs(recorder)["preexec_fn"]),
            [(512 * 1024 * 1024, 512 * 1024 * 1024)],
        )
        with self.assertRaises(ValueError):
            load_audio_capture_config(
                {"GATE_AUDIO_CAPTURE_MAX_ADDRESS_SPACE_BYTES": "134217728"}, None,
            )
        with self.assertRaises(ValueError):
            load_audio_capture_config(
                {"GATE_AUDIO_CAPTURE_MAX_ADDRESS_SPACE_BYTES": "17179869184"}, None,
            )

    # -- diagnosing a failure ---------------------------------------------

    def test_a_failing_child_journals_the_tail_of_what_it_complained_about(self):
        message = (b"ffmpeg: error while loading shared libraries: "
                   b"libcodec2.so.1.0: failed to map segment from shared object\n")
        recorder = self.recorder(output=b"", returncode=1, errors=message)
        recorder.on_camera_event(Event())

        with self.assertLogs("gate_controller.audio_capture", level="WARNING") as logs:
            self.assertFalse(recorder.run_once())

        line = "\n".join(logs.output)
        self.assertIn("outcome=failed reason=exit_status", line)
        self.assertIn("failed to map segment from shared object", line)
        self.assertEqual(len(logs.output), 1, logs.output)
        self.assertIn(
            "libcodec2.so.1.0", recorder.status()["last_capture"]["stderr"],
        )

    def test_a_chatty_child_cannot_flood_the_journal(self):
        recorder = self.recorder(output=b"", returncode=1, errors=b"x\n" * 100_000)
        recorder.on_camera_event(Event())

        with self.assertLogs("gate_controller.audio_capture", level="WARNING") as logs:
            recorder.run_once()

        tail = recorder.status()["last_capture"]["stderr"]
        self.assertEqual(len(tail), 200)
        self.assertNotIn("\n", tail)
        self.assertEqual(len(logs.output), 1)

    def test_a_child_that_said_nothing_still_journals_one_line(self):
        recorder = self.recorder(output=b"", returncode=1, errors=b"")
        recorder.on_camera_event(Event())

        with self.assertLogs("gate_controller.audio_capture", level="WARNING") as logs:
            recorder.run_once()

        self.assertIn("stderr=-", "\n".join(logs.output))
        self.assertIsNone(recorder.status()["last_capture"]["stderr"])

    def test_a_real_child_that_floods_stderr_neither_blocks_nor_loses_its_clip(self):
        """Over a pipe buffer of stderr, with no reader, would wedge the child.

        The only test here that spawns a real process, because it is the one
        thing a stand-in cannot show: stderr is a pipe now, so it has to be
        read in the same loop as stdout or a chatty child blocks on the write
        and never reaches the -t it was given.
        """
        noise = ("i=0; while [ $i -lt 5000 ]; do "
                 "printf 'noisy line %d padded out to fill the pipe buffer\\n' $i >&2; "
                 "i=$((i+1)); done; ")
        recorder = self.recorder()
        recorder._popen = subprocess.Popen
        script = noise + r"printf '\377\361\120\200\000\037\374'; head -c 4096 /dev/zero"
        with mock.patch.object(
            AudioClipRecorder, "command",
            new=property(lambda self: ("/bin/sh", "-c", script)),
        ), mock.patch.object(audio_capture, "_child_limiter", lambda limit: None):
            recorder.on_camera_event(Event())
            self.assertTrue(recorder.run_once())

        self.assertEqual(recorder.status()["captured"], 1)
        self.assertEqual(len(list(self.root.glob("*.aac"))), 1)

    def test_a_disabled_recorder_does_nothing_at_either_entry_point(self):
        recorder = AudioClipRecorder(
            AudioCaptureConfig(enabled=False, directory=self.root),
            popen=lambda *a, **k: self.fail("must not spawn"),
        )

        self.assertEqual(recorder.on_camera_event(Event()), "disabled")
        self.assertEqual(recorder.note_actuation(source="plate"), "disabled")
        self.assertFalse(recorder.run_once())

class AdtsFramingTests(unittest.TestCase):
    """The one assumption the pre-roll rests on: ADTS joins byte-wise.

    Every frame carries its own header and its own length and the format has
    no file-level index or timestamps, so two streams from one source
    concatenate -- provided the cut is on a frame boundary, which is what
    these walk the headers to guarantee.
    """

    def test_two_streams_join_into_one_stream_of_the_same_frames(self):
        joined = adts_stream(3, b"\x01") + adts_stream(2, b"\x02")

        frames, end, rate = audio_capture._adts_frames(joined)

        self.assertEqual(len(frames), 5)
        self.assertEqual(end, len(joined))
        self.assertEqual(rate, 16000)
        self.assertTrue(audio_capture._is_adts_aac(joined))

    def test_a_half_written_frame_is_never_carried_into_a_clip(self):
        """The newest ring segment is being written while it is read."""
        torn = adts_stream(4) + adts_frame()[:20]

        body, samples, _rate = audio_capture._adts_tail(torn)

        self.assertEqual(body, adts_stream(4))
        self.assertEqual(samples, 4 * 1024)

    def test_bytes_that_are_not_this_stream_contribute_nothing(self):
        self.assertEqual(audio_capture._adts_tail(b"\x00" * 512)[0], b"")
        self.assertEqual(audio_capture._adts_tail(b"")[0], b"")
        self.assertEqual(audio_capture._adts_tail(adts())[0], b"")

    def test_the_tail_is_taken_in_whole_frames_inside_both_budgets(self):
        stream = adts_stream(10)

        by_samples, samples, _ = audio_capture._adts_tail(stream, samples=3 * 1024)
        by_bytes, _, _ = audio_capture._adts_tail(stream, max_bytes=3 * FRAME_BYTES + 5)

        self.assertEqual(len(by_samples), 3 * FRAME_BYTES)
        self.assertEqual(samples, 3 * 1024)
        self.assertEqual(len(by_bytes), 3 * FRAME_BYTES)
        # The *last* frames, because the pre-roll wants the audio nearest the
        # trigger, not the audio furthest from it.
        self.assertEqual(by_samples, stream[-3 * FRAME_BYTES:])


class PrerollRingTests(RecorderHarness, unittest.TestCase):
    """The ring that makes issue #106 measurable: audio from before the alarm."""

    def ring_recorder(self, **overrides):
        recorder = self.recorder(**{"preroll_seconds": 20.0, **overrides})
        self.addCleanup(recorder.close)
        return recorder

    def write_segments(self, count, *, age_offset=0.0, frames=SEGMENT_FRAMES,
                       start=0, fill=b"\x00", tail=b""):
        """Segments as the ring child would have left them: newest last written.

        ``-segment_wrap`` cycles the names, so the modification time is the
        only thing that says which is newest -- which is what the harvest
        sorts on and what its staleness cut reads.
        """
        self.ring_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        now = self.wall[0].timestamp() - age_offset
        written = []
        for index in range(count):
            path = self.ring_root / ("seg%03d.aac" % (start + index))
            body = adts_stream(frames, fill)
            if tail and index == count - 1:
                body += tail
            path.write_bytes(body)
            # The oldest was last written `count - 1` segments ago.
            when = now - (count - 1 - index) * 5.0
            os.utime(path, (when, when))
            written.append(path)
        return written

    # -- the ring child ---------------------------------------------------

    def test_the_ring_command_never_decodes_anything_either(self):
        recorder = self.ring_recorder()
        command = recorder._ring.command

        self.assertIn("-vn", command)
        self.assertEqual(command[command.index("-c:a") + 1], "copy")
        self.assertEqual(command[command.index("-map") + 1], "0:a:0")
        self.assertEqual(command[command.index("-f") + 1], "segment")
        self.assertEqual(command[command.index("-segment_format") + 1], "adts")
        self.assertEqual(command[command.index("-segment_time") + 1], "5")
        self.assertEqual(command[command.index("-i") + 1], DEFAULT_SOURCE)
        # Bounded RAM: the number of files is fixed, so the ring cannot grow
        # however long the child runs.
        self.assertEqual(command[command.index("-segment_wrap") + 1], "6")
        self.assertEqual(command[-1], str(self.ring_root / "seg%03d.aac"))
        for forbidden in ("-vf", "-filter:v", "-c:v", "-s", "-r", "-ar", "-ac", "-af"):
            self.assertNotIn(forbidden, command)

    def test_the_ring_holds_the_pre_roll_and_no_more_of_it(self):
        recorder = self.ring_recorder(preroll_seconds=40.0)

        command = recorder._ring.command
        self.assertEqual(recorder._ring.segments, 10)
        self.assertEqual(command[command.index("-segment_wrap") + 1], "10")

    def test_one_ring_child_is_started_and_then_left_alone(self):
        recorder = self.ring_recorder()

        recorder.supervise_ring()
        for _ in range(5):
            self.time[0] += 1.0
            recorder.supervise_ring()

        self.assertEqual(len(self.ring_spawned), 1)
        self.assertTrue(recorder.status()["ring"]["running"])
        self.assertEqual(recorder.status()["ring"]["starts"], 1)
        # And it is not a capture: no clip, no capture child, no slot taken.
        self.assertEqual(len(self.spawned), 0)
        self.assertEqual(recorder.status()["capturing"], False)

    def test_the_ring_child_is_spawned_with_the_same_bounds_as_the_capture(self):
        captured = {}

        def popen(command, **kwargs):
            captured.update(kwargs)
            captured["command"] = command
            return FakeRingProcess()

        recorder = self.ring_recorder()
        recorder._ring._popen = popen
        recorder.supervise_ring()

        self.assertIsInstance(captured["command"], tuple)
        self.assertEqual(captured["stdin"], subprocess.DEVNULL)
        self.assertEqual(captured["stderr"], subprocess.PIPE)
        self.assertTrue(captured["close_fds"])
        self.assertEqual(
            self.applied_limits(captured["preexec_fn"]),
            [(DEFAULT_CHILD_ADDRESS_SPACE_BYTES, DEFAULT_CHILD_ADDRESS_SPACE_BYTES)],
        )

    def test_a_dead_ring_child_is_restarted_but_never_in_a_tight_loop(self):
        recorder = self.ring_recorder()
        recorder.supervise_ring()

        with self.assertLogs("gate_controller.audio_capture", level="WARNING"):
            self.ring_processes[-1].exit(1)
            recorder.supervise_ring()

        self.assertEqual(len(self.ring_spawned), 1)
        self.assertFalse(recorder.status()["ring"]["running"])
        self.assertEqual(recorder.status()["ring"]["failures"], 1)

        # Not before the backoff has passed, and then exactly once.
        recorder.supervise_ring()
        self.assertEqual(len(self.ring_spawned), 1)
        self.time[0] += RING_RESTART_BACKOFF_SECONDS
        recorder.supervise_ring()
        recorder.supervise_ring()
        self.assertEqual(len(self.ring_spawned), 2)
        self.assertTrue(recorder.status()["ring"]["running"])

    def test_each_further_failure_waits_longer_than_the_last(self):
        recorder = self.ring_recorder()
        waits = []
        for _ in range(4):
            recorder.supervise_ring()
            self.ring_processes[-1].exit(1)
            with self.assertLogs("gate_controller.audio_capture", level="WARNING"):
                recorder.supervise_ring()
            started = len(self.ring_spawned)
            waited = 0.0
            while len(self.ring_spawned) == started and waited < 300:
                self.time[0] += 1.0
                waited += 1.0
                recorder.supervise_ring()
            waits.append(waited)

        self.assertEqual(waits, sorted(waits))
        self.assertLess(waits[0], waits[-1])

    def test_a_ring_child_that_complained_says_so_in_the_journal(self):
        self.ring_errors = b"Server returned 404 Not Found\n"
        recorder = self.ring_recorder()
        recorder.supervise_ring()
        self.ring_processes[-1].exit(1)

        with self.assertLogs("gate_controller.audio_capture", level="WARNING") as logs:
            recorder.supervise_ring()

        self.assertIn("gate_audio_ring outcome=exited", "\n".join(logs.output))
        self.assertIn("404 Not Found", recorder.status()["ring"]["last_stderr"])

    # -- the pre-roll itself ----------------------------------------------

    def test_a_camera_event_clip_now_begins_before_the_camera_event(self):
        """The whole of issue #106: the approach is in the clip, labelled free."""
        recorder = self.ring_recorder(output=adts_stream(20, b"\x33"))
        recorder.supervise_ring()
        # Five segments in the ring; only the newest four are inside 20 s.
        self.write_segments(5, fill=b"\x11")

        recorder.on_camera_event(Event())
        self.assertTrue(recorder.run_once())

        sidecar = self.only_sidecar()
        clip = sorted(self.root.glob("*.aac"))[0].read_bytes()
        self.assertEqual(sidecar["audio"]["preroll_seconds"], 19.97)
        self.assertEqual(sidecar["audio"]["preroll_requested_seconds"], 20.0)
        self.assertEqual(sidecar["audio"]["preroll_segments"], 4)
        # The pre-roll is in front of the capture, frame-aligned, and the whole
        # thing is still one ADTS stream.
        self.assertEqual(len(clip), 4 * SEGMENT_FRAMES * FRAME_BYTES + 20 * FRAME_BYTES)
        self.assertTrue(clip.startswith(adts_frame(b"\x11")))
        self.assertTrue(clip.endswith(adts_stream(20, b"\x33")))
        frames, end, _rate = audio_capture._adts_frames(clip)
        self.assertEqual(end, len(clip))
        self.assertEqual(len(frames), 4 * SEGMENT_FRAMES + 20)

    def test_the_sidecar_says_where_the_trigger_is_in_the_clip(self):
        recorder = self.ring_recorder()
        recorder.supervise_ring()
        self.write_segments(4)

        recorder.on_camera_event(Event())
        self.time[0] += 4.0
        recorder.note_actuation(source="plate", idempotency_key="abc")
        self.assertTrue(recorder.run_once())

        sidecar = self.only_sidecar()
        # t=0 is still the trigger, so the actuation offset is unchanged...
        self.assertEqual(sidecar["actuation"]["offset_seconds"], 4.0)
        # ...and this is what locates t=0 in a clip that now starts earlier.
        self.assertEqual(sidecar["audio"]["trigger_offset_seconds"], 15.97)
        self.assertEqual(sidecar["audio"]["preroll_seconds"], 19.97)
        self.assertEqual(sidecar["captured_at"], "2026-09-07T12:00:00+00:00")
        self.assertEqual(sidecar["clip_started_at"], "2026-09-07T11:59:40.030000+00:00")

    def test_a_young_ring_gives_what_it_has_and_says_how_much(self):
        recorder = self.ring_recorder()
        recorder.supervise_ring()
        self.write_segments(1)

        recorder.on_camera_event(Event())
        self.assertTrue(recorder.run_once())

        sidecar = self.only_sidecar()
        self.assertEqual(sidecar["audio"]["preroll_seconds"], 4.99)
        self.assertEqual(sidecar["audio"]["preroll_segments"], 1)
        self.assertEqual(recorder.status()["preroll_absent"], 0)

    def test_no_ring_child_and_no_pre_roll_when_it_is_switched_off(self):
        recorder = self.ring_recorder(preroll_seconds=0.0)
        recorder.supervise_ring()
        self.write_segments(4)

        recorder.on_camera_event(Event())
        self.assertTrue(recorder.run_once())

        self.assertEqual(len(self.ring_spawned), 0)
        self.assertFalse(recorder.status()["ring"]["enabled"])
        sidecar = self.only_sidecar()
        self.assertEqual(sidecar["audio"]["preroll_seconds"], 0.0)
        self.assertEqual(sidecar["audio"]["preroll_requested_seconds"], 0.0)
        self.assertEqual(sidecar["clip_started_at"], sidecar["captured_at"])
        self.assertEqual(recorder.status()["preroll_absent"], 0)

    def test_segments_left_by_a_ring_that_is_not_running_are_never_used(self):
        """A clip must not be welded onto audio from before the gap."""
        recorder = self.ring_recorder()
        self.write_segments(4)

        recorder.on_camera_event(Event())
        self.assertTrue(recorder.run_once())

        clip = sorted(self.root.glob("*.aac"))[0].read_bytes()
        self.assertEqual(clip, adts())
        self.assertEqual(self.only_sidecar()["audio"]["preroll_seconds"], 0.0)
        self.assertEqual(recorder.status()["preroll_absent"], 1)
        self.assertEqual(recorder.status()["last_capture"]["preroll_seconds"], 0.0)

    def test_stale_segments_are_never_spliced_onto_a_later_event(self):
        recorder = self.ring_recorder()
        recorder.supervise_ring()
        self.write_segments(4, age_offset=600.0)

        recorder.on_camera_event(Event())
        self.assertTrue(recorder.run_once())

        self.assertEqual(self.only_sidecar()["audio"]["preroll_seconds"], 0.0)
        self.assertEqual(recorder.status()["preroll_absent"], 1)

    def test_a_segment_being_written_contributes_its_whole_frames_only(self):
        recorder = self.ring_recorder(output=adts_stream(2, b"\x33"))
        recorder.supervise_ring()
        # The newest segment is half a frame short of a frame boundary.
        self.write_segments(1, tail=adts_frame(b"\x22")[:31])

        recorder.on_camera_event(Event())
        self.assertTrue(recorder.run_once())

        clip = sorted(self.root.glob("*.aac"))[0].read_bytes()
        self.assertEqual(len(clip), (SEGMENT_FRAMES + 2) * FRAME_BYTES)
        _frames, end, _rate = audio_capture._adts_frames(clip)
        self.assertEqual(end, len(clip))

    def test_the_pre_roll_never_pushes_the_clip_past_the_byte_cap(self):
        recorder = self.ring_recorder(
            output=adts_stream(937, b"\x33"), max_clip_bytes=64 * 1024,
        )
        recorder.supervise_ring()
        self.write_segments(5)

        recorder.on_camera_event(Event())
        self.assertTrue(recorder.run_once())

        clip = sorted(self.root.glob("*.aac"))[0].read_bytes()
        self.assertLessEqual(len(clip), 64 * 1024)
        self.assertEqual(len(clip), (937 + 87) * FRAME_BYTES)
        # The pre-roll gave way, oldest frames first, and said what it became:
        # 87 whole frames of the 20 s it wanted, not a byte more.
        self.assertEqual(self.only_sidecar()["audio"]["preroll_seconds"], 5.57)
        _frames, end, _rate = audio_capture._adts_frames(clip)
        self.assertEqual(end, len(clip))

    def test_neither_trigger_ever_waits_for_the_ring(self):
        """The relay thread calls note_actuation while the relay is energised."""
        recorder = self.ring_recorder()
        recorder.supervise_ring()
        self.write_segments(4)
        reads = []
        original = audio_capture.AudioPrerollRing.harvest
        with mock.patch.object(
            audio_capture.AudioPrerollRing, "harvest",
            lambda self, **kwargs: reads.append(kwargs) or original(self, **kwargs),
        ):
            recorder.on_camera_event(Event())
            recorder.note_actuation(source="plate", idempotency_key="abc")
            self.assertEqual(reads, [])
            recorder.run_once()

        self.assertEqual(len(reads), 1)

    # -- the governor -----------------------------------------------------

    def test_a_hot_board_stops_the_ring_and_a_cool_one_starts_it_again(self):
        recorder = self.ring_recorder()
        recorder.supervise_ring()
        self.write_segments(4)
        child = self.ring_processes[-1]

        self.metrics["soc_temp_c"] = 81.0
        self.time[0] += 5.0
        recorder.supervise_ring()

        self.assertTrue(child.killed)
        self.assertFalse(recorder.status()["ring"]["running"])
        self.assertEqual(recorder.status()["ring"]["stopped_reason"], "hot")
        # And the ring is emptied, so nothing from before the gap survives it.
        self.assertEqual(list(self.ring_root.glob("*.aac")), [])

        self.metrics["soc_temp_c"] = 70.0
        self.time[0] += 5.0
        recorder.supervise_ring()

        self.assertTrue(recorder.status()["ring"]["running"])
        self.assertEqual(len(self.ring_spawned), 2)

    def test_the_governor_is_not_read_on_every_tick(self):
        reads = []
        recorder = self.ring_recorder()
        recorder._host_metrics = lambda: reads.append(1) or dict(self.metrics)

        for _ in range(10):
            self.time[0] += 1.0
            recorder.supervise_ring()

        self.assertLessEqual(len(reads), 3)

    def test_a_loaded_board_stops_the_ring_too(self):
        recorder = self.ring_recorder()
        recorder.supervise_ring()

        self.metrics["load_1m"] = 3.5
        self.time[0] += 5.0
        recorder.supervise_ring()

        self.assertEqual(recorder.status()["ring"]["stopped_reason"], "loaded")

    # -- the directory ----------------------------------------------------

    def test_the_ring_directory_is_private_and_emptied_on_start(self):
        stale = self.ring_root / "seg002.aac"
        keep = self.ring_root / "notes.txt"
        self.ring_root.mkdir(mode=0o700, parents=True)
        stale.write_bytes(adts_stream(4))
        keep.write_bytes(b"not the ring's to delete")

        self.ring_recorder().supervise_ring()

        self.assertFalse(stale.exists())
        self.assertTrue(keep.exists())
        self.assertEqual(oct(self.ring_root.stat().st_mode & 0o777), oct(0o700))

    def test_closing_the_recorder_takes_the_ring_child_with_it(self):
        recorder = self.ring_recorder()
        recorder.supervise_ring()
        self.write_segments(4)
        child = self.ring_processes[-1]

        recorder.close()

        self.assertTrue(child.killed)
        self.assertEqual(list(self.ring_root.glob("*.aac")), [])
        self.assertFalse(recorder.status()["ring"]["running"])
        # And a closed recorder starts nothing further, ring included.
        recorder.supervise_ring()
        self.assertEqual(len(self.ring_spawned), 1)

    def test_the_owning_thread_stops_the_ring_when_it_leaves(self):
        recorder = self.ring_recorder()
        recorder.supervise_ring()
        child = self.ring_processes[-1]
        stop = threading.Event()
        stop.set()

        recorder.run_forever(stop)

        self.assertTrue(child.killed)

    def test_the_status_says_what_the_ring_is_doing(self):
        recorder = self.ring_recorder()
        recorder.supervise_ring()

        ring = recorder.status()["ring"]

        self.assertEqual(ring["enabled"], True)
        self.assertEqual(ring["running"], True)
        self.assertEqual(ring["directory"], str(self.ring_root))
        self.assertEqual(ring["preroll_seconds"], 20.0)
        self.assertEqual(ring["segments"], 6)
        self.assertEqual(ring["starts"], 1)
        self.assertEqual(ring["failures"], 0)
        self.assertIsNone(ring["stopped_reason"])
        self.assertEqual(recorder.status()["preroll_seconds"], 20.0)


if __name__ == "__main__":
    unittest.main()
