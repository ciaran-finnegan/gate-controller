import logging
import os
from pathlib import Path
from queue import Empty, Full, Queue
import signal
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from time import monotonic, sleep, time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .images import rank_images, wait_until_readable
from .reolink_snapshots import (
    MAX_REOLINK_SNAPSHOT_TIMEOUT_SECONDS, ReolinkSnapshotSampler,
)


DEFAULT_MAX_BURST_CANDIDATES = 8
DEFAULT_MAX_CANDIDATE_BYTES = 8 * 1024 * 1024
MAX_BURST_CANDIDATES = 16
MAX_CANDIDATE_BYTES = 16 * 1024 * 1024
MAX_STARTUP_ENTRIES = 128


LOGGER = logging.getLogger(__name__)


class WorkerFailure(RuntimeError):
    pass


class BoundedBurstQueue:
    """A one-consumer queue that keeps the freshest pending camera work."""

    def __init__(self, max_pending: int = 2):
        self._queue = Queue(maxsize=max_pending)

    def put(self, item):
        dropped = None
        if self._queue.full():
            try:
                dropped = self._queue.get_nowait()
            except Empty:
                pass
        self._queue.put(item)
        return dropped

    def get(self):
        return self._queue.get()

    def try_put(self, item) -> bool:
        """Admit optional work only when it cannot displace queued work."""
        try:
            self._queue.put_nowait(item)
            return True
        except Full:
            return False


class BurstCollector:
    def __init__(self, emit, quiet_window: float = 0.5, ranker=rank_images, clock=monotonic,
                 arrival_clock=None, include_received_at: bool = False,
                 include_decision_started_at: bool = False,
                 include_processing_started_at: bool = False,
                 max_candidates: int = DEFAULT_MAX_BURST_CANDIDATES,
                 wall_clock=None):
        if not 1 <= max_candidates <= MAX_BURST_CANDIDATES:
            raise ValueError("max_candidates exceeds the safe range")
        self._emit = emit
        self._quiet_window = quiet_window
        self._ranker = ranker
        self._clock = clock
        self._arrival_clock = arrival_clock or (lambda: datetime.now(timezone.utc))
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._include_received_at = include_received_at
        self._include_decision_started_at = include_decision_started_at
        self._include_processing_started_at = include_processing_started_at
        self._max_candidates = max_candidates
        self._pending: list[Path] = []
        self._received_at: datetime | None = None
        self._first_seen: float | None = None
        self._deadline: float | None = None
        self._lock = Lock()

    def add(self, path: Path, received_at: datetime | None = None) -> bool:
        path = Path(path)
        dropped = None
        with self._lock:
            first_candidate = not self._pending
            if first_candidate:
                self._received_at = received_at or self._arrival_clock()
                self._first_seen = self._clock()
            elif received_at is not None and received_at < self._received_at:
                self._received_at = received_at
            if path in self._pending:
                self._pending.remove(path)
            self._pending.append(path)
            if len(self._pending) > self._max_candidates:
                dropped = self._pending.pop(0)
            self._deadline = self._clock() + self._quiet_window
        if dropped is not None:
            _remove_upload(dropped)
        return first_candidate

    def flush_due(self) -> bool:
        with self._lock:
            decision_started_at = self._clock()
            if self._deadline is None or decision_started_at < self._deadline:
                return False
            processing_started_at = self._wall_clock()
            pending = tuple(self._pending)
            received_at = self._received_at
            first_seen = self._first_seen
            self._pending = []
            self._received_at = None
            self._first_seen = None
            self._deadline = None
        ranked = tuple(self._ranker(pending))
        ranked_paths = set(ranked)
        _remove_uploads(path for path in pending if path not in ranked_paths)
        if not ranked:
            return False
        wait_ms = (
            max(0, round((decision_started_at - first_seen) * 1_000))
            if first_seen is not None else None
        )
        LOGGER.info(
            "gate_pipeline stage=burst_processing_started observed_at=%s "
            "candidate_count=%d ingress_wait_ms=%s",
            processing_started_at.astimezone(timezone.utc).isoformat(),
            len(ranked),
            wait_ms if wait_ms is not None else "unavailable",
        )
        details = [ranked]
        if self._include_received_at:
            details.append(received_at)
        if self._include_decision_started_at:
            details.append(decision_started_at)
        if self._include_processing_started_at:
            details.append(processing_started_at)
        self._emit(tuple(details) if len(details) > 1 else ranked)
        return True


class CompletedImageHandler(FileSystemEventHandler):
    def __init__(self, collector: BurstCollector, retry_interval: float = 0.25,
                 max_attempts: int = 20, max_age: float = 30.0, clock=monotonic,
                 on_rejected=None, arrival_clock=None,
                 max_candidate_bytes: int = DEFAULT_MAX_CANDIDATE_BYTES,
                 max_pending_candidates: int = DEFAULT_MAX_BURST_CANDIDATES,
                 on_first_completed=None, ignored_roots=()):
        super().__init__()
        if not 1 <= max_candidate_bytes <= MAX_CANDIDATE_BYTES:
            raise ValueError("max_candidate_bytes exceeds the safe range")
        if not 1 <= max_pending_candidates <= MAX_BURST_CANDIDATES:
            raise ValueError("max_pending_candidates exceeds the safe range")
        self._collector = collector
        self._retry_interval = retry_interval
        self._max_attempts = max_attempts
        self._max_age = max_age
        self._clock = clock
        self._arrival_clock = arrival_clock or (lambda: datetime.now(timezone.utc))
        self._on_rejected = on_rejected
        self._max_candidate_bytes = max_candidate_bytes
        self._max_pending_candidates = max_pending_candidates
        self._on_first_completed = on_first_completed
        self._ignored_roots = tuple(Path(root).resolve() for root in ignored_roots)
        self._retry_at: dict[Path, tuple[float, float, int, datetime]] = {}
        self._lock = Lock()

    def on_closed(self, event) -> None:
        self.schedule_candidate(Path(event.src_path), event.is_directory)

    def on_moved(self, event) -> None:
        self.schedule_candidate(Path(event.dest_path), event.is_directory)

    def on_created(self, event) -> None:
        self.schedule_candidate(Path(event.src_path), event.is_directory)

    def schedule_candidate(self, path: Path, is_directory: bool = False) -> None:
        if is_directory or self.ignores(path) or path.suffix.lower() not in {".jpg", ".jpeg"}:
            return
        dropped = []
        first_observation = None
        with self._lock:
            now = self._clock()
            candidate = self._retry_at.pop(path, None)
            if candidate is None:
                first_observation = self._arrival_clock()
            self._retry_at[path] = candidate or (now, now, 0, first_observation)
            while len(self._retry_at) > self._max_pending_candidates:
                dropped_path = next(iter(self._retry_at))
                self._retry_at.pop(dropped_path)
                dropped.append(dropped_path)
            pending_count = len(self._retry_at)
        if first_observation is not None:
            LOGGER.info(
                "gate_pipeline stage=filesystem_ingress observed_at=%s pending_count=%d",
                first_observation.astimezone(timezone.utc).isoformat(),
                pending_count,
            )
        for dropped_path in dropped:
            self._reject(dropped_path, "candidate_coalesced")

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._retry_at)

    def ignores(self, path: Path) -> bool:
        candidate = Path(path).resolve()
        return any(
            candidate == root or candidate.is_relative_to(root)
            for root in self._ignored_roots
        )

    def retry_pending(self) -> int:
        now = self._clock()
        with self._lock:
            due = [
                path for path, (first_seen, retry_at, attempts, received_at)
                in self._retry_at.items()
                if retry_at <= now
            ]
        completed = 0
        for path in due:
            with self._lock:
                candidate = self._retry_at.get(path)
            if candidate is None:
                continue
            first_seen, _, attempts, received_at = candidate
            if attempts >= self._max_attempts or now - first_seen >= self._max_age or not path.exists():
                with self._lock:
                    self._retry_at.pop(path, None)
                self._reject(path, "upload_incomplete")
                continue
            if self._too_large(path):
                with self._lock:
                    self._retry_at.pop(path, None)
                self._reject(path, "image_too_large")
                continue
            if wait_until_readable(path, timeout=0, poll_interval=0):
                first_candidate = self._collector.add(path, received_at)
                with self._lock:
                    self._retry_at.pop(path, None)
                if first_candidate:
                    self._request_augmentation(received_at)
                completed += 1
            else:
                with self._lock:
                    if path in self._retry_at:
                        self._retry_at[path] = (first_seen, self._clock() + self._retry_interval,
                                                attempts + 1, received_at)
        return completed

    def _request_augmentation(self, received_at: datetime) -> None:
        request = "disabled"
        if self._on_first_completed is not None:
            try:
                request = (
                    "skipped"
                    if self._on_first_completed(received_at) is False
                    else "accepted"
                )
            except Exception:
                LOGGER.warning(
                    "gate_camera source=camera_ftp subtype=unverified "
                    "augmentation_request=failed reason=request_error"
                )
                return
        LOGGER.info(
            "gate_camera source=camera_ftp subtype=unverified augmentation_request=%s",
            request,
        )

    def _too_large(self, path: Path) -> bool:
        try:
            return path.stat().st_size > self._max_candidate_bytes
        except OSError:
            return False

    def _reject(self, path: Path, reason: str) -> None:
        try:
            if self._on_rejected is not None:
                self._on_rejected(path, reason)
        finally:
            _remove_upload(path)


class StartupReconciler:
    """Incrementally inspect uploads that completed while the daemon was offline."""

    def __init__(self, directory: Path, handler: CompletedImageHandler, *,
                 max_image_age: float = 8.0, on_skipped=None, clock=None):
        self._entries = [os.scandir(directory)]
        self._handler = handler
        self._max_image_age = max_image_age
        self._on_skipped = on_skipped
        self._clock = clock or time
        self._closed = False

    def run_batch(self, max_entries: int = MAX_STARTUP_ENTRIES) -> bool:
        if self._closed:
            return False
        for _ in range(max_entries):
            entry = self._next_entry()
            if entry is None:
                return False
            path = Path(entry.path)
            ignores = getattr(self._handler, "ignores", None)
            if callable(ignores) and ignores(path):
                continue
            if entry.is_dir(follow_symlinks=False):
                self._entries.append(os.scandir(path))
                continue
            if path.suffix.lower() not in {".jpg", ".jpeg"}:
                continue
            try:
                age = self._clock() - entry.stat(follow_symlinks=False).st_mtime
                stale = age < 0 or age > self._max_image_age
            except OSError:
                stale = True
            if stale:
                try:
                    if self._on_skipped is not None:
                        self._on_skipped(path)
                finally:
                    _remove_upload(path)
                continue
            self._handler.schedule_candidate(path, False)
        return True

    def _next_entry(self):
        while self._entries:
            try:
                return next(self._entries[-1])
            except StopIteration:
                self._entries.pop().close()
        self.close()
        return None

    def close(self) -> None:
        if not self._closed:
            while self._entries:
                self._entries.pop().close()
            self._closed = True


def reconcile_completed_images(directory: Path, handler: CompletedImageHandler, *,
                               max_image_age: float = 8.0, on_skipped=None,
                               max_entries: int = MAX_STARTUP_ENTRIES) -> bool:
    """Inspect one bounded batch of uploads that completed while offline."""
    reconciler = StartupReconciler(
        directory, handler, max_image_age=max_image_age, on_skipped=on_skipped
    )
    try:
        return reconciler.run_batch(max_entries)
    finally:
        reconciler.close()


def start_observer_then_reconcile(observer, directory: Path, handler: CompletedImageHandler,
                                  **reconcile_options) -> None:
    """Close the startup gap by observing before taking the filesystem snapshot."""
    observer.start()
    reconcile_completed_images(directory, handler, **reconcile_options)


def run_worker(directory: Path, emit, quiet_window: float = 0.5,
               poll_interval: float = 0.05, background_workers=(), max_pending_bursts: int = 2,
               max_image_age: float = 8.0, on_skipped=None, on_error=None,
               shutdown=None, max_burst_candidates: int = DEFAULT_MAX_BURST_CANDIDATES,
               max_candidate_bytes: int = DEFAULT_MAX_CANDIDATE_BYTES,
               on_timed_skipped=None, snapshot_sampling=None) -> None:
    """Watch completed JPEG uploads and process ranked bursts without blocking collection."""
    bursts = BoundedBurstQueue(max_pending_bursts)

    def report_dropped(item, reason):
        paths, received_at, *timing = item
        if on_timed_skipped is not None and timing:
            on_timed_skipped(paths, reason, received_at, *timing)
        elif on_skipped is not None:
            on_skipped(paths, reason, received_at)

    def enqueue_primary(item):
        dropped = bursts.put(item)
        if dropped is not None:
            try:
                report_dropped(dropped, "queue_coalesced")
            finally:
                _remove_uploads(dropped[0])

    def enqueue_augmentation(item):
        if bursts.try_put(item):
            return
        try:
            report_dropped(item, "augmentation_queue_full")
        finally:
            _remove_uploads(item[0])

    collector_options = {
        "quiet_window": quiet_window,
        "ranker": lambda paths: rank_images(paths, max_bytes=max_candidate_bytes),
        "include_received_at": True,
        "include_decision_started_at": True,
        "include_processing_started_at": True,
        "max_candidates": max_burst_candidates,
    }
    collector = BurstCollector(enqueue_primary, **collector_options)
    sampler = None
    augmentation_collector = None
    sampling_enabled = bool(snapshot_sampling is not None and snapshot_sampling.enabled)
    if snapshot_sampling is not None:
        if sampling_enabled:
            augmentation_collector = BurstCollector(
                enqueue_augmentation, **collector_options,
            )
            add_snapshot_candidate = augmentation_collector.add
        else:
            add_snapshot_candidate = lambda *_: None
        sampler = ReolinkSnapshotSampler(snapshot_sampling, add_snapshot_candidate)
    handler = CompletedImageHandler(
        collector,
        on_rejected=(lambda path, reason: on_skipped((path,), reason, None)) if on_skipped else None,
        max_candidate_bytes=max_candidate_bytes,
        max_pending_candidates=max_burst_candidates,
        on_first_completed=sampler.request if sampling_enabled else None,
        ignored_roots=(sampler.output_directory,) if sampler is not None else (),
    )
    observer = Observer()
    observer.schedule(handler, str(directory), recursive=True)
    stop_event = Event()
    failures = Queue()
    processing_thread = Thread(
        target=_supervise_worker,
        args=("GateBurstProcessor", _process_bursts, (bursts, emit, on_error),
              stop_event, failures),
        daemon=True, name="GateBurstProcessor",
    )
    sampling_thread = (
        Thread(
            target=_supervise_worker,
            args=("ReolinkSnapshotSampler", sampler.run_forever, (stop_event,),
                  stop_event, failures),
            daemon=True, name="ReolinkSnapshotSampler",
        ) if sampling_enabled else None
    )
    background_threads = [
        Thread(
            target=_supervise_worker,
            args=(worker.__class__.__name__, worker.run_forever, (stop_event,),
                  stop_event, failures),
            daemon=True, name=worker.__class__.__name__,
        )
        for worker in background_workers
    ]
    previous_sigterm = None
    if current_thread_is_main():
        previous_sigterm = _install_sigterm_handler(stop_event)
    observer_started = False
    startup_reconciler = None
    startup_reconciliation_pending = False
    processing_started = False
    sampling_started = False
    started_background_threads = []
    try:
        observer.start()
        observer_started = True
        startup_reconciler = StartupReconciler(
            directory, handler, max_image_age=max_image_age,
            on_skipped=(
                lambda path: on_skipped((path,), "stale_startup", None)
            ) if on_skipped else None,
        )
        startup_reconciliation_pending = startup_reconciler.run_batch()
        processing_thread.start()
        processing_started = True
        if sampling_thread is not None:
            sampling_thread.start()
            sampling_started = True
        for thread in background_threads:
            thread.start()
            started_background_threads.append(thread)
        while not stop_event.is_set():
            observer_alive = getattr(observer, "is_alive", None)
            if callable(observer_alive) and not observer_alive():
                raise WorkerFailure("critical worker watchdog Observer exited unexpectedly")
            if startup_reconciliation_pending:
                startup_reconciliation_pending = startup_reconciler.run_batch()
            handler.retry_pending()
            collector.flush_due()
            if augmentation_collector is not None:
                augmentation_collector.flush_due()
            sleep(poll_interval)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        try:
            if shutdown is not None:
                shutdown()
        finally:
            try:
                if startup_reconciler is not None:
                    startup_reconciler.close()
                if observer_started:
                    observer.stop()
                    observer.join()
                if sampling_started:
                    sampling_thread.join(
                        timeout=MAX_REOLINK_SNAPSHOT_TIMEOUT_SECONDS + 0.5
                    )
                if sampler is not None:
                    sampler.close()
                if processing_started:
                    dropped = bursts.put(None)
                    if dropped is not None:
                        try:
                            report_dropped(dropped, "service_stopping")
                        finally:
                            _remove_uploads(dropped[0])
                    processing_thread.join(timeout=5)
                for thread in started_background_threads:
                    thread.join(timeout=1)
            finally:
                if previous_sigterm is not None:
                    signal.signal(signal.SIGTERM, previous_sigterm)
    try:
        name, error = failures.get_nowait()
    except Empty:
        return
    if error is None:
        raise WorkerFailure(f"critical worker {name} exited unexpectedly")
    raise WorkerFailure(f"critical worker {name} failed: {error}") from error


def _process_bursts(bursts, emit, on_error=None) -> None:
    while True:
        item = bursts.get()
        if item is None:
            return
        paths, received_at, *timing = item
        try:
            emit(paths, received_at, *timing)
        except Exception as error:
            if on_error is not None:
                on_error(paths, error, received_at)
        finally:
            _remove_uploads(paths)


def _supervise_worker(name, target, args, stop_event, failures) -> None:
    try:
        target(*args)
    except BaseException as error:
        if stop_event.is_set():
            return
        failures.put((name, error))
        stop_event.set()
        return
    if not stop_event.is_set():
        failures.put((name, None))
        stop_event.set()


def current_thread_is_main() -> bool:
    from threading import current_thread, main_thread
    return current_thread() is main_thread()


def _install_sigterm_handler(stop_event: Event):
    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    return previous


def _remove_uploads(paths) -> None:
    for path in paths:
        _remove_upload(path)


def _remove_upload(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as error:
        LOGGER.warning("camera_upload_cleanup_failed path=%s error=%s", path, error)
