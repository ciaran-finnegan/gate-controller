import logging
import os
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
import signal
from datetime import datetime, timezone
from threading import Condition, Event, Lock, Thread
from time import monotonic, sleep, time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .images import rank_images, wait_until_readable
from .reolink_snapshots import (
    MAX_REOLINK_SNAPSHOT_TIMEOUT_SECONDS, ReolinkSnapshotSampler,
)
from .telemetry import ftp_fallback_trigger


DEFAULT_MAX_BURST_CANDIDATES = 8
DEFAULT_MAX_CANDIDATE_BYTES = 8 * 1024 * 1024
MAX_BURST_CANDIDATES = 16
MAX_CANDIDATE_BYTES = 16 * 1024 * 1024
MAX_STARTUP_ENTRIES = 128
MAX_FINAL_OCR_CANDIDATES = 3
_TRIGGER_UNSET = object()


LOGGER = logging.getLogger(__name__)


class WorkerFailure(RuntimeError):
    pass


class BoundedBurstQueue:
    """Keep fresh FTP work in a lane that snapshots cannot displace."""

    def __init__(self, max_pending: int = 2, on_discard=None):
        self._max_pending = max_pending
        self._on_discard = on_discard
        self._primary = deque()
        self._augmentation = deque()
        self._augmentation_reservations = 0
        self._stopping = False
        self._condition = Condition()

    def put(self, item):
        if item is None:
            self.stop()
            return None
        discard = False
        with self._condition:
            if self._stopping:
                discard = True
                dropped = None
            else:
                dropped = None
                if len(self._primary) >= self._max_pending:
                    dropped = self._primary.popleft()
                self._primary.append(item)
                self._condition.notify()
        if discard:
            self._discard((item,))
        return dropped

    def reserve_augmentation(self) -> bool:
        with self._condition:
            if self._stopping or self._augmentation_reservations >= self._max_pending:
                return False
            self._augmentation_reservations += 1
            return True

    def cancel_augmentation_reservation(self) -> None:
        with self._condition:
            if self._augmentation_reservations > len(self._augmentation):
                self._augmentation_reservations -= 1

    def put_augmentation(self, item):
        discard = False
        with self._condition:
            if self._stopping:
                discard = True
            elif self._augmentation_reservations <= len(self._augmentation):
                raise RuntimeError("augmentation queue slot was not reserved")
            else:
                self._augmentation.append(item)
                self._condition.notify()
        if discard:
            self._discard((item,))
        return None

    def stop(self) -> None:
        with self._condition:
            if self._stopping:
                return
            self._stopping = True
            discarded = tuple(self._primary) + tuple(self._augmentation)
            self._primary.clear()
            self._augmentation.clear()
            self._augmentation_reservations = 0
            self._condition.notify_all()
        self._discard(discarded)

    def get(self):
        with self._condition:
            while (
                not self._primary
                and not self._augmentation
                and not self._stopping
            ):
                self._condition.wait()
            if self._stopping:
                return None
            if self._primary:
                return self._primary.popleft()
            if self._augmentation:
                self._augmentation_reservations -= 1
                return self._augmentation.popleft()
            return None

    def _discard(self, items) -> None:
        if self._on_discard is None:
            return
        for item in items:
            try:
                self._on_discard(self, item)
            except Exception:
                LOGGER.exception("gate_burst_shutdown_cleanup_failed")


@dataclass
class ProgressiveTrigger:
    received_at: datetime
    release: object = lambda: None
    primary_item: tuple | None = None
    snapshots: tuple[Path, ...] | None = None
    initial_result: object | None = None
    failed: bool = False
    finalized: bool = False
    finalizing: bool = False
    augmentation_submitted: bool = False
    abandoned: bool = False
    augmentation_started_at: float | None = None
    augmentation_reason: str | None = None
    terminalizer: object | None = None
    trigger_summary: object | None = None
    trigger_resolved: bool = False
    _lock: Lock = field(default_factory=Lock, repr=False)
    _released: bool = False

    def release_once(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self.release()


@dataclass(frozen=True)
class BurstWork:
    kind: str
    item: tuple
    trigger: ProgressiveTrigger


@dataclass(frozen=True)
class CollectedBurst:
    item: tuple
    context: object


class BurstCollector:
    def __init__(self, emit, quiet_window: float = 0.5, ranker=rank_images, clock=monotonic,
                 arrival_clock=None, include_received_at: bool = False,
                 include_decision_started_at: bool = False,
                 include_processing_started_at: bool = False,
                 max_candidates: int = DEFAULT_MAX_BURST_CANDIDATES,
                 wall_clock=None, on_abandoned=None):
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
        self._on_abandoned = on_abandoned
        self._pending: list[Path] = []
        self._received_at: datetime | None = None
        self._first_seen: float | None = None
        self._deadline: float | None = None
        self._context = None
        self._lock = Lock()

    def add(self, path: Path, received_at: datetime | None = None) -> bool:
        if not getattr(path, "_descriptor_anchored", False):
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

    def bind_context(self, context) -> bool:
        with self._lock:
            if not self._pending or self._context is not None:
                return False
            self._context = context
            return True

    def flush_due(self) -> bool:
        with self._lock:
            decision_started_at = self._clock()
            if self._deadline is None or decision_started_at < self._deadline:
                return False
            processing_started_at = self._wall_clock()
            pending = tuple(self._pending)
            received_at = self._received_at
            first_seen = self._first_seen
            context = self._context
            self._pending = []
            self._received_at = None
            self._first_seen = None
            self._deadline = None
            self._context = None
        ranked = tuple(self._ranker(pending))
        ranked_paths = set(ranked)
        _remove_uploads(path for path in pending if path not in ranked_paths)
        if not ranked:
            if context is not None and self._on_abandoned is not None:
                self._on_abandoned(context)
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
        item = tuple(details) if len(details) > 1 else ranked
        self._emit(
            CollectedBurst(item, context) if context is not None else item
        )
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
                    context = self._request_augmentation(received_at)
                    bind_context = getattr(self._collector, "bind_context", None)
                    if context is not None and callable(bind_context):
                        bind_context(context)
                completed += 1
            else:
                with self._lock:
                    if path in self._retry_at:
                        self._retry_at[path] = (first_seen, self._clock() + self._retry_interval,
                                                attempts + 1, received_at)
        return completed

    def _request_augmentation(self, received_at: datetime):
        request = "disabled"
        context = None
        if self._on_first_completed is not None:
            try:
                result = self._on_first_completed(received_at)
                request = "skipped" if result is False else "accepted"
                if result is not None and result is not False and result is not True:
                    context = result
            except Exception:
                LOGGER.warning(
                    "gate_camera source=camera_ftp subtype=unverified "
                    "augmentation_request=failed reason=request_error"
                )
                return None
        LOGGER.info(
            "gate_camera source=camera_ftp subtype=unverified augmentation_request=%s",
            request,
        )
        return context

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
               on_timed_skipped=None, snapshot_sampling=None,
               trigger_resolver=None) -> None:
    """Watch completed JPEG uploads and process ranked bursts without blocking collection."""
    bursts = BoundedBurstQueue(
        max_pending_bursts,
        on_discard=lambda queue, item: _discard_burst_work(item, queue),
    )

    def report_dropped(item, reason):
        if isinstance(item, BurstWork):
            item = item.item
        paths, received_at, *timing = item
        options = {}
        if trigger_resolver is not None:
            options["trigger"] = ftp_fallback_trigger()
        if on_timed_skipped is not None and timing:
            on_timed_skipped(paths, reason, received_at, *timing, **options)
        elif on_skipped is not None:
            on_skipped(paths, reason, received_at, **options)

    def report_pre_ocr_skip(paths, reason, received_at=None):
        if on_skipped is None:
            return
        options = {}
        if trigger_resolver is not None:
            options["trigger"] = ftp_fallback_trigger()
        on_skipped(paths, reason, received_at, **options)

    def enqueue(item):
        work = (
            BurstWork("primary", item.item, item.context)
            if isinstance(item, CollectedBurst) else item
        )
        dropped = bursts.put(work)
        if dropped is not None:
            try:
                report_dropped(dropped, "queue_coalesced")
            finally:
                dropped_item = dropped.item if isinstance(dropped, BurstWork) else dropped
                _remove_uploads(dropped_item[0])
                if isinstance(dropped, BurstWork):
                    _abandon_progressive_trigger(dropped.trigger, bursts)

    collector_options = {
        "quiet_window": quiet_window,
        "ranker": lambda paths: rank_images(paths, max_bytes=max_candidate_bytes),
        "include_received_at": True,
        "include_decision_started_at": True,
        "include_processing_started_at": True,
        "max_candidates": max_burst_candidates,
        "on_abandoned": lambda trigger: _abandon_progressive_trigger(
            trigger, bursts
        ),
    }
    collector = BurstCollector(enqueue, **collector_options)
    sampler = None
    progressive_triggers = {}
    progressive_triggers_lock = Lock()
    sampling_enabled = bool(snapshot_sampling is not None and snapshot_sampling.enabled)

    def release_trigger(key, trigger):
        with progressive_triggers_lock:
            if progressive_triggers.get(key) is trigger:
                progressive_triggers.pop(key, None)

    def complete_augmentation(paths, received_at):
        paths = tuple(paths)
        key = id(received_at)
        with progressive_triggers_lock:
            trigger = progressive_triggers.get(key)
        if trigger is None:
            _remove_uploads(paths)
            return
        with trigger._lock:
            if trigger.abandoned:
                _remove_uploads(paths)
                return
            trigger.snapshots = paths
            trigger.augmentation_submitted = True
            work = BurstWork(
                "augmentation", (paths, trigger.received_at), trigger
            )
        bursts.put_augmentation(work)

    if snapshot_sampling is not None:
        sampler = ReolinkSnapshotSampler(snapshot_sampling, complete_augmentation)

    def request_augmentation(received_at):
        if sampler is None or not bursts.reserve_augmentation():
            return False
        key = id(received_at)
        trigger = ProgressiveTrigger(
            received_at, augmentation_started_at=monotonic(),
        )
        trigger.release = lambda: release_trigger(key, trigger)
        with progressive_triggers_lock:
            progressive_triggers[key] = trigger
        if sampler.request(received_at):
            return trigger
        with progressive_triggers_lock:
            progressive_triggers.pop(key, None)
        bursts.cancel_augmentation_reservation()
        return False

    handler = CompletedImageHandler(
        collector,
        on_rejected=(
            lambda path, reason: report_pre_ocr_skip((path,), reason)
        ) if on_skipped else None,
        max_candidate_bytes=max_candidate_bytes,
        max_pending_candidates=max_burst_candidates,
        on_first_completed=request_augmentation if sampling_enabled else None,
        ignored_roots=(sampler.output_directory,) if sampling_enabled else (),
    )
    observer = Observer()
    observer.schedule(handler, str(directory), recursive=True)
    stop_event = Event()
    failures = Queue()
    processing_args = (
        bursts, emit, on_error,
        lambda paths: rank_images(paths, max_bytes=max_candidate_bytes),
    )
    if trigger_resolver is not None:
        processing_args += (trigger_resolver,)
    processing_thread = Thread(
        target=_supervise_worker,
        args=("GateBurstProcessor", _process_bursts, processing_args,
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
                lambda path: report_pre_ocr_skip((path,), "stale_startup")
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
            sleep(poll_interval)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        bursts.put(None)
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


def _process_bursts(
    bursts, emit, on_error=None, ranker=None, trigger_resolver=None,
) -> None:
    ranker = ranker or rank_images
    while True:
        item = bursts.get()
        if item is None:
            return
        if isinstance(item, BurstWork):
            _process_progressive_work(
                item, emit, on_error, bursts, ranker, trigger_resolver,
            )
            continue
        paths, received_at, *timing = item
        trigger_summary = _TRIGGER_UNSET
        try:
            options = {}
            if trigger_resolver is not None:
                trigger_summary = _resolve_trigger(
                    trigger_resolver, received_at,
                )
                options["trigger"] = trigger_summary
            emit(paths, received_at, *timing, **options)
        except Exception as error:
            _report_processing_error(
                on_error, paths, error, received_at, trigger_summary,
            )
        finally:
            _remove_uploads(paths)


def _process_progressive_work(
    work: BurstWork, emit, on_error, bursts, ranker, trigger_resolver=None,
) -> None:
    trigger = work.trigger
    paths, received_at, *timing = work.item
    paths = tuple(paths)
    if work.kind == "augmentation":
        with trigger._lock:
            already_finalized = trigger.finalized
            trigger.snapshots = paths
            waiting_for_primary = (
                trigger.initial_result is None and not trigger.failed
            )
        if already_finalized:
            _cleanup_progressive_trigger(trigger, paths)
            return
        if waiting_for_primary:
            return
        _finalize_progressive_trigger(trigger, emit, on_error, ranker)
        return

    with trigger._lock:
        trigger.primary_item = (paths, received_at, *timing)
        trigger.terminalizer = lambda: _finalize_progressive_trigger(
            trigger, emit, on_error, ranker,
        )
    options = {"final": False}
    if trigger_resolver is not None:
        trigger_summary = _resolve_trigger(trigger_resolver, received_at)
        with trigger._lock:
            trigger.trigger_summary = trigger_summary
            trigger.trigger_resolved = True
        options["trigger"] = trigger_summary
    try:
        result = emit(paths, received_at, *timing, **options)
    except Exception as error:
        _report_processing_error(
            on_error, paths, error, received_at,
            trigger.trigger_summary if trigger.trigger_resolved else _TRIGGER_UNSET,
        )
        _remove_uploads(paths)
        _abandon_progressive_trigger(trigger, bursts)
        return
    with trigger._lock:
        trigger.initial_result = result
        snapshots = trigger.snapshots
        abandoned = trigger.abandoned
    if getattr(result, "terminal", True):
        _remove_uploads(paths)
        _abandon_progressive_trigger(trigger, bursts)
        return
    if abandoned:
        _finalize_progressive_trigger(trigger, emit, on_error, ranker)
        return
    if snapshots is not None:
        _finalize_progressive_trigger(trigger, emit, on_error, ranker)


def _finalize_progressive_trigger(trigger: ProgressiveTrigger, emit, on_error, ranker) -> None:
    with trigger._lock:
        if trigger.finalized or trigger.finalizing:
            return
        snapshots = trigger.snapshots or ()
        primary_item = trigger.primary_item
        failed = trigger.failed
        result = trigger.initial_result
        if primary_item is None or result is None:
            if not failed:
                return
            trigger.finalizing = True
            cleanup_paths = tuple(snapshots)
            primary_item = None
        else:
            trigger.finalizing = True
            cleanup_paths = tuple(primary_item[0]) + tuple(snapshots)
        augmentation_started_at = trigger.augmentation_started_at
        augmentation_reason = trigger.augmentation_reason
    try:
        if primary_item is None:
            return
        primary_paths, received_at, *timing = primary_item
        if getattr(result, "terminal", True):
            return
        combined = tuple(primary_paths) + tuple(snapshots)
        augmentation_failed = failed or not snapshots
        selected = tuple(primary_paths)
        if not augmentation_failed:
            try:
                selected = tuple(ranker(combined))[:MAX_FINAL_OCR_CANDIDATES]
            except Exception as error:
                _report_processing_error(
                    on_error, combined, error, received_at,
                    trigger.trigger_summary
                    if trigger.trigger_resolved else _TRIGGER_UNSET,
                )
                augmentation_failed = True
                augmentation_reason = "selection_error"
            else:
                augmentation_failed = not selected
        options = {
            "idempotency_key": result.idempotency_key,
            "final": True,
        }
        if trigger.trigger_resolved:
            options["trigger"] = trigger.trigger_summary
        if augmentation_failed:
            selected = tuple(primary_paths)
            options["provisional_result"] = result
            options["augmentation"] = {
                "outcome": "failed",
                "reason": augmentation_reason or (
                    "empty" if not snapshots else "selection_empty"
                ),
                "candidate_count": len(snapshots),
                "duration_ms": _augmentation_duration_ms(augmentation_started_at),
            }
        else:
            options["augmentation"] = {
                "outcome": "completed",
                "reason": "completed",
                "candidate_count": len(snapshots),
                "duration_ms": _augmentation_duration_ms(augmentation_started_at),
                "correlation": result.idempotency_key,
            }
        try:
            emit(selected, received_at, *timing, **options)
        except Exception as error:
            _report_processing_error(
                on_error, combined, error, received_at,
                trigger.trigger_summary
                if trigger.trigger_resolved else _TRIGGER_UNSET,
            )
    finally:
        _cleanup_progressive_trigger(trigger, cleanup_paths)
        with trigger._lock:
            trigger.finalizing = False
            trigger.finalized = True


def _resolve_trigger(trigger_resolver, received_at):
    try:
        return trigger_resolver(received_at)
    except Exception:
        LOGGER.warning("gate_camera trigger_correlation=failed", exc_info=True)
        return ftp_fallback_trigger()


def _abandon_progressive_trigger(
    trigger: ProgressiveTrigger, bursts: BoundedBurstQueue, reason: str = "abandoned",
) -> None:
    with trigger._lock:
        if trigger.abandoned:
            return
        trigger.abandoned = True
        trigger.failed = True
        trigger.augmentation_reason = reason
        snapshots = trigger.snapshots or ()
        cancel_reservation = not trigger.augmentation_submitted
        result = trigger.initial_result
        terminalizer = trigger.terminalizer
        primary_item = trigger.primary_item
    if cancel_reservation:
        bursts.cancel_augmentation_reservation()
    if result is not None and not getattr(result, "terminal", True) and terminalizer:
        terminalizer()
        return
    primary_paths = primary_item[0] if primary_item is not None else ()
    _cleanup_progressive_trigger(trigger, tuple(primary_paths) + tuple(snapshots))


def _discard_burst_work(item, bursts: BoundedBurstQueue) -> None:
    if isinstance(item, BurstWork):
        _abandon_progressive_trigger(item.trigger, bursts, reason="shutdown")
        _remove_uploads(item.item[0])
        return
    _remove_uploads(item[0])


def _augmentation_duration_ms(started_at: float | None) -> int:
    if started_at is None:
        return 0
    return max(0, round((monotonic() - started_at) * 1_000))


def _report_processing_error(
    on_error, paths, error, received_at, trigger=_TRIGGER_UNSET,
) -> None:
    if on_error is None:
        return
    try:
        if trigger is _TRIGGER_UNSET:
            on_error(paths, error, received_at)
        else:
            on_error(paths, error, received_at, trigger=trigger)
    except Exception:
        LOGGER.exception("gate_burst_error_handler_failed")


def _cleanup_progressive_trigger(trigger: ProgressiveTrigger, paths) -> None:
    try:
        _remove_uploads(paths)
    except Exception:
        LOGGER.exception("gate_burst_cleanup_failed")
    finally:
        try:
            trigger.release_once()
        except Exception:
            LOGGER.exception("gate_burst_release_failed")


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
        if getattr(path, "_descriptor_anchored", False):
            path.unlink(missing_ok=True)
        else:
            Path(path).unlink(missing_ok=True)
    except OSError as error:
        LOGGER.warning("camera_upload_cleanup_failed path=%s error=%s", path, error)
