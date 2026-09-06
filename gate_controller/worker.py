import logging
import os
from pathlib import Path
from queue import Empty, Queue
import signal
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from time import monotonic, sleep, time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .images import content_digest, rank_images, wait_until_readable
from .telemetry import ftp_fallback_trigger, TriggerTelemetry


DEFAULT_MAX_BURST_CANDIDATES = 8
DEFAULT_MAX_CANDIDATE_BYTES = 8 * 1024 * 1024
MAX_BURST_CANDIDATES = 16
MAX_CANDIDATE_BYTES = 16 * 1024 * 1024
MAX_STARTUP_ENTRIES = 128
_TRIGGER_UNSET = object()


LOGGER = logging.getLogger(__name__)


class WorkerFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class BurstIdentity:
    idempotency_key: str
    # A burst injected by webhook-triggered capture carries its own sanitized
    # trigger; the FTP path resolves one from the correlator instead.
    trigger: TriggerTelemetry | None = None


class BoundedBurstQueue:
    """A one-consumer queue that keeps the freshest pending camera work.

    A full queue coalesces the oldest burst away, except that a
    webhook-triggered frame is given up only when there is nothing else to
    give up: a presence session holds one frame outstanding at a time, and an
    FTP upload landing in the same second must not push it out.
    """

    def __init__(self, max_pending: int = 2):
        if max_pending <= 0:
            raise ValueError("max_pending must be positive")
        self._queue = Queue(maxsize=max_pending)
        self._lock = Lock()
        self._stopping = False

    def put(self, item):
        with self._lock:
            if self._stopping:
                return item
            dropped = self._coalesce() if self._queue.full() else None
            self._queue.put_nowait(item)
            return dropped

    def _coalesce(self):
        """Remove the oldest untriggered burst, or the oldest burst of all."""
        held = []
        while True:
            try:
                held.append(self._queue.get_nowait())
            except Empty:
                break
        if len(held) < self._queue.maxsize:
            # The consumer took a burst between the full() check and this
            # drain, so there is room after all: nothing has to be given up.
            for candidate in held:
                self._queue.put_nowait(candidate)
            return None
        index = next(
            (position for position, candidate in enumerate(held)
             if not _is_triggered(candidate)),
            0,
        )
        dropped = held.pop(index)
        for candidate in held:
            self._queue.put_nowait(candidate)
        return dropped

    def get(self):
        return self._queue.get()

    def stop(self) -> tuple:
        with self._lock:
            if self._stopping:
                return ()
            self._stopping = True
            dropped = []
            while True:
                try:
                    dropped.append(self._queue.get_nowait())
                except Empty:
                    break
            self._queue.put_nowait(None)
            return tuple(dropped)


def _is_triggered(item) -> bool:
    """True for a burst injected by webhook capture: it carries its trigger."""
    identity = item[-1] if item else None
    return isinstance(identity, BurstIdentity) and identity.trigger is not None


class BurstCollector:
    def __init__(self, emit, quiet_window: float = 0.5, ranker=rank_images, clock=monotonic,
                 arrival_clock=None, include_received_at: bool = False,
                 include_decision_started_at: bool = False,
                 include_processing_started_at: bool = False,
                 include_idempotency_key: bool = False,
                 prefer_first_candidate: bool = False,
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
        self._include_idempotency_key = include_idempotency_key
        self._prefer_first_candidate = prefer_first_candidate
        self._max_candidates = max_candidates
        self._pending: list[Path] = []
        self._received_at: datetime | None = None
        self._idempotency_key: str | None = None
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
                if self._include_idempotency_key:
                    try:
                        self._idempotency_key = content_digest(path)
                    except OSError:
                        self._idempotency_key = None
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

    @property
    def remaining_capacity(self) -> int:
        with self._lock:
            return max(0, self._max_candidates - len(self._pending))

    def flush_due(self) -> bool:
        with self._lock:
            decision_started_at = self._clock()
            if self._deadline is None or decision_started_at < self._deadline:
                return False
            processing_started_at = self._wall_clock()
            pending = tuple(self._pending)
            received_at = self._received_at
            idempotency_key = self._idempotency_key
            first_seen = self._first_seen
            self._pending = []
            self._received_at = None
            self._idempotency_key = None
            self._first_seen = None
            self._deadline = None
        ranked = tuple(self._ranker(pending))
        if self._prefer_first_candidate and pending and pending[0] in ranked:
            primary = pending[0]
            ranked = (primary, *(path for path in ranked if path != primary))
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
        if self._include_idempotency_key and idempotency_key is not None:
            details.append(BurstIdentity(idempotency_key))
        self._emit(tuple(details) if len(details) > 1 else ranked)
        return True


class CompletedImageHandler(FileSystemEventHandler):
    # 50 ms polling keeps ingress fast; 100 attempts preserves the same five
    # second readability window a slow FTP transfer had at 250 ms x 20.
    def __init__(self, collector: BurstCollector, retry_interval: float = 0.05,
                 max_attempts: int = 100, max_age: float = 30.0, clock=monotonic,
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
                    self._add_hot_frames(received_at)
                completed += 1
            else:
                with self._lock:
                    if path in self._retry_at:
                        self._retry_at[path] = (first_seen, self._clock() + self._retry_interval,
                                                attempts + 1, received_at)
        return completed

    def _add_hot_frames(self, received_at: datetime) -> None:
        if self._on_first_completed is None:
            return
        selected = ()
        added = 0
        try:
            selected = tuple(self._on_first_completed(received_at) or ())
            capacity = min(2, self._collector.remaining_capacity)
            for path in selected[:capacity]:
                self._collector.add(Path(path), received_at)
                added += 1
            _remove_uploads(Path(path) for path in selected[capacity:])
            LOGGER.info("gate_hot_stream added_to_burst=%d", added)
        except Exception:
            _remove_uploads(Path(path) for path in selected[added:])
            LOGGER.warning("gate_hot_stream added_to_burst=0 reason=selection_error")

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
               on_timed_skipped=None, trigger_resolver=None,
               hot_frame_provider=None, trigger_capture=None, on_result=None) -> None:
    """Watch completed JPEG uploads and process ranked bursts without blocking collection."""
    bursts = BoundedBurstQueue(max_pending_bursts)
    note_result = getattr(trigger_capture, "note_result", None)
    if callable(note_result):
        provided = on_result

        def on_result(paths, result, _note=note_result, _provided=provided):
            _note(paths, result)
            if _provided is not None:
                _provided(paths, result)

    note_dropped = getattr(trigger_capture, "note_dropped", None)

    def report_lost(paths, reason):
        """Tell the capture a frame of its own never reached a decision.

        Every path that leaves the pipeline without a result comes through
        here, so a presence session's pending count can never stick.
        """
        if not callable(note_dropped):
            return
        try:
            note_dropped(paths, reason)
        except Exception:
            LOGGER.exception("gate_burst_drop_handler_failed")

    def report_dropped(item, reason):
        paths, received_at, *timing = item
        identity = timing.pop() if timing and isinstance(timing[-1], BurstIdentity) else None
        options = {}
        if identity is not None and identity.trigger is not None:
            options["trigger"] = identity.trigger
        elif trigger_resolver is not None:
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
        dropped = bursts.put(item)
        if dropped is not None:
            try:
                report_dropped(dropped, "queue_coalesced")
            finally:
                report_lost(dropped[0], "queue_coalesced")
                _remove_uploads(dropped[0])

    def inject_trigger_burst(paths, received_at, trigger):
        # A webhook-triggered clear frame enters the same bounded queue as an
        # FTP burst, with its own content identity and sanitized trigger.
        paths = tuple(Path(path) for path in paths)
        identity = BurstIdentity(content_digest(paths[0]), trigger)
        enqueue((paths, received_at, monotonic(), datetime.now(timezone.utc), identity))

    if trigger_capture is not None:
        trigger_capture.attach(inject_trigger_burst)

    collector = BurstCollector(
        enqueue, quiet_window=quiet_window,
        ranker=lambda paths: rank_images(paths, max_bytes=max_candidate_bytes),
        include_received_at=True, include_decision_started_at=True,
        include_processing_started_at=True,
        include_idempotency_key=True,
        prefer_first_candidate=True,
        max_candidates=max_burst_candidates,
    )
    handler = CompletedImageHandler(
        collector,
        on_rejected=(
            lambda path, reason: report_pre_ocr_skip((path,), reason)
        ) if on_skipped else None,
        max_candidate_bytes=max_candidate_bytes,
        max_pending_candidates=max_burst_candidates,
        on_first_completed=(
            hot_frame_provider.select if hot_frame_provider is not None else None
        ),
        ignored_roots=(
            ((hot_frame_provider.output_directory,)
             if hot_frame_provider is not None else ())
            + ((trigger_capture.output_directory,)
               if trigger_capture is not None else ())
        ),
    )
    observer = Observer()
    observer.schedule(handler, str(directory), recursive=True)
    stop_event = Event()
    failures = Queue()
    processing_args = (
        bursts, emit, on_error,
        lambda paths: rank_images(paths, max_bytes=max_candidate_bytes),
        trigger_resolver, on_result, report_lost,
    )
    processing_thread = Thread(
        target=_supervise_worker,
        args=("GateBurstProcessor", _process_bursts, processing_args,
              stop_event, failures),
        daemon=True, name="GateBurstProcessor",
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
        if processing_started:
            for dropped in bursts.stop():
                if dropped is None:
                    continue
                try:
                    report_dropped(dropped, "service_stopping")
                finally:
                    report_lost(dropped[0], "service_stopping")
                    _remove_uploads(dropped[0])
            processing_thread.join()
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
    bursts, emit, on_error=None, ranker=None, trigger_resolver=None, on_result=None,
    on_dropped=None,
) -> None:
    ranker = ranker or rank_images
    while True:
        item = bursts.get()
        if item is None:
            return
        paths, received_at, *timing = item
        trigger_summary = _TRIGGER_UNSET
        try:
            options = {}
            identity = None
            if timing and isinstance(timing[-1], BurstIdentity):
                identity = timing.pop()
                options["idempotency_key"] = identity.idempotency_key
            if identity is not None and identity.trigger is not None:
                trigger_summary = identity.trigger
                options["trigger"] = trigger_summary
            elif trigger_resolver is not None:
                trigger_summary = _resolve_trigger(
                    trigger_resolver, received_at,
                )
                options["trigger"] = trigger_summary
            result = emit(paths, received_at, *timing, **options)
        except Exception as error:
            _report_processing_error(
                on_error, paths, error, received_at, trigger_summary,
            )
            # No result exists, so the frame is lost as far as its capture is
            # concerned; say so instead of leaving it pending.
            if on_dropped is not None:
                try:
                    on_dropped(paths, "processing_error")
                except Exception:
                    LOGGER.exception("gate_burst_drop_handler_failed")
        else:
            if on_result is not None:
                try:
                    on_result(paths, result)
                except Exception:
                    LOGGER.exception("gate_burst_result_handler_failed")
        finally:
            _remove_uploads(paths)


def _resolve_trigger(trigger_resolver, received_at):
    try:
        return trigger_resolver(received_at)
    except Exception:
        LOGGER.warning("gate_camera trigger_correlation=failed", exc_info=True)
        return ftp_fallback_trigger()


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
