"""30 Sep - 4 Oct 2026: the gate network slowed, and every passage was refused.

The camera FTPs a 4K still to the Pi when it sees a vehicle. Camera and Pi
share a switch at the gate, and a 1.0-1.9 MB still normally lands at
1,300-1,600 KB/s. From about 21:52 UTC on 30 September the same uploads ran
at 3-40 KB/s and took 30-50 s or more. vsftpd logged nearly every one
``OK UPLOAD``; the controller had given each upload a flat five seconds
(100 polls x 50 ms), rejected every one ``upload_incomplete``, and never ran
the on-device reader on any of them. All 24 passages over three days were
refused, and the camera's webhook did not arrive in that period either.

Two halves, and both matter:

* an upload that is still arriving is waited for, and a still that completes
  goes through the pipeline instead of being thrown away unread;
* a still that completes *late* is judged by the processor's freshness rule
  (``GATE_MAX_IMAGE_AGE_SECONDS``, measured from the moment the upload was
  first seen), so the relay never fires for a car that may have gone -- the
  relay is on the operator's step-by-step input, and a pulse into an open or
  moving gate closes or stops it.

The production-path tests run the real ``run_worker`` with the real watchdog
observer, burst collector, ``GateProcessor``, on-device recogniser, cloud
client, store and actuation coordinator. The fakes sit at the boundaries: the
ONNX engine, the HTTP session (unreachable), the relay, and the clock -- the
controller's clocks run on real time plus an offset the test moves forward, so
40 s of upload take a second of the suite.
"""
import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from threading import Lock, Thread
from time import monotonic, sleep, time
from unittest import mock

import requests

from gate_controller import __main__ as gate_main
from gate_controller import worker
from gate_controller.local_recognizer import LocalRecognizer, LocalRecognizerConfig
from gate_controller.ocr import PlateRecognizerClient
from gate_controller.processor import GateProcessor
from gate_controller.store import LocalStore
from gate_controller.worker import (
    BurstCollector, CompletedImageHandler, StartupReconciler, run_worker,
)
from tests.test_local_recognizer import FakeSession
from tests.test_processor import RecordingRelay
from tests.test_sweep_pipeline import (
    AUTHORISED, REGION, CapturedLogs, ScriptedEngine, StopHook, digest, frame, wait_for,
)


class FakeTime:
    """Real time plus an offset the test moves: every controller clock reads it."""

    def __init__(self):
        self._lock = Lock()
        self._offset = 0.0

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._offset += seconds

    @property
    def offset(self) -> float:
        with self._lock:
            return self._offset

    def monotonic(self) -> float:
        return monotonic() + self.offset

    def wall(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=self.offset)

    def epoch(self) -> float:
        return time() + self.offset


class UnreachableCloud(FakeSession):
    """The cloud plate reader with no route to it: every request fails at once."""

    def __init__(self):
        super().__init__([])
        self.posted = 0

    def post(self, *args, **kwargs):
        self.posted += 1
        raise requests.exceptions.ConnectionError("no route to host")


class MutableClock:
    def __init__(self, value: float = 0.0):
        self.value = value

    def __call__(self):
        return self.value


class RecordingCollector:
    def __init__(self):
        self.paths = []
        self.received_at = []

    def add(self, path, received_at=None):
        self.paths.append(path)
        self.received_at.append(received_at)
        return len(self.paths) == 1


class ClosedEvent:
    def __init__(self, path):
        self.src_path = str(path)
        self.dest_path = str(path)
        self.is_directory = False


def chunks(data: bytes, count: int):
    size = -(-len(data) // count)
    return [data[index:index + size] for index in range(0, len(data), size)]


def stamp(path: Path, epoch: float) -> None:
    """Set the mtime the kernel would have stamped on the write at ``epoch``."""
    nanoseconds = int(epoch * 1e9)
    os.utime(path, ns=(nanoseconds, nanoseconds))


class SlowUploadGate:
    """The controller as ``__main__`` wires the FTP path, boundaries faked.

    No trigger capture and no hot stream: in the incident the webhook never
    arrived, so the FTP still was the only trigger there was.
    """

    def __init__(self, test, *, answers, max_image_age=8.0, decision_timeout=6.0,
                 before_start=None):
        self.test = test
        self.time = FakeTime()
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.uploads = root / "uploads"
        self.uploads.mkdir()
        if before_start is not None:
            before_start(self)
        self.engine = ScriptedEngine(answers)
        self.relay_calls = []
        self.results = []
        self.skipped = []
        self._lock = Lock()
        self.local = LocalRecognizer(
            LocalRecognizerConfig(
                mode="active", cloud="fallback", min_confidence=0.5,
                model_dir=Path("/var/lib/gate-controller/models"),
            ),
            engine_factory=lambda config: self.engine, plate_region=REGION,
        )
        self.local.start()
        assert self.local.wait_ready(5)
        self.cloud = UnreachableCloud()
        self.client = PlateRecognizerClient(
            "token", session=self.cloud, local_recognizer=self.local,
            authorised=lambda: AUTHORISED, max_upload_width=1920, plate_region=REGION,
        )
        self.store = LocalStore(root / "gate.db")
        self.processor = GateProcessor(
            recognizer=self.client, store=self.store,
            relay=RecordingRelay(self.relay_calls), authorised=lambda: AUTHORISED,
            clock=self.time.wall, decision_clock=self.time.monotonic,
            telemetry_clock=self.time.monotonic, telemetry_wall_clock=self.time.wall,
            max_image_age=timedelta(seconds=max_image_age),
            decision_timeout=decision_timeout,
        )
        self._patches = ExitStack()
        self._patches.enter_context(mock.patch.object(
            worker, "CompletedImageHandler", partial(
                CompletedImageHandler, clock=self.time.monotonic,
                arrival_clock=self.time.wall,
            ),
        ))
        self._patches.enter_context(mock.patch.object(
            worker, "BurstCollector", partial(
                BurstCollector, clock=self.time.monotonic,
                arrival_clock=self.time.wall, wall_clock=self.time.wall,
            ),
        ))
        self._patches.enter_context(mock.patch.object(
            worker, "StartupReconciler", partial(StartupReconciler, clock=self.time.epoch),
        ))
        self.hook = StopHook()
        self._thread = Thread(target=self._run, daemon=True, name="slow-upload-gate")
        self._thread.start()
        assert self.hook.ready.wait(10), "run_worker never started its background workers"

    def _run(self):
        run_worker(
            self.uploads, self.processor.process, quiet_window=0.1, poll_interval=0.01,
            max_image_age=8.0, background_workers=(self.hook,),
            prepare=self.processor.prepare, on_result=self._on_result,
            on_skipped=self._on_skipped,
        )

    def _on_result(self, paths, result):
        with self._lock:
            self.results.append(result)

    def _on_skipped(self, paths, reason, received_at=None, **_options):
        # What `__main__` records for a frame rejected before any read.
        with self._lock:
            self.skipped.append(reason)
        self.processor.record_skipped(paths, reason, received_at)

    def pipeline_bytes(self, data: bytes) -> bytes:
        """The band exactly as the pipeline's own local pass encodes it."""
        with tempfile.NamedTemporaryFile(suffix=".jpg") as handle:
            handle.write(data)
            handle.flush()
            upload, _geometry = self.client._open_upload(Path(handle.name))
            try:
                return upload.read()
            finally:
                upload.close()

    def outcomes(self):
        with self._lock:
            return [(result.opened, result.reason) for result in self.results]

    def rejections(self):
        with self._lock:
            return list(self.skipped)

    def upload(self, name: str, data: bytes, *, seconds: float, pieces: int, logs):
        """The camera's FTP client: create the file, then trickle it in.

        vsftpd creates the file empty on STOR and the bytes follow, so the
        controller first sees it at 0 bytes. Every write is stamped with the
        controller's own (moved) time, as the kernel would.
        """
        path = self.uploads / name
        step = seconds / pieces
        with path.open("wb") as handle:
            handle.flush()
            stamp(path, self.time.epoch())
            self.test.assertTrue(
                wait_for(lambda: "stage=filesystem_ingress" in logs.text(), 10.0),
                "the observer never saw the upload start",
            )
            parts = chunks(data, pieces)
            for index, piece in enumerate(parts):
                self.time.advance(step)
                handle.write(piece)
                handle.flush()
                stamp(path, self.time.epoch())
                if index < len(parts) - 1:
                    # Let the worker's 10 ms loop look at it at least once.
                    sleep(0.03)
            # vsftpd closes the file straight after its last write.
        return path

    def close(self):
        if self.hook.stop_event is not None:
            self.hook.stop_event.set()
        self._thread.join(15)
        self._patches.close()
        self.local.close()
        self.directory.cleanup()


class SlowUploadPassageTests(unittest.TestCase):
    """Through the path production takes, with the cloud unreachable."""

    def setUp(self):
        self.gate = None

    def tearDown(self):
        if self.gate is not None:
            self.gate.close()

    def _gate(self, still, score=0.97, **options):
        answers = {}
        self.gate = SlowUploadGate(self, answers=answers, **options)
        # The on-device reader reads the authorised plate off this still.
        answers[digest(self.gate.pipeline_bytes(still))] = ("10CE1990", score)
        return self.gate

    def test_a_still_that_takes_40_s_to_arrive_is_decided_and_refused_as_stale(self):
        """The incident's upload: 40 s at a trickle, complete in the end.

        Before: ``upload_incomplete`` at five seconds, nothing read, nothing
        known. Now: the still is waited for and reaches the processor, and the
        freshness rule -- 40 s against `GATE_MAX_IMAGE_AGE_SECONDS=8` --
        refuses it ``stale_burst`` without a read, a cloud request or a
        pulse. The car that still was of may have left, or been let in by a
        fob 30 s ago; a pulse now would close the gate on whoever is in it.
        """
        still = frame(401)
        gate = self._gate(still)
        with CapturedLogs() as logs:
            gate.upload("RLC-811A_00_20261001221500.jpg", still, seconds=40.0,
                        pieces=20, logs=logs)
            self.assertTrue(
                wait_for(lambda: gate.outcomes() or gate.rejections(), 10.0),
                f"the still was never decided:\n{logs.text()}",
            )
            sleep(0.3)

        self.assertEqual(gate.rejections(), [], "a complete upload was rejected unread")
        self.assertEqual(gate.outcomes(), [(False, "stale_burst")])
        self.assertEqual(gate.relay_calls, [], "the relay fired for a 40 s old still")
        self.assertEqual(gate.cloud.posted, 0)
        self.assertNotIn("upload_incomplete", logs.text())
        self.assertEqual(logs.text().count("stage=upload_arriving"), 1)
        self.assertEqual(logs.text().count("stage=upload_completed"), 1)
        completed = next(
            line for line in logs.lines if "stage=upload_completed" in line
        )
        upload_ms = int(completed.split("upload_ms=")[1].split()[0])
        self.assertGreaterEqual(upload_ms, 40_000)
        self.assertIn("unobserved_ms=0", completed)

    def test_a_still_that_takes_5_5_s_to_arrive_still_opens_on_the_device_read(self):
        """Slow, but inside the freshness window: the gate opens, offline.

        5.5 s was already past the old flat five. With the cloud
        unreachable the on-device reader decides it, once.
        """
        still = frame(402)
        gate = self._gate(still)
        with CapturedLogs() as logs:
            gate.upload("RLC-811A_00_20261001221600.jpg", still, seconds=5.5,
                        pieces=11, logs=logs)
            self.assertTrue(
                wait_for(lambda: gate.outcomes() or gate.rejections(), 10.0),
                f"the still was never decided:\n{logs.text()}",
            )
            sleep(0.3)

        self.assertEqual(gate.rejections(), [])
        self.assertEqual(gate.relay_calls, ["relay"])
        opened = [outcome for outcome in gate.outcomes() if outcome[0]]
        self.assertEqual(len(opened), 1, gate.outcomes())
        self.assertEqual(gate.cloud.posted, 0, "the device's read needed no cloud")
        self.assertEqual(logs.text().count("stage=upload_arriving"), 1)
        self.assertEqual(logs.text().count("stage=upload_completed"), 1)

    def test_a_still_already_half_arrived_when_the_controller_restarts_is_not_fresh(self):
        """The case first-sight timing alone gets wrong, and the guard for it.

        The controller restarts (a deploy) while a still is trickling in. At
        startup the file already holds ten elevenths of its bytes, which took
        about 30 s to arrive; the rest lands 3 s after startup. Measured from
        first sight the still is 3 s old and the gate would open. Timed by
        its own mtime, the bytes after first sight came in at a rate that
        puts the unseen part at about 30 s, so the still is ~33 s old and is
        refused ``stale_burst``, and the relay is left alone.
        """
        still = frame(403)
        split = len(still) * 10 // 11
        head, tail = still[:split], still[split:]

        def interrupted_upload(gate):
            path = gate.uploads / "RLC-811A_00_20261002071200.jpg"
            path.write_bytes(head)
            stamp(path, gate.time.epoch())
            gate.partial = path

        with CapturedLogs() as logs:
            answers = {}
            gate = self.gate = SlowUploadGate(
                self, answers=answers, before_start=interrupted_upload,
            )
            answers[digest(gate.pipeline_bytes(still))] = ("10CE1990", 0.97)
            self.assertTrue(
                wait_for(lambda: "stage=filesystem_ingress" in logs.text(), 10.0),
                "startup reconciliation never picked the upload up",
            )
            with gate.partial.open("ab") as handle:
                parts = chunks(tail, 6)
                for index, piece in enumerate(parts):
                    gate.time.advance(0.5)
                    handle.write(piece)
                    handle.flush()
                    stamp(gate.partial, gate.time.epoch())
                    if index < len(parts) - 1:
                        sleep(0.03)
            self.assertTrue(
                wait_for(lambda: gate.outcomes() or gate.rejections(), 10.0),
                f"the still was never decided:\n{logs.text()}",
            )
            sleep(0.3)

        self.assertEqual(gate.outcomes(), [(False, "stale_burst")])
        self.assertEqual(gate.relay_calls, [])
        completed = next(
            line for line in logs.lines if "stage=upload_completed" in line
        )
        unobserved_ms = int(completed.split("unobserved_ms=")[1].split()[0])
        self.assertGreater(unobserved_ms, 25_000)
        self.assertLess(unobserved_ms, 35_000)


class UploadWaitTests(unittest.TestCase):
    """The handler's own rules, on a clock the test holds."""

    def _handler(self, clock, rejected, collector=None, **options):
        return CompletedImageHandler(
            collector or RecordingCollector(), retry_interval=0.05, clock=clock,
            on_rejected=lambda path, reason: rejected.append((path.name, reason)),
            **options,
        )

    def test_a_truncated_upload_that_stops_growing_is_still_rejected(self):
        """vsftpd's one ``FAIL UPLOAD``: half a still, and nothing more coming."""
        still = frame(404)
        clock, rejected = MutableClock(), []
        handler = self._handler(clock, rejected)
        with tempfile.TemporaryDirectory() as directory, CapturedLogs() as logs:
            path = Path(directory) / "truncated.jpg"
            path.write_bytes(still[: len(still) // 2])
            handler.on_created(ClosedEvent(path))
            for second in range(1, 10):
                clock.value = float(second)
                handler.retry_pending()
            self.assertEqual(rejected, [], "rejected while within the stall window")
            clock.value = 10.0
            handler.retry_pending()
            self.assertFalse(path.exists())

        self.assertEqual(rejected, [("truncated.jpg", "upload_incomplete")])
        self.assertIn("cause=stalled", logs.text())
        self.assertNotIn(directory, logs.text())

    def test_a_growing_upload_is_waited_for_well_past_the_old_five_seconds(self):
        still = frame(405)
        clock, rejected = MutableClock(), []
        collector = RecordingCollector()
        arrival = datetime(2026, 10, 1, 21, 52, tzinfo=timezone.utc)
        handler = self._handler(
            clock, rejected, collector=collector, arrival_clock=lambda: arrival,
        )
        with tempfile.TemporaryDirectory() as directory, CapturedLogs() as logs:
            path = Path(directory) / "trickle.jpg"
            path.write_bytes(b"")
            handler.on_created(ClosedEvent(path))
            pieces = chunks(still, 45)
            with path.open("ab") as handle:
                for second, piece in enumerate(pieces, start=1):
                    clock.value = float(second)
                    handle.write(piece)
                    handle.flush()
                    handler.retry_pending()
                    handler.retry_pending()

        self.assertEqual(rejected, [])
        self.assertEqual(collector.paths, [path])
        self.assertEqual(collector.received_at, [arrival], "first sight is the frame's age")
        self.assertEqual(logs.text().count("stage=upload_arriving"), 1,
                         "the still-arriving line is once per upload, not per poll")
        self.assertIn("waited_ms=3000", logs.text())
        self.assertIn(f"bytes_per_second={round(sum(len(p) for p in pieces[:3]) / 3)}",
                      logs.text())
        self.assertIn(f"stage=upload_completed upload_ms={len(pieces) * 1000}", logs.text())

    def test_an_upload_deleted_mid_transfer_is_rejected(self):
        still = frame(406)
        clock, rejected = MutableClock(), []
        handler = self._handler(clock, rejected)
        with tempfile.TemporaryDirectory() as directory, CapturedLogs() as logs:
            path = Path(directory) / "deleted.jpg"
            path.write_bytes(still[:100])
            handler.on_created(ClosedEvent(path))
            clock.value = 1.0
            handler.retry_pending()
            path.unlink()
            clock.value = 2.0
            handler.retry_pending()
            # vsftpd then closes the unlinked file; there is nothing to record twice.
            handler.on_closed(ClosedEvent(path))
            clock.value = 3.0
            handler.retry_pending()

        self.assertEqual(rejected, [("deleted.jpg", "upload_incomplete")])
        self.assertIn("cause=missing", logs.text())
        self.assertEqual(handler.pending_count, 0)

    def test_the_ceiling_ends_an_upload_that_is_still_growing(self):
        """A still that would take ten minutes at 3 KB/s is not waited for forever."""
        clock, rejected = MutableClock(), []
        handler = self._handler(clock, rejected, max_age=120.0)
        with tempfile.TemporaryDirectory() as directory, CapturedLogs() as logs:
            path = Path(directory) / "endless.jpg"
            path.write_bytes(frame(407)[:2])
            handler.on_created(ClosedEvent(path))
            with path.open("ab") as handle:
                for second in range(1, 121):
                    clock.value = float(second)
                    handle.write(b"\x00" * 3072)
                    handle.flush()
                    handler.retry_pending()
                    if second < 120:
                        self.assertEqual(rejected, [], f"rejected at {second} s while growing")

        self.assertEqual(rejected, [("endless.jpg", "upload_incomplete")])
        self.assertIn("cause=ceiling", logs.text())

    def test_an_oversized_upload_is_still_rejected_as_too_large(self):
        clock, rejected = MutableClock(), []
        handler = self._handler(clock, rejected, max_candidate_bytes=1024)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "huge.jpg"
            path.write_bytes(b"\xff\xd8\xff" + b"\x00" * 2048)
            handler.on_created(ClosedEvent(path))
            handler.retry_pending()

        self.assertEqual(rejected, [("huge.jpg", "image_too_large")])

    def test_a_close_for_a_tracked_upload_looks_again_at_once(self):
        still = frame(408)
        clock, rejected = MutableClock(), []
        collector = RecordingCollector()
        handler = self._handler(clock, rejected, collector=collector)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "closed.jpg"
            path.write_bytes(still[:10])
            handler.on_created(ClosedEvent(path))
            handler.retry_pending()  # not whole; next look in 50 ms
            path.write_bytes(still)
            handler.on_closed(ClosedEvent(path))  # IN_CLOSE_WRITE
            handler.retry_pending()  # same instant on the clock

        self.assertEqual(collector.paths, [path])

    def test_bytes_already_there_at_first_sight_age_the_frame(self):
        """First sight is not the start of an upload found half-way through."""
        still = frame(409)
        clock, rejected = MutableClock(), []
        collector = RecordingCollector()
        arrival = datetime(2026, 10, 2, 7, 12, tzinfo=timezone.utc)
        handler = self._handler(
            clock, rejected, collector=collector, arrival_clock=lambda: arrival,
        )
        epoch = 1_790_000_000.0
        quarter = len(still) // 4
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "half.jpg"
            path.write_bytes(still[: 2 * quarter])
            stamp(path, epoch)
            handler.schedule_candidate(path)
            with path.open("ab") as handle:
                handle.write(still[2 * quarter: 3 * quarter])
                handle.flush()
                stamp(path, epoch + 5.0)
                clock.value = 5.0
                handler.retry_pending()
                handle.write(still[3 * quarter:])
                handle.flush()
                stamp(path, epoch + 10.0)
                clock.value = 10.0
                handler.retry_pending()

        self.assertEqual(collector.paths, [path])
        # Half the still arrived in the 10 s after first sight, so the half
        # already there is taken to have taken about as long.
        unseen = (arrival - collector.received_at[0]).total_seconds()
        expected = 2 * quarter * 10.0 / (len(still) - 2 * quarter)
        self.assertAlmostEqual(unseen, expected, places=3)

    def test_an_upload_seen_whole_at_first_sight_keeps_its_first_sight(self):
        still = frame(410)
        clock, rejected = MutableClock(), []
        collector = RecordingCollector()
        arrival = datetime(2026, 10, 2, 7, 13, tzinfo=timezone.utc)
        handler = self._handler(
            clock, rejected, collector=collector, arrival_clock=lambda: arrival,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "whole.jpg"
            path.write_bytes(still)
            handler.schedule_candidate(path)
            handler.retry_pending()

        self.assertEqual(collector.received_at, [arrival])


class UploadWaitConfigurationTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(gate_main.upload_wait_limits({}), (10.0, 120.0))

    def test_configured_values_are_used(self):
        self.assertEqual(
            gate_main.upload_wait_limits({
                "GATE_UPLOAD_STALL_SECONDS": "20", "GATE_UPLOAD_MAX_SECONDS": "180",
            }),
            (20.0, 180.0),
        )

    def test_unusable_values_fall_back_to_the_defaults_with_an_error(self):
        for key, value in (
            ("GATE_UPLOAD_STALL_SECONDS", "0"),
            ("GATE_UPLOAD_STALL_SECONDS", "nan"),
            ("GATE_UPLOAD_STALL_SECONDS", "61"),
            ("GATE_UPLOAD_MAX_SECONDS", "5"),
            ("GATE_UPLOAD_MAX_SECONDS", "301"),
            ("GATE_UPLOAD_MAX_SECONDS", "two minutes"),
        ):
            with self.subTest(key=key, value=value), self.assertLogs(
                "gate_controller.__main__", level="ERROR",
            ) as logs:
                self.assertEqual(gate_main.upload_wait_limits({key: value}), (10.0, 120.0))
            self.assertIn(f"key={key} status=rejected", "\n".join(logs.output))

    def test_run_worker_is_handed_the_configured_limits(self):
        source = Path(gate_main.__file__).read_text(encoding="utf-8")
        self.assertIn("upload_stall_seconds=upload_stall_seconds", source)
        self.assertIn("upload_max_seconds=upload_max_seconds", source)


if __name__ == "__main__":
    unittest.main()
