import unittest
import tempfile
import os
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event as ThreadEvent, Thread
from unittest.mock import patch

from gate_controller.worker import (
    BoundedBurstQueue, BurstCollector, CompletedImageHandler, StartupReconciler,
    reconcile_completed_images,
    start_observer_then_reconcile,
    _process_bursts,
    _install_sigterm_handler,
    run_worker,
)


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
        self.assertEqual(configured["handler"]["max_candidate_bytes"], 1024)
        self.assertEqual(configured["handler"]["max_pending_candidates"], 5)

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
