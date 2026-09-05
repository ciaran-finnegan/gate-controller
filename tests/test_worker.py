import hashlib
import unittest
import tempfile
import os
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event as ThreadEvent, Thread
from unittest.mock import patch
from PIL import Image

from gate_controller.worker import (
    BoundedBurstQueue, BurstCollector, BurstIdentity, CompletedImageHandler, StartupReconciler,
    reconcile_completed_images,
    start_observer_then_reconcile,
    _process_bursts,
    _install_sigterm_handler,
    run_worker,
)
from gate_controller.models import ProcessingResult
from gate_controller.processor import GateProcessor
from gate_controller.store import LocalStore
from gate_controller.telemetry import TriggerTelemetry


class MutableClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class RecordingCollector:
    def __init__(self):
        self.paths = []
        self.received_at = []

    def add(self, path, received_at=None):
        self.paths.append(path)
        self.received_at.append(received_at)


class SequenceQueue:
    def __init__(self, *items):
        self._items = iter(items)

    def get(self):
        return next(self._items)

class Event:
    def __init__(self, path):
        self.src_path = str(path)
        self.dest_path = str(path)
        self.is_directory = False


class PassiveObserver:
    def schedule(self, *args, **kwargs):
        pass

    def start(self):
        pass

    def stop(self):
        pass

    def join(self):
        pass


class WorkerTests(unittest.TestCase):
    def test_first_completed_ftp_candidate_adds_hot_frames_to_the_same_burst(self):
        received_at = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ftp = root / "ftp.jpg"
            hot_one = root / "hot-one.jpg"
            hot_two = root / "hot-two.jpg"
            for path, colour in ((ftp, "red"), (hot_one, "green"), (hot_two, "blue")):
                Image.new("RGB", (32, 16), color=colour).save(path, format="JPEG")
            emitted = []
            collector = BurstCollector(
                emitted.append, quiet_window=0,
                arrival_clock=lambda: received_at,
            )
            selected = []
            handler = CompletedImageHandler(
                collector,
                arrival_clock=lambda: received_at,
                on_first_completed=lambda candidate_received_at: (
                    selected.append(candidate_received_at) or (hot_one, hot_two)
                ),
            )

            handler.schedule_candidate(ftp)
            self.assertEqual(1, handler.retry_pending())
            self.assertTrue(collector.flush_due())

            self.assertEqual([received_at], selected)
            self.assertEqual({ftp, hot_one, hot_two}, set(emitted[0]))

    def test_hot_frame_selection_failure_keeps_the_ftp_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            ftp = Path(directory) / "ftp.jpg"
            Image.new("RGB", (32, 16), color="red").save(ftp, format="JPEG")
            emitted = []
            collector = BurstCollector(emitted.append, quiet_window=0)
            handler = CompletedImageHandler(
                collector,
                on_first_completed=lambda _received_at: (_ for _ in ()).throw(
                    RuntimeError("buffer unavailable")
                ),
            )

            handler.schedule_candidate(ftp)
            with self.assertLogs("gate_controller.worker", level="WARNING"):
                self.assertEqual(1, handler.retry_pending())
            self.assertTrue(collector.flush_due())

            self.assertEqual((ftp,), emitted[0])

    def test_hot_frames_never_evict_the_triggering_ftp_candidate(self):
        received_at = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / name for name in ("ftp.jpg", "hot-1.jpg", "hot-2.jpg", "hot-3.jpg")]
            for path, colour in zip(paths, ("red", "green", "blue", "yellow")):
                Image.new("RGB", (32, 16), color=colour).save(path, format="JPEG")
            emitted = []
            collector = BurstCollector(
                emitted.append, quiet_window=0, ranker=lambda candidates: candidates,
                max_candidates=3, arrival_clock=lambda: received_at,
            )
            handler = CompletedImageHandler(
                collector, arrival_clock=lambda: received_at,
                on_first_completed=lambda _received_at: tuple(paths[1:]),
            )

            handler.schedule_candidate(paths[0])
            self.assertEqual(1, handler.retry_pending())
            self.assertTrue(collector.flush_due())

            self.assertEqual(tuple(paths[:3]), emitted[0])
            self.assertTrue(paths[0].exists())
            self.assertFalse(paths[3].exists())

    def test_ranked_hot_frames_keep_the_ftp_digest_as_processing_identity(self):
        received_at = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ftp = root / "ftp.jpg"
            hot = root / "hot.jpg"
            Image.new("RGB", (32, 16), color="red").save(ftp, format="JPEG")
            Image.new("RGB", (32, 16), color="blue").save(hot, format="JPEG")
            expected = hashlib.sha256(ftp.read_bytes()).hexdigest()
            emitted = []
            collector = BurstCollector(
                emitted.append, quiet_window=0,
                ranker=lambda candidates: tuple(reversed(candidates)),
                include_received_at=True,
                include_idempotency_key=True,
                prefer_first_candidate=True,
                arrival_clock=lambda: received_at,
            )
            collector.add(ftp, received_at)
            collector.add(hot, received_at)
            self.assertTrue(collector.flush_due())
            calls = []

            _process_bursts(
                SequenceQueue(emitted[0], None),
                lambda *args, **kwargs: calls.append((args, kwargs)),
            )

            self.assertEqual((ftp, hot), calls[0][0][0])
            self.assertEqual(expected, calls[0][1]["idempotency_key"])

    def test_trigger_correlation_does_not_wait_before_initial_ftp_recognition(self):
        received_at = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
        calls = []
        primary = ((Path("ftp.jpg"),), received_at)

        def resolve_trigger(candidate_received_at):
            calls.append(("correlate", candidate_received_at))
            return "sanitized-trigger"

        def recognise(paths, candidate_received_at, *, trigger=None):
            calls.append((
                "recognise", paths, candidate_received_at, trigger,
            ))
            return ProcessingResult(False, "no_match", terminal=True)

        _process_bursts(
            SequenceQueue(primary, None),
            recognise,
            trigger_resolver=resolve_trigger,
        )

        self.assertEqual(calls, [
            ("correlate", received_at),
            (
                "recognise", (Path("ftp.jpg"),), received_at,
                "sanitized-trigger",
            ),
        ])

    def test_default_readability_window_stays_at_five_seconds(self):
        handler = CompletedImageHandler(BurstCollector(lambda *_: None))

        self.assertEqual(handler._retry_interval, 0.05)
        self.assertGreaterEqual(handler._retry_interval * handler._max_attempts, 5.0)

    def test_injected_trigger_burst_uses_its_own_trigger_and_never_correlates(self):
        received_at = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
        early = TriggerTelemetry(
            source="reolink_webhook", event_type="vehicle",
            rule_id="front_gate", correlation="matched", delta_ms=180,
        )
        calls = []

        def resolve_trigger(candidate_received_at):
            calls.append(("correlate", candidate_received_at))
            return "ftp-trigger"

        def recognise(paths, candidate_received_at, *timing, trigger=None,
                      idempotency_key=None):
            calls.append(("recognise", paths, trigger, idempotency_key))
            return ProcessingResult(False, "no_match", terminal=True)

        _process_bursts(
            SequenceQueue(
                ((Path("clear.jpg"),), received_at, 1.0, received_at,
                 BurstIdentity("digest-1", early)),
                None,
            ),
            recognise,
            trigger_resolver=resolve_trigger,
        )

        self.assertEqual(calls, [
            ("recognise", (Path("clear.jpg"),), early, "digest-1"),
        ])

    def test_run_worker_attaches_an_injector_that_enqueues_a_trigger_burst(self):
        received_at = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
        early = TriggerTelemetry(
            source="reolink_webhook", event_type="vehicle",
            rule_id="front_gate", correlation="matched", delta_ms=180,
        )
        processed = []
        stop_after_first = ThreadEvent()

        class Capture:
            output_directory = None

            def attach(self, inject):
                self.inject = inject

        capture = Capture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture.output_directory = root / ".trigger-capture"
            capture.output_directory.mkdir(mode=0o700)
            frame = capture.output_directory / "frame.jpg"
            frame.write_bytes(b"\xff\xd8\xff\xd9")

            def emit(paths, candidate_received_at, *timing, trigger=None,
                     idempotency_key=None):
                processed.append((paths, candidate_received_at, trigger, idempotency_key))
                stop_after_first.set()
                return ProcessingResult(False, "no_match", terminal=True)

            class OneShotWorker:
                def run_forever(self, stop_event):
                    capture.inject((frame,), received_at, early)
                    stop_after_first.wait(timeout=5)
                    stop_event.set()

            run_worker(
                root, emit, quiet_window=0.1, poll_interval=0.01,
                background_workers=(OneShotWorker(),),
                trigger_resolver=lambda _received_at: self.fail("must not correlate"),
                trigger_capture=capture,
            )

        self.assertEqual(len(processed), 1)
        paths, candidate_received_at, trigger, idempotency_key = processed[0]
        self.assertEqual(paths, (frame,))
        self.assertEqual(candidate_received_at, received_at)
        self.assertIs(trigger, early)
        self.assertEqual(len(idempotency_key), 64)
        self.assertFalse(frame.exists())

    def test_run_worker_reports_each_result_to_the_capture(self):
        received_at = datetime(2026, 9, 5, 19, 33, tzinfo=timezone.utc)
        trigger = TriggerTelemetry(
            source="reolink_webhook", event_type="vehicle",
            rule_id="front_gate", correlation="matched", delta_ms=10,
        )
        noted = []
        seen_by_hook = []
        done = ThreadEvent()

        class Capture:
            output_directory = None

            def attach(self, inject):
                self.inject = inject

            def note_result(self, paths, result):
                noted.append((paths, result))

        capture = Capture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture.output_directory = root / ".trigger-capture"
            capture.output_directory.mkdir(mode=0o700)
            frame = capture.output_directory / "frame.jpg"
            frame.write_bytes(b"\xff\xd8\xff\xd9")
            verdict = ProcessingResult(False, "ocr_error")

            def emit(paths, candidate_received_at, *timing, trigger=None, idempotency_key=None):
                done.set()
                return verdict

            class OneShotWorker:
                def run_forever(self, stop_event):
                    capture.inject((frame,), received_at, trigger)
                    done.wait(timeout=5)
                    stop_event.set()

            run_worker(
                root, emit, quiet_window=0.1, poll_interval=0.01,
                background_workers=(OneShotWorker(),),
                trigger_capture=capture,
                on_result=lambda paths, result: seen_by_hook.append(result),
            )

        self.assertEqual(noted, [((frame,), verdict)])
        self.assertEqual(seen_by_hook, [verdict])

    def test_failed_recognition_reports_the_same_sanitized_trigger(self):
        received_at = datetime(2026, 8, 20, 10, 1, tzinfo=timezone.utc)
        failures = []

        def recognise(*_args, **_kwargs):
            raise RuntimeError("recognition failed")

        def report_error(paths, error, candidate_received_at, *, trigger=None):
            failures.append((paths, error, candidate_received_at, trigger))

        _process_bursts(
            SequenceQueue(((Path("ftp.jpg"),), received_at), None),
            recognise,
            on_error=report_error,
            trigger_resolver=lambda _: "sanitized-trigger",
        )

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][0], (Path("ftp.jpg"),))
        self.assertIsInstance(failures[0][1], RuntimeError)
        self.assertEqual(failures[0][2:], (received_at, "sanitized-trigger"))

    def test_processing_exception_persists_the_consumed_matched_trigger(self):
        received_at = datetime(2026, 8, 20, 10, 1, tzinfo=timezone.utc)
        matched = TriggerTelemetry(
            source="reolink_webhook", event_type="line_crossing",
            rule_id="line_crossing_inbound", correlation="matched",
            delta_ms=32,
        )

        class UnusedRecognizer:
            def recognise(self, _path):
                raise AssertionError("processing should fail before OCR")

        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "ftp.jpg"
            Image.new("L", (16, 8), color=128).save(frame, format="JPEG")
            store = LocalStore(Path(directory) / "gate.db")
            processor = GateProcessor(
                recognizer=UnusedRecognizer(), store=store, relay=object(),
                authorised=(),
            )
            event_ids = []

            def fail_processing(*_args, **_kwargs):
                raise RuntimeError("processing failed")

            def persist_error(paths, _error, candidate_received_at, *, trigger=None):
                event_ids.append(processor.record_skipped(
                    paths, "processing_error", candidate_received_at,
                    trigger=trigger,
                ).event_id)

            _process_bursts(
                SequenceQueue(((frame,), received_at), None),
                fail_processing,
                on_error=persist_error,
                trigger_resolver=lambda _received_at: matched,
            )

            self.assertEqual(len(event_ids), 1)
            self.assertEqual(store.event_telemetry(event_ids[0])["trigger"], {
                "source": "reolink_webhook",
                "event_type": "line_crossing",
                "rule_id": "line_crossing_inbound",
                "correlation": "matched",
                "delta_ms": 32,
            })

    def test_trigger_resolver_exception_uses_the_exact_ftp_fallback(self):
        received_at = datetime(2026, 8, 20, 10, 1, tzinfo=timezone.utc)
        observed = []

        def fail_to_resolve(_received_at):
            raise RuntimeError("correlator unavailable")

        def recognise(_paths, _received_at, *, trigger=None):
            observed.append(None if trigger is None else trigger.to_wire())
            return ProcessingResult(False, "no_match", terminal=True)

        with self.assertLogs("gate_controller.worker", level="WARNING"):
            _process_bursts(
                SequenceQueue(((Path("ftp.jpg"),), received_at), None),
                recognise,
                trigger_resolver=fail_to_resolve,
            )

        self.assertEqual(observed, [{
            "source": "camera_ftp",
            "event_type": "unverified",
            "correlation": "unverified",
        }])

    def test_pre_ocr_rejection_reports_the_exact_ftp_fallback_trigger(self):
        configured = {}
        skipped = []

        class Collector:
            def __init__(self, _emit, **_kwargs):
                pass

            def flush_due(self):
                return False

        class Handler:
            def __init__(self, _collector, **kwargs):
                configured["on_rejected"] = kwargs["on_rejected"]

            def retry_pending(self):
                return 0

            def schedule_candidate(self, *_args):
                pass

        class WorkerThread:
            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                pass

            def join(self, timeout=None):
                pass

        def reject_then_stop(_interval):
            configured["on_rejected"](
                Path("oversized.jpg"), "image_too_large",
            )
            raise KeyboardInterrupt

        matched = TriggerTelemetry(
            source="reolink_webhook", event_type="line_crossing",
            rule_id="line_crossing_inbound", correlation="matched",
            delta_ms=25,
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "gate_controller.worker.BurstCollector", Collector
        ), patch(
            "gate_controller.worker.CompletedImageHandler", Handler
        ), patch(
            "gate_controller.worker.Observer", return_value=PassiveObserver()
        ), patch(
            "gate_controller.worker.Thread", WorkerThread
        ), patch(
            "gate_controller.worker.current_thread_is_main", return_value=False
        ), patch(
            "gate_controller.worker.sleep", side_effect=reject_then_stop
        ):
            run_worker(
                Path(directory), lambda *_args, **_kwargs: None,
                on_skipped=lambda *_args, trigger=None, **_kwargs: skipped.append(
                    None if trigger is None else trigger.to_wire()
                ),
                trigger_resolver=lambda _received_at: matched,
            )

        self.assertEqual(skipped, [{
            "source": "camera_ftp",
            "event_type": "unverified",
            "correlation": "unverified",
        }])

    def test_filesystem_ingress_is_logged_without_exposing_the_image_path(self):
        collector = RecordingCollector()
        observed_at = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        handler = CompletedImageHandler(collector, arrival_clock=lambda: observed_at)
        private_path = Path("/private/camera/customer-plate.jpg")

        with self.assertLogs("gate_controller.worker", level="INFO") as logs:
            handler.schedule_candidate(private_path)

        combined = "\n".join(logs.output)
        self.assertIn("gate_pipeline stage=filesystem_ingress", combined)
        self.assertIn("observed_at=2026-08-14T10:00:00+00:00", combined)
        self.assertNotIn(str(private_path), combined)

    def test_coalesces_completed_files_until_the_quiet_window_expires(self):
        clock = MutableClock()
        emitted = []
        collector = BurstCollector(
            emitted.append,
            quiet_window=0.5,
            ranker=lambda paths: list(reversed(paths)),
            clock=clock,
        )
        first = Path("first.jpg")
        second = Path("second.jpg")

        collector.add(first)
        clock.value = 0.2
        collector.add(second)
        clock.value = 0.69
        collector.flush_due()
        clock.value = 0.7
        collector.flush_due()

        self.assertEqual(emitted, [(second, first)])

    def test_200ms_window_resets_after_the_latest_completed_upload(self):
        clock = MutableClock()
        ranked_inputs = []
        emitted = []

        def ranker(paths):
            ranked_inputs.append(tuple(paths))
            return tuple(reversed(paths))

        collector = BurstCollector(
            emitted.append, quiet_window=0.2, ranker=ranker, clock=clock,
        )
        first = Path("first.jpg")
        second = Path("second.jpg")

        collector.add(first)
        clock.value = 0.15
        collector.add(second)
        clock.value = 0.34
        self.assertFalse(collector.flush_due())
        clock.value = 0.351
        self.assertTrue(collector.flush_due())

        self.assertEqual(ranked_inputs, [(first, second)])
        self.assertEqual(emitted, [(second, first)])

    def test_burst_collector_keeps_only_the_freshest_candidate_limit(self):
        clock = MutableClock()
        ranked_inputs = []
        emitted = []

        def ranker(paths):
            ranked_inputs.append(tuple(paths))
            return list(reversed(paths))

        collector = BurstCollector(
            emitted.append, quiet_window=0.5, ranker=ranker, clock=clock,
            max_candidates=2,
        )
        first = Path("first.jpg")
        second = Path("second.jpg")
        third = Path("third.jpg")

        collector.add(first)
        collector.add(second)
        collector.add(third)
        clock.value = 0.5
        collector.flush_due()

        self.assertEqual(ranked_inputs, [(second, third)])
        self.assertEqual(emitted, [(third, second)])

    def test_burst_collector_removes_an_upload_evicted_before_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first.jpg", root / "second.jpg"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            collector = BurstCollector(
                lambda _: None, ranker=lambda paths: paths, max_candidates=1
            )

            collector.add(first)
            collector.add(second)

            self.assertFalse(first.exists())
            self.assertTrue(second.exists())

    def test_burst_collector_removes_uploads_rejected_during_ranking(self):
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rejected, accepted = root / "rejected.jpg", root / "accepted.jpg"
            rejected.write_bytes(b"rejected")
            accepted.write_bytes(b"accepted")
            collector = BurstCollector(
                lambda _: None, ranker=lambda paths: (accepted,), clock=clock
            )
            collector.add(rejected)
            collector.add(accepted)
            clock.value = 1

            collector.flush_due()

            self.assertFalse(rejected.exists())
            self.assertTrue(accepted.exists())

    def test_burst_decision_clock_starts_before_ranking(self):
        clock = MutableClock()
        received_at = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        emitted = []

        def ranker(paths):
            clock.value = 5.0
            return paths

        collector = BurstCollector(
            emitted.append, quiet_window=0.5, ranker=ranker, clock=clock,
            include_received_at=True, include_decision_started_at=True,
        )
        path = Path("frame.jpg")
        collector.add(path, received_at)
        clock.value = 1.0

        collector.flush_due()

        self.assertEqual(emitted, [((path,), received_at, 1.0)])

    def test_burst_log_pairs_pre_ranking_wall_and_monotonic_boundaries(self):
        clock = MutableClock()
        wall_clock = [datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)]
        processing_started_at = wall_clock[0]
        emitted = []

        def delayed_ranker(paths):
            clock.value += 4.0
            wall_clock[0] += timedelta(seconds=4)
            return paths

        collector = BurstCollector(
            emitted.append,
            quiet_window=0.5,
            ranker=delayed_ranker,
            clock=clock,
            wall_clock=lambda: wall_clock[0],
            include_processing_started_at=True,
        )
        path = Path("frame.jpg")
        collector.add(path)
        clock.value = 1.0

        with self.assertLogs("gate_controller.worker", level="INFO") as logs:
            collector.flush_due()

        combined = "\n".join(logs.output)
        self.assertIn("observed_at=2026-08-14T10:00:00+00:00", combined)
        self.assertIn("ingress_wait_ms=1000", combined)
        self.assertNotIn("observed_at=2026-08-14T10:00:04+00:00", combined)
        self.assertEqual(emitted, [((path,), processing_started_at)])

    def test_created_file_fallback_adds_only_a_readable_completed_upload(self):
        collector = RecordingCollector()
        handler = CompletedImageHandler(collector)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "finished.jpg"
            path.write_bytes(b"upload")

            with patch("gate_controller.worker.wait_until_readable", return_value=True):
                handler.on_created(Event(path))
                handler.retry_pending()

            self.assertEqual(collector.paths, [path])

    def test_created_file_fallback_retries_without_blocking_or_dropping_the_path(self):
        collector = RecordingCollector()
        clock = MutableClock()
        handler = CompletedImageHandler(collector, retry_interval=1, clock=clock)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "slow-upload.jpg"
            path.write_bytes(b"upload")

            with patch("gate_controller.worker.wait_until_readable", side_effect=[False, True]):
                handler.on_created(Event(path))
                handler.retry_pending()
                clock.value = 1
                handler.retry_pending()

            self.assertEqual(collector.paths, [path])

    def test_oversized_candidate_is_rejected_before_image_decoding(self):
        collector = RecordingCollector()
        rejected = []
        handler = CompletedImageHandler(
            collector, max_candidate_bytes=4,
            on_rejected=lambda path, reason: rejected.append((path, reason)),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oversized.jpg"
            path.write_bytes(b"12345")
            handler.on_created(Event(path))

            with patch("gate_controller.worker.wait_until_readable") as readable:
                handler.retry_pending()

            self.assertFalse(path.exists())

        readable.assert_not_called()
        self.assertEqual(collector.paths, [])
        self.assertEqual(rejected, [(path, "image_too_large")])

    def test_pending_upload_validation_keeps_only_the_freshest_candidates(self):
        collector = RecordingCollector()
        handler = CompletedImageHandler(collector, max_pending_candidates=2)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.jpg"
            second = root / "second.jpg"
            third = root / "third.jpg"
            for path in (first, second, third):
                path.write_bytes(b"upload")
                handler.schedule_candidate(path)

            with patch("gate_controller.worker.wait_until_readable", return_value=True):
                handler.retry_pending()

            self.assertFalse(first.exists())

        self.assertEqual(collector.paths, [second, third])

    def test_slow_upload_keeps_its_first_filesystem_arrival_time(self):
        collector = RecordingCollector()
        monotonic_clock = MutableClock()
        first_seen = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        wall_clock = [first_seen]
        handler = CompletedImageHandler(
            collector, retry_interval=1, clock=monotonic_clock,
            arrival_clock=lambda: wall_clock[0],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "slow-upload.jpg"
            path.write_bytes(b"upload")

            with patch("gate_controller.worker.wait_until_readable", side_effect=[False, True]):
                handler.on_created(Event(path))
                handler.retry_pending()
                monotonic_clock.value = 1
                wall_clock[0] += timedelta(seconds=4)
                handler.retry_pending()

        self.assertEqual(collector.paths, [path])
        self.assertEqual(collector.received_at, [first_seen])

    def test_add_during_flush_is_emitted_in_the_next_burst(self):
        clock = MutableClock()
        started = ThreadEvent()
        allow_finish = ThreadEvent()
        emitted = []

        def ranker(paths):
            started.set()
            allow_finish.wait(timeout=1)
            return paths

        collector = BurstCollector(emitted.append, ranker=ranker, clock=clock)
        first = Path("first.jpg")
        second = Path("second.jpg")
        collector.add(first)
        clock.value = 1
        flushing = Thread(target=collector.flush_due)
        flushing.start()
        self.assertTrue(started.wait(timeout=1))
        collector.add(second)
        allow_finish.set()
        flushing.join(timeout=1)
        clock.value = 2
        collector.flush_due()

        self.assertEqual(emitted, [(first,), (second,)])

    def test_startup_reconciles_complete_jpegs(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "arrived-while-offline.jpg"
            image.write_bytes(b"valid later checked by Pillow")
            collector = RecordingCollector()
            handler = CompletedImageHandler(collector)

            with patch("gate_controller.worker.wait_until_readable", return_value=True):
                reconcile_completed_images(Path(directory), handler)
                handler.retry_pending()

            self.assertEqual(collector.paths, [image])

    def test_startup_reconciliation_skips_old_jpegs_observably(self):
        skipped = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old.jpg"
            fresh = root / "fresh.jpg"
            old.write_bytes(b"old")
            fresh.write_bytes(b"fresh")
            os.utime(old, (80, 80))
            os.utime(fresh, (99, 99))
            with patch("gate_controller.worker.time", return_value=100):
                handler = CompletedImageHandler(RecordingCollector())

                reconcile_completed_images(root, handler, max_image_age=5, on_skipped=skipped.append)

            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())

        self.assertEqual(skipped, [old])
        self.assertEqual(handler.pending_count, 1)

    def test_startup_reconciliation_inspects_only_a_bounded_number_of_entries(self):
        class RecordingHandler:
            def __init__(self):
                self.paths = []

            def schedule_candidate(self, path, is_directory=False):
                self.paths.append(path)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(129):
                (root / f"{index:03d}.jpg").write_bytes(b"fresh")
            handler = RecordingHandler()

            reconcile_completed_images(root, handler)

            self.assertLessEqual(len(handler.paths), 128)

    def test_bounded_startup_reconciliation_eventually_inspects_every_entry(self):
        class RecordingHandler:
            def __init__(self):
                self.paths = []

            def schedule_candidate(self, path, is_directory=False):
                self.paths.append(path)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(257):
                (root / f"{index:03d}.jpg").write_bytes(b"fresh")
            handler = RecordingHandler()
            reconciler = StartupReconciler(root, handler, max_image_age=60)

            while reconciler.run_batch(max_entries=32):
                pass

            self.assertEqual(len(handler.paths), 257)

    def test_startup_reconciliation_inspects_nested_reolink_directories(self):
        class RecordingHandler:
            def __init__(self):
                self.paths = []

            def schedule_candidate(self, path, is_directory=False):
                self.paths.append(path)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "2026" / "08" / "18"
            nested.mkdir(parents=True)
            image = nested / "Front Gate_00_20260818211110.jpg"
            image.write_bytes(b"fresh")
            handler = RecordingHandler()

            reconcile_completed_images(root, handler, max_image_age=60)

            self.assertEqual(handler.paths, [image])

    def test_run_worker_observes_nested_camera_directories(self):
        scheduled = []

        class Observer:
            def schedule(self, handler, path, recursive=False):
                scheduled.append((path, recursive))

            def start(self):
                pass

            def stop(self):
                pass

            def join(self):
                pass

        class WorkerThread:
            def __init__(self, *args, **kwargs):
                self.name = kwargs.get("name")

            def start(self):
                pass

            def join(self, timeout=None):
                pass

        with tempfile.TemporaryDirectory() as directory, patch(
            "gate_controller.worker.Observer", return_value=Observer()
        ), patch(
            "gate_controller.worker.Thread", WorkerThread
        ), patch(
            "gate_controller.worker.current_thread_is_main", return_value=False
        ), patch(
            "gate_controller.worker.sleep", side_effect=KeyboardInterrupt
        ):
            run_worker(Path(directory), lambda *_: None)

        self.assertEqual(scheduled, [(directory, True)])

    def test_future_dated_startup_upload_is_rejected_after_clock_rollback(self):
        skipped = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            future = root / "future.jpg"
            future.write_bytes(b"future")
            os.utime(future, (110, 110))
            handler = CompletedImageHandler(RecordingCollector())

            with patch("gate_controller.worker.time", return_value=100):
                reconcile_completed_images(
                    root, handler, max_image_age=5, on_skipped=skipped.append
                )

            self.assertFalse(future.exists())
            self.assertEqual(skipped, [future])

    def test_bounded_queue_coalesces_backlog_to_the_newest_burst(self):
        queue = BoundedBurstQueue(max_pending=1)

        queue.put((Path("first.jpg"),))
        dropped = queue.put((Path("second.jpg"),))

        self.assertEqual(dropped, (Path("first.jpg"),))
        self.assertEqual(queue.get(), (Path("second.jpg"),))

    def test_bounded_queue_rejects_non_positive_capacity(self):
        for max_pending in (0, -1):
            with self.subTest(max_pending=max_pending):
                with self.assertRaisesRegex(ValueError, "positive"):
                    BoundedBurstQueue(max_pending=max_pending)

    def test_stopping_the_burst_queue_discards_pending_work_before_the_sentinel(self):
        queue = BoundedBurstQueue(max_pending=2)
        first = ((Path("first.jpg"),), datetime(2026, 8, 22, tzinfo=timezone.utc))
        second = ((Path("second.jpg"),), datetime(2026, 8, 22, tzinfo=timezone.utc))
        queue.put(first)
        queue.put(second)

        self.assertEqual((first, second), queue.stop())
        self.assertIsNone(queue.get())

    def test_burst_received_at_uses_first_filesystem_arrival_not_image_mtime(self):
        monotonic_clock = MutableClock()
        arrivals = iter([
            datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 14, 10, 0, 2, tzinfo=timezone.utc),
        ])
        emitted = []
        collector = BurstCollector(
            emitted.append,
            quiet_window=0.5,
            ranker=lambda paths: paths,
            clock=monotonic_clock,
            arrival_clock=lambda: next(arrivals),
            include_received_at=True,
        )
        first = Path("camera-clock-2038.jpg")
        second = Path("camera-clock-1970.jpg")

        collector.add(first)
        monotonic_clock.value = 0.2
        collector.add(second)
        monotonic_clock.value = 0.7
        collector.flush_due()

        self.assertEqual(
            emitted,
            [((first, second), datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc))],
        )

    def test_processing_exception_is_forwarded_to_observable_error_handler(self):
        queue = BoundedBurstQueue(max_pending=2)
        received_at = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        paths = (Path("failed.jpg"),)
        errors = []
        queue.put((paths, received_at))
        queue.put(None)

        _process_bursts(
            queue,
            lambda *_: (_ for _ in ()).throw(RuntimeError("processor failed")),
            lambda caught_paths, error, caught_at: errors.append(
                (caught_paths, str(error), caught_at)
            ),
        )

        self.assertEqual(errors, [(paths, "processor failed", received_at)])

    def test_processed_upload_is_removed_after_emit_returns(self):
        queue = BoundedBurstQueue(max_pending=2)
        received_at = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "processed.jpg"
            path.write_bytes(b"camera upload")
            queue.put(((path,), received_at))
            queue.put(None)

            _process_bursts(queue, lambda *_: None)

            self.assertFalse(path.exists())

    def test_processing_receives_the_preprocessing_start_time(self):
        queue = BoundedBurstQueue(max_pending=2)
        received_at = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        paths = (Path("ranked.jpg"),)
        calls = []
        queue.put((paths, received_at, 12.5))
        queue.put(None)

        _process_bursts(queue, lambda *args: calls.append(args))

        self.assertEqual(calls, [(paths, received_at, 12.5)])

    def test_run_worker_applies_configured_candidate_limits(self):
        configured = {}

        class Collector:
            def __init__(self, emit, **kwargs):
                configured["collector"] = kwargs

            def flush_due(self):
                return False

        class Handler:
            def __init__(self, collector, **kwargs):
                configured["handler"] = kwargs

            def retry_pending(self):
                return 0

            def schedule_candidate(self, *args):
                pass

        class WorkerThread:
            def __init__(self, *args, **kwargs):
                self.name = kwargs.get("name")

            def start(self):
                pass

            def join(self, timeout=None):
                pass

        with tempfile.TemporaryDirectory() as directory, patch(
            "gate_controller.worker.BurstCollector", Collector
        ), patch(
            "gate_controller.worker.CompletedImageHandler", Handler
        ), patch(
            "gate_controller.worker.Observer", return_value=PassiveObserver()
        ), patch(
            "gate_controller.worker.Thread", WorkerThread
        ), patch(
            "gate_controller.worker.current_thread_is_main", return_value=False
        ), patch(
            "gate_controller.worker.sleep", side_effect=KeyboardInterrupt
        ):
            run_worker(
                Path(directory), lambda *_: None,
                max_burst_candidates=5, max_candidate_bytes=1024,
            )

        self.assertEqual(configured["collector"]["max_candidates"], 5)
        self.assertTrue(configured["collector"]["include_decision_started_at"])
        self.assertTrue(configured["collector"]["include_processing_started_at"])
        self.assertTrue(configured["collector"]["prefer_first_candidate"])
        self.assertEqual(configured["handler"]["max_candidate_bytes"], 1024)
        self.assertEqual(configured["handler"]["max_pending_candidates"], 5)

    def test_queue_coalesced_callback_receives_exact_collector_boundaries(self):
        configured = {}
        received_at = datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc)
        processing_started_at = received_at + timedelta(milliseconds=250)
        timed_skips = []
        legacy_skips = []

        class Collector:
            def __init__(self, emit, **kwargs):
                configured["emit"] = emit

            def flush_due(self):
                return False

        class Handler:
            def __init__(self, collector, **kwargs):
                pass

            def retry_pending(self):
                return 0

            def schedule_candidate(self, *args):
                pass

        class WorkerThread:
            def __init__(self, *args, **kwargs):
                self.name = kwargs.get("name")

            def start(self):
                pass

            def join(self, timeout=None):
                pass

        def enqueue_then_stop(_):
            configured["emit"](((Path("first.jpg"),), received_at, 12.5,
                                 processing_started_at))
            configured["emit"](((Path("second.jpg"),), received_at, 13.0,
                                 processing_started_at + timedelta(seconds=1)))
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as directory, patch(
            "gate_controller.worker.BurstCollector", Collector
        ), patch(
            "gate_controller.worker.CompletedImageHandler", Handler
        ), patch(
            "gate_controller.worker.Observer", return_value=PassiveObserver()
        ), patch(
            "gate_controller.worker.Thread", WorkerThread
        ), patch(
            "gate_controller.worker.current_thread_is_main", return_value=False
        ), patch(
            "gate_controller.worker.sleep", side_effect=enqueue_then_stop
        ):
            run_worker(
                Path(directory), lambda *_: None, max_pending_bursts=1,
                on_skipped=lambda *args: legacy_skips.append(args),
                on_timed_skipped=lambda *args: timed_skips.append(args),
            )

        self.assertEqual(timed_skips[0], (
            (Path("first.jpg"),), "queue_coalesced", received_at, 12.5,
            processing_started_at,
        ))
        self.assertEqual(legacy_skips, [])

    def test_fatal_image_worker_exception_fails_the_service(self):
        started = ThreadEvent()

        def fail_image_worker(*_):
            started.set()
            raise RuntimeError("image worker stopped")

        sleep_calls = []

        def wait_then_stop(_):
            sleep_calls.append(1)
            if len(sleep_calls) == 1:
                started.wait(timeout=1)
                return
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as directory, patch(
            "gate_controller.worker.Observer", return_value=PassiveObserver()
        ), patch(
            "gate_controller.worker.current_thread_is_main", return_value=False
        ), patch(
            "gate_controller.worker._process_bursts", side_effect=fail_image_worker
        ), patch(
            "gate_controller.worker.sleep", side_effect=wait_then_stop
        ):
            with self.assertRaisesRegex(RuntimeError, "GateBurstProcessor.*image worker stopped"):
                run_worker(Path(directory), lambda *_: None)

    def test_dead_watchdog_observer_fails_the_service(self):
        class DeadObserver(PassiveObserver):
            def is_alive(self):
                return False

        with tempfile.TemporaryDirectory() as directory, patch(
            "gate_controller.worker.Observer", return_value=DeadObserver()
        ), patch(
            "gate_controller.worker.current_thread_is_main", return_value=False
        ), patch(
            "gate_controller.worker.sleep", side_effect=KeyboardInterrupt
        ):
            with self.assertRaisesRegex(RuntimeError, "watchdog Observer exited unexpectedly"):
                run_worker(Path(directory), lambda *_: None)

    def test_unexpected_background_worker_exit_fails_the_service(self):
        returned = ThreadEvent()

        class ReturningWorker:
            def run_forever(self, stop_event):
                returned.set()

        sleep_calls = []

        def wait_then_stop(_):
            sleep_calls.append(1)
            if len(sleep_calls) == 1:
                returned.wait(timeout=1)
                return
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as directory, patch(
            "gate_controller.worker.Observer", return_value=PassiveObserver()
        ), patch(
            "gate_controller.worker.current_thread_is_main", return_value=False
        ), patch(
            "gate_controller.worker.sleep", side_effect=wait_then_stop
        ):
            with self.assertRaisesRegex(RuntimeError, "ReturningWorker.*exited unexpectedly"):
                run_worker(
                    Path(directory), lambda *_: None,
                    background_workers=(ReturningWorker(),),
                )

    def test_sigterm_handler_requests_orderly_shutdown(self):
        stop = ThreadEvent()
        installed = {}

        with patch("gate_controller.worker.signal.getsignal", return_value="previous"), patch(
            "gate_controller.worker.signal.signal",
            side_effect=lambda number, handler: installed.update(number=number, handler=handler),
        ):
            previous = _install_sigterm_handler(stop)
            installed["handler"](signal.SIGTERM, None)

        self.assertEqual(previous, "previous")
        self.assertEqual(installed["number"], signal.SIGTERM)
        self.assertTrue(stop.is_set())

    def test_sigterm_handler_is_installed_before_any_runtime_component_starts(self):
        calls = []

        class Observer:
            def schedule(self, *args, **kwargs):
                pass

            def start(self):
                calls.append("observer_start")

            def stop(self):
                calls.append("observer_stop")

            def join(self):
                calls.append("observer_join")

        class WorkerThread:
            def __init__(self, *args, **kwargs):
                self.name = kwargs.get("name")

            def start(self):
                calls.append(f"thread_start:{self.name}")

            def join(self, timeout=None):
                calls.append(f"thread_join:{self.name}")

        with tempfile.TemporaryDirectory() as directory, patch(
            "gate_controller.worker.Observer", return_value=Observer()
        ), patch(
            "gate_controller.worker.Thread", WorkerThread
        ), patch(
            "gate_controller.worker.current_thread_is_main", return_value=True
        ), patch(
            "gate_controller.worker._install_sigterm_handler",
            side_effect=lambda _: calls.append("sigterm_install") or "previous",
        ), patch(
            "gate_controller.worker.signal.signal",
            side_effect=lambda *_: calls.append("sigterm_restore"),
        ), patch(
            "gate_controller.worker.sleep", side_effect=KeyboardInterrupt
        ):
            run_worker(Path(directory), lambda *_: None)

        self.assertLess(calls.index("sigterm_install"), calls.index("observer_start"))
        self.assertLess(calls.index("sigterm_install"), calls.index("thread_start:GateBurstProcessor"))
        self.assertEqual(calls[-1], "sigterm_restore")

    def test_startup_failure_restores_sigterm_and_shuts_down_the_relay(self):
        calls = []

        class Observer:
            def schedule(self, *args, **kwargs):
                pass

            def start(self):
                calls.append("observer_start")
                raise RuntimeError("observer unavailable")

            def stop(self):
                calls.append("observer_stop")

            def join(self):
                calls.append("observer_join")

        with tempfile.TemporaryDirectory() as directory, patch(
            "gate_controller.worker.Observer", return_value=Observer()
        ), patch(
            "gate_controller.worker.current_thread_is_main", return_value=True
        ), patch(
            "gate_controller.worker._install_sigterm_handler",
            side_effect=lambda _: calls.append("sigterm_install") or "previous",
        ), patch(
            "gate_controller.worker.signal.signal",
            side_effect=lambda *_: calls.append("sigterm_restore"),
        ):
            with self.assertRaisesRegex(RuntimeError, "observer unavailable"):
                run_worker(
                    Path(directory), lambda *_: None,
                    shutdown=lambda: calls.append("relay_shutdown"),
                )

        self.assertEqual(calls[0], "sigterm_install")
        self.assertIn("relay_shutdown", calls)
        self.assertEqual(calls[-1], "sigterm_restore")

    def test_service_stop_joins_burst_processor_before_controller_shutdown(self):
        calls = []

        class Observer:
            def schedule(self, *args, **kwargs):
                pass

            def start(self):
                calls.append("observer_start")

            def stop(self):
                calls.append("observer_stop")

            def join(self):
                calls.append("observer_join")

        class WorkerThread:
            def __init__(self, *args, **kwargs):
                self.name = kwargs.get("name")

            def start(self):
                calls.append(f"thread_start:{self.name}")

            def join(self, timeout=None):
                calls.append((f"thread_join:{self.name}", timeout))

        with tempfile.TemporaryDirectory() as directory, patch(
            "gate_controller.worker.Observer", return_value=Observer()
        ), patch(
            "gate_controller.worker.Thread", WorkerThread
        ), patch(
            "gate_controller.worker.current_thread_is_main", return_value=False
        ), patch(
            "gate_controller.worker.sleep", side_effect=KeyboardInterrupt
        ):
            run_worker(
                Path(directory), lambda *_: None,
                shutdown=lambda: calls.append("relay_shutdown"),
            )

        self.assertLess(
            calls.index(("thread_join:GateBurstProcessor", None)),
            calls.index("relay_shutdown"),
        )
        self.assertLess(calls.index("relay_shutdown"), calls.index("observer_stop"))

    def test_retry_policy_expires_missing_candidates(self):
        collector = RecordingCollector()
        rejected = []
        clock = MutableClock()
        handler = CompletedImageHandler(
            collector, retry_interval=1, max_attempts=2, max_age=2, clock=clock,
            on_rejected=lambda path, reason: rejected.append((path, reason)),
        )
        path = Path("missing.jpg")
        handler.on_created(Event(path))

        with patch("gate_controller.worker.wait_until_readable", return_value=False):
            handler.retry_pending()
            clock.value = 1
            handler.retry_pending()
            clock.value = 2
            handler.retry_pending()

        self.assertEqual(handler.pending_count, 0)
        self.assertEqual(rejected, [(path, "upload_incomplete")])

    def test_startup_window_upload_is_seen_after_observer_starts(self):
        class Observer:
            def __init__(self, path):
                self.path = path

            def start(self):
                self.path.write_bytes(b"arrived between startup phases")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "startup-window.jpg"
            collector = RecordingCollector()
            handler = CompletedImageHandler(collector)

            start_observer_then_reconcile(Observer(path), Path(directory), handler)

            self.assertEqual(handler.pending_count, 1)


if __name__ == "__main__":
    unittest.main()
