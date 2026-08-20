import unittest
import tempfile
import os
import signal
import gate_controller.worker as worker_module
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event as ThreadEvent, Thread
from unittest.mock import patch

from PIL import Image

from gate_controller.worker import (
    BoundedBurstQueue, BurstCollector, CompletedImageHandler, StartupReconciler,
    reconcile_completed_images,
    start_observer_then_reconcile,
    _process_bursts,
    _install_sigterm_handler,
    run_worker,
)
from gate_controller.models import ProcessingResult
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

    def cancel_augmentation_reservation(self):
        pass


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
    def test_trigger_correlation_does_not_wait_before_initial_ftp_recognition(self):
        received_at = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
        calls = []
        snapshot_still_running = ThreadEvent()
        snapshot_still_running.set()
        progressive = worker_module.ProgressiveTrigger(received_at)
        primary = worker_module.BurstWork(
            "primary", ((Path("ftp.jpg"),), received_at), progressive,
        )

        def resolve_trigger(candidate_received_at):
            calls.append(("correlate", candidate_received_at))
            return "sanitized-trigger"

        def recognise(paths, candidate_received_at, *, final=True, trigger=None):
            calls.append((
                "recognise", paths, candidate_received_at, final, trigger,
            ))
            self.assertTrue(snapshot_still_running.is_set())
            return ProcessingResult(False, "no_match", terminal=True)

        _process_bursts(
            SequenceQueue(primary, None),
            recognise,
            trigger_resolver=resolve_trigger,
        )

        self.assertEqual(calls, [
            ("correlate", received_at),
            (
                "recognise", (Path("ftp.jpg"),), received_at, False,
                "sanitized-trigger",
            ),
        ])

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

    def test_empty_ranked_burst_abandons_its_exact_context_before_the_next_burst(self):
        clock = MutableClock()
        emitted = []
        abandoned = []
        rank_calls = []

        def ranker(paths):
            rank_calls.append(paths)
            return () if len(rank_calls) == 1 else paths

        collector = BurstCollector(
            emitted.append,
            quiet_window=0.5,
            ranker=ranker,
            clock=clock,
            include_received_at=True,
            on_abandoned=abandoned.append,
        )
        received_a = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)
        received_b = received_a + timedelta(seconds=1)
        collector.add(Path("a.jpg"), received_a)
        collector.bind_context("trigger-a")
        clock.value = 0.5

        self.assertFalse(collector.flush_due())

        collector.add(Path("b.jpg"), received_b)
        collector.bind_context("trigger-b")
        clock.value = 1.0
        self.assertTrue(collector.flush_due())

        self.assertEqual(abandoned, ["trigger-a"])
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].context, "trigger-b")
        self.assertEqual(emitted[0].item[:2], ((Path("b.jpg"),), received_b))

    def test_abandoning_trigger_releases_unsubmitted_reservation_and_snapshot_files(self):
        queue = BoundedBurstQueue(max_pending=1)
        released = []
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot.jpg"
            snapshot.write_bytes(b"snapshot")
            trigger = worker_module.ProgressiveTrigger(
                datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc),
                release=lambda: released.append(True),
                snapshots=(snapshot,),
            )
            self.assertTrue(queue.reserve_augmentation())

            worker_module._abandon_progressive_trigger(trigger, queue)

            self.assertFalse(snapshot.exists())
            self.assertEqual(released, [True])
            self.assertTrue(queue.reserve_augmentation())

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

    def test_first_completed_ftp_candidate_requests_one_unverified_augmentation(self):
        clock = MutableClock()
        sample_requests = []
        collector = BurstCollector(
            lambda _: None, quiet_window=0.5, ranker=lambda paths: paths, clock=clock,
        )
        handler = CompletedImageHandler(
            collector,
            on_first_completed=lambda received_at: sample_requests.append(received_at),
        )
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "private-first.jpg"
            second = Path(directory) / "private-second.jpg"
            first.write_bytes(b"first")
            second.write_bytes(b"second")

            with patch("gate_controller.worker.wait_until_readable", return_value=True), self.assertLogs(
                "gate_controller.worker", level="INFO"
            ) as logs:
                handler.schedule_candidate(first)
                handler.retry_pending()
                handler.schedule_candidate(second)
                handler.retry_pending()

            combined = "\n".join(logs.output)
            self.assertEqual(len(sample_requests), 1)
            self.assertIn("source=camera_ftp subtype=unverified", combined)
            self.assertIn("augmentation_request=accepted", combined)
            self.assertNotIn(str(first), combined)
            self.assertNotIn(str(second), combined)

    def test_augmentation_request_failure_keeps_the_first_ftp_candidate(self):
        clock = MutableClock()
        emitted = []
        collector = BurstCollector(
            emitted.append, quiet_window=0.5, ranker=lambda paths: paths, clock=clock,
        )
        handler = CompletedImageHandler(
            collector,
            on_first_completed=lambda received_at: (_ for _ in ()).throw(
                RuntimeError("private failure")
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.jpg"
            first.write_bytes(b"first")
            with patch("gate_controller.worker.wait_until_readable", return_value=True), self.assertLogs(
                "gate_controller.worker", level="WARNING"
            ) as logs:
                handler.schedule_candidate(first)
                self.assertEqual(handler.retry_pending(), 1)
            clock.value = 1
            self.assertTrue(collector.flush_due())

        self.assertEqual(emitted, [(first,)])
        combined = "\n".join(logs.output)
        self.assertIn("augmentation_request=failed", combined)
        self.assertIn("reason=request_error", combined)
        self.assertNotIn("private failure", combined)

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

    def test_reserved_augmentation_never_evicts_or_precedes_ftp_work(self):
        queue = BoundedBurstQueue(max_pending=1)
        ftp = ((Path("ftp.jpg"),), "ftp")
        snapshots = ((Path("snapshot.jpg"),), "augmentation")

        self.assertTrue(queue.reserve_augmentation())
        self.assertIsNone(queue.put(ftp))
        self.assertIsNone(queue.put_augmentation(snapshots))

        self.assertEqual(queue.get(), ftp)
        self.assertEqual(queue.get(), snapshots)

    def test_final_progressive_selection_can_include_an_anchored_snapshot(self):
        class AnchoredSnapshot:
            _descriptor_anchored = True

            def __init__(self):
                self.unlinked = 0

            def unlink(self, missing_ok=False):
                self.unlinked += 1

        received_at = datetime(2026, 8, 20, 11, 0, tzinfo=timezone.utc)
        ftp = tuple(Path(f"ftp-{index}.jpg") for index in range(4))
        snapshot = AnchoredSnapshot()
        trigger = worker_module.ProgressiveTrigger(received_at)
        primary = worker_module.BurstWork(
            "primary", (ftp, received_at), trigger,
        )
        augmentation = worker_module.BurstWork(
            "augmentation", ((snapshot,), received_at), trigger,
        )
        calls = []

        def emit(paths, captured_at, *timing, **options):
            calls.append((paths, captured_at, options))
            return ProcessingResult(
                False, "no_match", idempotency_key="progressive-event",
                terminal=False,
            )

        with patch(
            "gate_controller.worker.rank_images",
            return_value=(snapshot, ftp[3], ftp[2], ftp[1], ftp[0]),
        ):
            _process_bursts(SequenceQueue(primary, augmentation, None), emit)

        self.assertEqual(calls[0], (ftp, received_at, {"final": False}))
        self.assertEqual(calls[1][0], (snapshot, ftp[3], ftp[2]))
        self.assertIs(calls[1][0][0], snapshot)
        self.assertEqual(calls[1][2], {
            "idempotency_key": "progressive-event",
            "final": True,
            "augmentation": {
                "outcome": "completed",
                "reason": "completed",
                "candidate_count": 1,
                "duration_ms": 0,
                "correlation": "progressive-event",
            },
        })
        self.assertEqual(snapshot.unlinked, 1)

    def test_shutdown_discards_reserved_and_future_work_idempotently(self):
        discarded = []
        queue = BoundedBurstQueue(
            max_pending=1,
            on_discard=lambda stopped_queue, item: discarded.append(item),
        )
        augmentation = ((Path("snapshot.jpg"),), "augmentation")
        future_primary = ((Path("future-ftp.jpg"),), "ftp")
        future_augmentation = ((Path("future-snapshot.jpg"),), "augmentation")

        self.assertTrue(queue.reserve_augmentation())
        queue.put_augmentation(augmentation)
        queue.put(None)
        queue.put(future_primary)
        queue.put_augmentation(future_augmentation)
        queue.put(None)

        self.assertIsNone(queue.get())
        self.assertFalse(queue.reserve_augmentation())
        self.assertEqual(
            discarded, [augmentation, future_primary, future_augmentation]
        )

    def test_shutdown_does_not_emit_queued_primary_or_augmentation_work(self):
        queue = BoundedBurstQueue(max_pending=2)
        received_at = datetime(2026, 8, 20, 11, 0, tzinfo=timezone.utc)
        emitted = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ordinary = root / "ordinary.jpg"
            progressive = root / "progressive.jpg"
            snapshot = root / "snapshot.jpg"
            for path in (ordinary, progressive, snapshot):
                path.write_bytes(b"candidate")
            trigger = worker_module.ProgressiveTrigger(
                received_at, snapshots=(snapshot,), augmentation_submitted=True,
            )
            queue.put(((ordinary,), received_at))
            queue.put(worker_module.BurstWork(
                "primary", ((progressive,), received_at), trigger,
            ))
            self.assertTrue(queue.reserve_augmentation())
            queue.put_augmentation(worker_module.BurstWork(
                "augmentation", ((snapshot,), received_at), trigger,
            ))
            queue.put(None)

            def emit(paths, captured_at, *timing, **options):
                emitted.append((paths, captured_at, options))
                return ProcessingResult(
                    False, "no_match", idempotency_key="queued-trigger",
                    terminal=False,
                )

            _process_bursts(queue, emit)

        self.assertEqual(emitted, [])

    def test_shutdown_cleanup_removes_paths_and_abandons_shared_trigger_once(self):
        received_at = datetime(2026, 8, 20, 11, 5, tzinfo=timezone.utc)
        released = []
        queue = BoundedBurstQueue(
            max_pending=2,
            on_discard=lambda stopped_queue, item: (
                worker_module._discard_burst_work(item, stopped_queue)
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ordinary = root / "ordinary.jpg"
            progressive = root / "progressive.jpg"
            snapshot = root / "snapshot.jpg"
            future = root / "future.jpg"
            for path in (ordinary, progressive, snapshot, future):
                path.write_bytes(b"candidate")
            trigger = worker_module.ProgressiveTrigger(
                received_at,
                release=lambda: released.append(True),
                snapshots=(snapshot,),
                augmentation_submitted=True,
            )
            queue.put(((ordinary,), received_at))
            queue.put(worker_module.BurstWork(
                "primary", ((progressive,), received_at), trigger,
            ))
            self.assertTrue(queue.reserve_augmentation())
            queue.put_augmentation(worker_module.BurstWork(
                "augmentation", ((snapshot,), received_at), trigger,
            ))

            queue.put(None)
            queue.put(((future,), received_at))
            queue.put(None)

            self.assertFalse(any(
                path.exists() for path in (ordinary, progressive, snapshot, future)
            ))
            self.assertTrue(trigger.abandoned)
            self.assertEqual(released, [True])
            self.assertIsNone(queue.get())

    def test_shutdown_terminalizes_a_computed_provisional_result_when_sampler_completes(self):
        received_at = datetime(2026, 8, 20, 11, 10, tzinfo=timezone.utc)
        initial_processed = ThreadEvent()
        calls = []
        terminal_candidate_existence = []
        queue = BoundedBurstQueue(
            max_pending=1,
            on_discard=lambda stopped_queue, item: (
                worker_module._discard_burst_work(item, stopped_queue)
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "ftp.jpg"
            snapshot = root / "snapshot.jpg"
            primary.write_bytes(b"ftp")
            snapshot.write_bytes(b"snapshot")
            trigger = worker_module.ProgressiveTrigger(received_at)
            queue.put(worker_module.BurstWork(
                "primary", ((primary,), received_at), trigger,
            ))

            def emit(paths, captured_at, *timing, **options):
                calls.append((paths, captured_at, options))
                if not options.get("final"):
                    initial_processed.set()
                    return ProcessingResult(
                        False, "no_match", idempotency_key="shutdown-event",
                        terminal=False,
                    )
                terminal_candidate_existence.append((
                    primary.exists(), snapshot.exists(),
                ))
                return ProcessingResult(
                    False, "no_match", idempotency_key="shutdown-event",
                )

            processor = Thread(target=_process_bursts, args=(queue, emit))
            processor.start()
            self.assertTrue(initial_processed.wait(timeout=1))
            queue.put(None)
            processor.join(timeout=1)
            self.assertFalse(processor.is_alive())

            with trigger._lock:
                trigger.snapshots = (snapshot,)
                trigger.augmentation_submitted = True
            queue.put_augmentation(worker_module.BurstWork(
                "augmentation", ((snapshot,), received_at), trigger,
            ))

            self.assertFalse(primary.exists())
            self.assertFalse(snapshot.exists())

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][2], {"final": False})
        self.assertEqual(calls[1][2]["final"], True)
        self.assertEqual(calls[1][2]["provisional_result"], ProcessingResult(
            False, "no_match", idempotency_key="shutdown-event", terminal=False,
        ))
        self.assertEqual(terminal_candidate_existence, [(True, True)])

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
        received_at = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        paths = (Path("failed.jpg"),)
        errors = []

        _process_bursts(
            SequenceQueue((paths, received_at), None),
            lambda *_: (_ for _ in ()).throw(RuntimeError("processor failed")),
            lambda caught_paths, error, caught_at: errors.append(
                (caught_paths, str(error), caught_at)
            ),
        )

        self.assertEqual(errors, [(paths, "processor failed", received_at)])

    def test_processed_upload_is_removed_after_emit_returns(self):
        received_at = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "processed.jpg"
            path.write_bytes(b"camera upload")

            _process_bursts(
                SequenceQueue(((path,), received_at), None), lambda *_: None
            )

            self.assertFalse(path.exists())

    def test_progressive_work_reuses_ftp_identity_and_retains_it_until_terminal_result(self):
        received_at = datetime(2026, 8, 19, 18, 30, tzinfo=timezone.utc)
        released = []
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            ftp = Path(directory) / "ftp.jpg"
            snapshot = Path(directory) / "snapshot.jpg"
            Image.new("RGB", (32, 24), color="red").save(ftp, format="JPEG")
            Image.new("RGB", (32, 24), color="blue").save(snapshot, format="JPEG")
            trigger = worker_module.ProgressiveTrigger(
                received_at, release=lambda: released.append(True)
            )
            items = iter((
                worker_module.BurstWork("primary", ((ftp,), received_at), trigger),
                worker_module.BurstWork(
                    "augmentation", ((snapshot,), received_at), trigger
                ),
                None,
            ))

            class WorkQueue:
                def get(self):
                    return next(items)

            def emit(paths, captured_at, *timing, **options):
                calls.append((paths, captured_at, options))
                if len(calls) == 1:
                    return ProcessingResult(
                        False, "no_match", idempotency_key="ftp-trigger",
                        terminal=False,
                    )
                return ProcessingResult(
                    True, "activated", event_id=1,
                    idempotency_key="ftp-trigger", terminal=True,
                )

            _process_bursts(WorkQueue(), emit, ranker=lambda candidates: candidates)

            self.assertEqual(calls[0][0], (ftp,))
            self.assertEqual(calls[0][2], {"final": False})
            self.assertEqual(calls[1][0], (ftp, snapshot))
            self.assertEqual(calls[1][2], {
                "idempotency_key": "ftp-trigger", "final": True,
                "augmentation": {
                    "outcome": "completed",
                    "reason": "completed",
                    "candidate_count": 1,
                    "duration_ms": 0,
                    "correlation": "ftp-trigger",
                },
            })
            self.assertFalse(ftp.exists())
            self.assertFalse(snapshot.exists())
            self.assertEqual(released, [True])

    def test_throwing_final_ranker_releases_progressive_candidates(self):
        received_at = datetime(2026, 8, 20, 12, 30, tzinfo=timezone.utc)
        released = []
        errors = []
        with tempfile.TemporaryDirectory() as directory:
            ftp = Path(directory) / "ftp.jpg"
            snapshot = Path(directory) / "snapshot.jpg"
            ftp.write_bytes(b"ftp")
            snapshot.write_bytes(b"snapshot")
            trigger = worker_module.ProgressiveTrigger(
                received_at, release=lambda: released.append(True),
            )
            items = iter((
                worker_module.BurstWork("primary", ((ftp,), received_at), trigger),
                worker_module.BurstWork(
                    "augmentation", ((snapshot,), received_at), trigger
                ),
                None,
            ))

            class WorkQueue:
                def get(self):
                    return next(items)

            _process_bursts(
                WorkQueue(),
                lambda *_args, **_kwargs: ProcessingResult(
                    False, "no_match", idempotency_key="ftp-trigger", terminal=False,
                ),
                lambda paths, error, captured_at: errors.append(
                    (paths, str(error), captured_at)
                ),
                ranker=lambda _paths: (_ for _ in ()).throw(RuntimeError("rank failed")),
            )

            self.assertTrue(trigger.finalized)
            self.assertEqual(errors, [((ftp, snapshot), "rank failed", received_at)])
            self.assertFalse(ftp.exists())
            self.assertFalse(snapshot.exists())
            self.assertEqual(released, [True])

    def test_late_augmentation_cleans_after_its_primary_work_was_coalesced(self):
        received_at = datetime(2026, 8, 19, 18, 30, tzinfo=timezone.utc)
        released = []
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot.jpg"
            snapshot.write_bytes(b"snapshot")
            trigger = worker_module.ProgressiveTrigger(
                received_at, release=lambda: released.append(True), failed=True,
            )
            items = iter((
                worker_module.BurstWork(
                    "augmentation", ((snapshot,), received_at), trigger
                ),
                None,
            ))

            class WorkQueue:
                def get(self):
                    return next(items)

            _process_bursts(
                WorkQueue(), lambda *_args, **_kwargs: self.fail(
                    "coalesced trigger reached processing"
                )
            )

            self.assertFalse(snapshot.exists())
            self.assertEqual(released, [True])

    def test_empty_augmentation_finalizes_the_original_provisional_result(self):
        received_at = datetime(2026, 8, 19, 18, 30, tzinfo=timezone.utc)
        calls = []
        provisional = ProcessingResult(
            False, "no_match", idempotency_key="ftp-trigger", terminal=False,
        )
        with tempfile.TemporaryDirectory() as directory:
            ftp = Path(directory) / "ftp.jpg"
            ftp.write_bytes(b"ftp")
            trigger = worker_module.ProgressiveTrigger(received_at)
            items = iter((
                worker_module.BurstWork("primary", ((ftp,), received_at), trigger),
                worker_module.BurstWork("augmentation", ((), received_at), trigger),
                None,
            ))

            class WorkQueue:
                def get(self):
                    return next(items)

            def emit(paths, captured_at, *timing, **options):
                calls.append(options)
                return provisional if len(calls) == 1 else ProcessingResult(
                    False, "no_match", event_id=1,
                    idempotency_key="ftp-trigger", terminal=True,
                )

            _process_bursts(WorkQueue(), emit)

            self.assertEqual(calls[0], {"final": False})
            self.assertEqual(calls[1], {
                "idempotency_key": "ftp-trigger",
                "final": True,
                "provisional_result": provisional,
                "augmentation": {
                    "outcome": "failed",
                    "reason": "empty",
                    "candidate_count": 0,
                    "duration_ms": 0,
                },
            })
            self.assertFalse(ftp.exists())

    def test_processing_receives_the_preprocessing_start_time(self):
        received_at = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        paths = (Path("ranked.jpg"),)
        calls = []

        _process_bursts(
            SequenceQueue((paths, received_at, 12.5), None),
            lambda *args: calls.append(args),
        )

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
        self.assertEqual(configured["handler"]["max_candidate_bytes"], 1024)
        self.assertEqual(configured["handler"]["max_pending_candidates"], 5)

    def test_run_worker_stops_queued_bursts_before_controller_shutdown(self):
        configured = {}
        consumer_ready = ThreadEvent()
        release_consumer = ThreadEvent()
        consumer_finished = ThreadEvent()
        emitted = []
        observed_during_shutdown = []
        real_process_bursts = worker_module._process_bursts

        class Collector:
            def __init__(self, emit, **kwargs):
                configured["enqueue"] = emit

            def flush_due(self):
                return False

        class Handler:
            def __init__(self, collector, **kwargs):
                pass

            def retry_pending(self):
                return 0

            def schedule_candidate(self, *args):
                pass

        class OneItemQueue:
            def __init__(self, queue):
                self._queue = queue
                self._consumed = False

            def get(self):
                if self._consumed:
                    return None
                self._consumed = True
                return self._queue.get()

        def delayed_processor(queue, emit, on_error, ranker=None):
            consumer_ready.set()
            release_consumer.wait(timeout=1)
            try:
                real_process_bursts(OneItemQueue(queue), emit, on_error, ranker)
            finally:
                consumer_finished.set()

        with tempfile.TemporaryDirectory() as directory:
            queued = Path(directory) / "queued.jpg"
            received_at = datetime(2026, 8, 20, 11, 30, tzinfo=timezone.utc)

            def enqueue_then_stop(_):
                self.assertTrue(consumer_ready.wait(timeout=1))
                queued.write_bytes(b"candidate")
                configured["enqueue"](((queued,), received_at))
                raise KeyboardInterrupt

            def blocked_shutdown():
                release_consumer.set()
                self.assertTrue(consumer_finished.wait(timeout=1))
                observed_during_shutdown.extend(emitted)

            with patch(
                "gate_controller.worker.BurstCollector", Collector
            ), patch(
                "gate_controller.worker.CompletedImageHandler", Handler
            ), patch(
                "gate_controller.worker.Observer", return_value=PassiveObserver()
            ), patch(
                "gate_controller.worker._process_bursts", delayed_processor
            ), patch(
                "gate_controller.worker.current_thread_is_main", return_value=False
            ), patch(
                "gate_controller.worker.sleep", side_effect=enqueue_then_stop
            ):
                run_worker(
                    Path(directory), lambda *args: emitted.append(args),
                    shutdown=blocked_shutdown,
                )

            self.assertFalse(queued.exists())

        self.assertEqual(observed_during_shutdown, [])
        self.assertEqual(emitted, [])

    def test_run_worker_never_defers_the_initial_ftp_burst_for_snapshot_sampling(self):
        configured = {}
        lifecycle = []

        class Collector:
            def __init__(self, emit, **kwargs):
                configured.setdefault("collectors", []).append(kwargs)

            def add(self, path, received_at=None):
                return True

            def flush_due(self):
                return False

        class Handler:
            def __init__(self, collector, **kwargs):
                configured["handler"] = kwargs

            def retry_pending(self):
                return 0

            def schedule_candidate(self, *args):
                pass

        class Sampler:
            output_directory = Path("private-snapshots")

            def __init__(self, config, complete):
                configured["sampler_config"] = config
                configured["sampler_complete"] = complete

            def request(self, received_at=None):
                return True

            def run_forever(self, stop_event):
                pass

            def close(self):
                lifecycle.append("sampler_close")

        class WorkerThread:
            def __init__(self, *args, **kwargs):
                self.name = kwargs.get("name")

            def start(self):
                lifecycle.append(f"thread_start:{self.name}")

            def join(self, timeout=None):
                lifecycle.append(f"thread_join:{self.name}")

        with tempfile.TemporaryDirectory() as directory, patch(
            "gate_controller.worker.BurstCollector", Collector
        ), patch(
            "gate_controller.worker.CompletedImageHandler", Handler
        ), patch(
            "gate_controller.worker.ReolinkSnapshotSampler", Sampler
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
                snapshot_sampling=type("Config", (), {"enabled": True})(),
            )

        self.assertEqual(len(configured["collectors"]), 1)
        self.assertTrue(all(
            "defer_flush" not in options for options in configured["collectors"]
        ))
        self.assertIsNone(getattr(
            configured["handler"]["on_first_completed"], "__self__", None
        ))
        self.assertTrue(callable(configured["sampler_complete"]))
        self.assertLess(
            lifecycle.index("thread_join:ReolinkSnapshotSampler"),
            lifecycle.index("sampler_close"),
        )

    def test_disabled_sampling_does_not_touch_private_files_or_start_a_thread(self):
        configured = {}
        lifecycle = []

        class Collector:
            def __init__(self, emit, **kwargs):
                pass

            def flush_due(self):
                return False

        class Handler:
            def __init__(self, collector, **kwargs):
                configured["handler"] = kwargs

            def retry_pending(self):
                return 0

            def schedule_candidate(self, *args):
                pass

        class Sampler:
            def __init__(self, config, add_candidate):
                self.output_directory = config.output_directory
                lifecycle.append("sampler_init")

            def close(self):
                lifecycle.append("sampler_close")

        class WorkerThread:
            def __init__(self, *args, **kwargs):
                self.name = kwargs.get("name")

            def start(self):
                lifecycle.append(f"thread_start:{self.name}")

            def join(self, timeout=None):
                pass

        with tempfile.TemporaryDirectory() as directory, patch(
            "gate_controller.worker.BurstCollector", Collector
        ), patch(
            "gate_controller.worker.CompletedImageHandler", Handler
        ), patch(
            "gate_controller.worker.ReolinkSnapshotSampler", Sampler
        ), patch(
            "gate_controller.worker.Observer", return_value=PassiveObserver()
        ), patch(
            "gate_controller.worker.Thread", WorkerThread
        ), patch(
            "gate_controller.worker.current_thread_is_main", return_value=False
        ), patch(
            "gate_controller.worker.sleep", side_effect=KeyboardInterrupt
        ):
            output_directory = Path(directory) / ".reolink-snapshots"
            config = type("Config", (), {
                "enabled": False,
                "output_directory": output_directory,
            })()
            run_worker(Path(directory), lambda *_: None, snapshot_sampling=config)

        self.assertEqual(lifecycle.count("sampler_init"), 1)
        self.assertEqual(lifecycle.count("sampler_close"), 1)
        self.assertNotIn("thread_start:ReolinkSnapshotSampler", lifecycle)
        self.assertEqual(configured["handler"]["ignored_roots"], ())
        self.assertIsNone(configured["handler"]["on_first_completed"])

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

    def test_service_stop_latches_relay_before_worker_cleanup(self):
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

        with tempfile.TemporaryDirectory() as directory, patch(
            "gate_controller.worker.Observer", return_value=Observer()
        ), patch(
            "gate_controller.worker.current_thread_is_main", return_value=False
        ), patch(
            "gate_controller.worker.sleep", side_effect=KeyboardInterrupt
        ):
            run_worker(
                Path(directory), lambda *_: None,
                shutdown=lambda: calls.append("relay_shutdown"),
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
