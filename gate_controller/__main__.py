import argparse
import ipaddress
import logging
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Thread
from urllib.parse import urlparse

from .audio import PromptPlayer
from .actuation import ActuationCoordinator
from .backpressure import ActivityGate, DEFAULT_QUIET_SECONDS, bounded_quiet_seconds
from .authorisation import (
    AuthorisationRefreshWorker, AuthorisedPlateCache, CloudflarePlateFetcher,
)
from .cloudflare_client import (
    CloudflareMetricsReporter, CloudflareServiceClient, CloudflareStatusReporter,
)
from .command_server import CommandServerWorker, DirectCommandExecutor
from .control_plane import HeartbeatWorker
from .direction import DirectionTracker, load_direction_config
from .host_metrics import read_host_metrics
from .hot_stream import HotStreamBuffer, load_hot_stream_config
from .local_recognizer import build_local_recognizer
from .net_probe import NetProbeWorker, load_net_probe_config
from .ocr import MAX_UPLOAD_WIDTH, MIN_UPLOAD_WIDTH
from .plate_region import parse_plate_region
from .trigger_capture import (
    ClearKeyframeBuffer, TriggerFrameCapture, load_trigger_capture_config,
)
from .camera_control_state import (
    CAMERA_CONTROL_STATE_PATH, read_camera_control_state,
)
from .media_capabilities import read_media_capabilities
from .metrics import (
    MetricsRollupWorker, build_metrics_ring, metrics_rollup_seconds,
)
from .ocr import PlateRecognizerClient
from .outbox import (
    CloudflareOutboxSender, HttpOutboxSender, OutboxWorker,
    TelemetryRetentionWorker,
)
from .corpus_upload import (
    CloudflareCorpusSender, CorpusUploadWorker, load_corpus_upload_config,
)
from .processor import GateProcessor
from .relay import PiRelayAdapter, RelayController
from .settings import (
    CloudflareSettingsFetcher, MatchPolicyCache, SettingsRefreshWorker,
)
from .reolink_events import (
    ReolinkEventCorrelator, ReolinkWebhookWorker,
    load_reolink_webhook_config,
)
from .store import LocalStore
from .telemetry_export import export_telemetry
from .worker import (
    DEFAULT_MAX_BURST_CANDIDATES, DEFAULT_MAX_CANDIDATE_BYTES,
    MAX_BURST_CANDIDATES, MAX_CANDIDATE_BYTES, run_worker,
)
from .runtime import require_python_version


MIN_QUIET_WINDOW_SECONDS = 0.1
MAX_QUIET_WINDOW_SECONDS = 2.0
DEFAULT_QUIET_WINDOW_SECONDS = 0.2
MANAGED_RELEASES_ROOT = Path("/opt/gate-controller-deploy/releases")
MANAGED_RELEASE_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


def main() -> None:
    require_python_version()
    logging.basicConfig(
        level=os.environ.get("GATE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if len(sys.argv) > 1 and sys.argv[1] == "telemetry-export":
        _run_telemetry_export(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(description="Watch completed gate-camera uploads")
    authorised_default, database_default = default_runtime_paths(os.environ)
    parser.add_argument("--directory", type=Path,
                        default=Path(os.environ.get("GATE_WATCH_DIRECTORY", "/home/ftp-user")))
    parser.add_argument("--authorised-plates", type=Path,
                        default=authorised_default)
    parser.add_argument("--database", type=Path,
                        default=database_default)
    parser.add_argument(
        "--quiet-window", type=_quiet_window, default=DEFAULT_QUIET_WINDOW_SECONDS
    )
    arguments = parser.parse_args()
    token = os.environ.get("PLATE_RECOGNIZER_API_TOKEN")
    if not token:
        parser.error("PLATE_RECOGNIZER_API_TOKEN is required")

    # Read with the rest of the configuration, before a relay is claimed, a
    # store is recovered or a recogniser thread is started. The one value it
    # refuses is a *loosened* direction gate, and it used to refuse it far
    # below here, once the recogniser's threads were running and the store had
    # already recovered its interrupted actuations -- a shadow signal's typo
    # taking the controller down half-way up. Everything else it can only
    # journal and default.
    direction_config = load_direction_config(os.environ)
    relay = RelayController(PiRelayAdapter())
    store = LocalStore(arguments.database)
    store.recover_interrupted_actuations()
    # Off unless GATE_AUDIO_CAPTURE_ENABLED is set: no thread, no child, and
    # nothing recorded. Built before the coordinator because the coordinator is
    # the sole owner of the relay, and the relay is what labels the clips.
    audio_capture = _audio_capture_recorder(os.environ)
    coordinator = ActuationCoordinator(
        store, relay, timedelta(seconds=20), activation_observer=audio_capture,
    )
    max_image_age = float(os.environ.get("GATE_MAX_IMAGE_AGE_SECONDS", "8"))
    decision_timeout = float(os.environ.get("GATE_DECISION_TIMEOUT_SECONDS", "4"))
    max_burst_candidates, max_candidate_bytes = image_runtime_limits(os.environ)
    hot_stream_config = load_hot_stream_config(os.environ, arguments.directory)
    hot_stream = HotStreamBuffer(hot_stream_config) if hot_stream_config.enabled else None
    authorised = AuthorisedPlateCache(
        arguments.authorised_plates,
        max_staleness=(
            authorisation_max_staleness(os.environ) if _cloud_configured(os.environ) else None
        ),
    )
    latest_image = {"path": None, "received_at": None}
    # The priority ladder for one 4.5 Mbit/s uplink: gate decisions, then
    # event delivery, then the corpus. Built here because the pipeline marks
    # it and the corpus uploader reads it, and both are wired below.
    activity = ActivityGate(
        quiet_seconds=_corpus_quiet_seconds(os.environ),
        # Read through a lambda, not bound here: a store that cannot answer
        # must make the corpus stand down, not stop the controller starting.
        pending_events=lambda: store.pending_outbox_count(),
    )
    match_policy = MatchPolicyCache(
        Path(arguments.database).resolve().parent / "match-policy.json"
    )
    plate_region = parse_plate_region(os.environ.get("GATE_PLATE_REGION"))
    # Off unless GATE_LOCAL_OCR_MODE is set: nothing is imported or loaded.
    local_recognizer = build_local_recognizer(os.environ, plate_region=plate_region)
    if local_recognizer is not None:
        local_recognizer.start()
    # trigger_capture is built before the background workers so the heartbeat
    # status closure can see it. Building it afterwards is what left
    # presence.unresolved, dropped_frames and lost_verdicts incrementing on
    # the Pi and never reaching the cloud, while the docs claimed otherwise.
    trigger_capture_config = load_trigger_capture_config(
        os.environ, Path(arguments.database).resolve().parent,
        webhook_enabled=load_reolink_webhook_config(os.environ).enabled,
    )
    clear_keyframes = _clear_stream_source(trigger_capture_config)
    trigger_capture = (
        TriggerFrameCapture(
            trigger_capture_config, frame_source=clear_keyframes,
            activity=activity,
        )
        if trigger_capture_config.enabled else None
    )
    corpus = _training_corpus(os.environ)
    # The metrics ring is built here, before the workers, because two things
    # need the same instance: the rollup worker that posts it, and the burst
    # pipeline below that fills it. `None` when GATE_METRICS_ENABLED is false,
    # and then nothing is constructed and no thread runs.
    metrics = build_metrics_ring(
        os.environ, state_directory=Path(arguments.database).resolve().parent,
    )
    background_workers, _, _ = build_background_workers(
        store, relay, latest_image=latest_image, coordinator=coordinator,
        authorised=authorised, camera_directory=arguments.directory,
        hot_stream=hot_stream, match_policy=match_policy,
        local_recognizer=local_recognizer,
        trigger_capture=trigger_capture,
        corpus=corpus, activity=activity, metrics=metrics,
    )
    # Shadow only: it reads boxes the pipeline already produced and journals
    # a verdict. It reaches no decision, spends no lookup and ends no
    # presence session; gate-controller#95 is where acting on it lives.
    direction = DirectionTracker(direction_config)
    recognizer = PlateRecognizerClient(
        token, max_upload_width=_ocr_upload_width(os.environ),
        plate_region=plate_region,
        corpus=corpus,
        direction=direction,
        activity=activity,
        local_recognizer=local_recognizer,
        authorised=authorised.get,
        # The very same provider the GateProcessor below is given. The local
        # admission gate has to run under the band the processor is about to
        # apply to the same frame; without it a fuzzy local read would be
        # admitted under `standard` inside a `strict` band, spending the frame
        # the cloud would have read exactly.
        match_policy=match_policy.get,
        # Frames the keyframe decoder already cropped must not be cropped again.
        precropped_directory=(
            trigger_capture_config.output_directory
            if trigger_capture_config.plate_region is not None
            and trigger_capture_config.crop_capture else None
        ),
    )
    trigger_correlator, trigger_workers = build_reolink_trigger_pipeline(
        os.environ,
        on_accepted=_camera_event_handler(trigger_capture, recognizer, audio_capture),
    )
    background_workers = tuple(background_workers) + tuple(trigger_workers)
    if audio_capture is not None:
        background_workers += (audio_capture,)
    if hot_stream is not None:
        background_workers += (hot_stream,)
    if clear_keyframes is not None:
        background_workers += (clear_keyframes,)
    if trigger_capture is not None:
        background_workers += (trigger_capture,)
    outbox = next((worker for worker in background_workers if isinstance(worker, OutboxWorker)), None)
    processor = GateProcessor(
        recognizer=recognizer,
        store=store,
        relay=relay,
        authorised=authorised.get,
        cooldown=timedelta(seconds=20),
        outbox=outbox,
        coordinator=coordinator,
        max_image_age=timedelta(seconds=max_image_age),
        decision_timeout=decision_timeout,
        match_policy=match_policy.get,
        min_cloud_request_seconds=_min_cloud_request_seconds(os.environ),
        cloud_skip_stillness=_cloud_skip_stillness(os.environ),
    )

    def prepare(paths, received_at=None, decision_started_at=None,
                processing_started_at=None, *, trigger=None,
                idempotency_key=None, stillness=None):
        # The fast lane's half of a decision: identity, trace and the
        # on-device read, never the network. `process` finishes it.
        latest_image["path"] = str(paths[0]) if paths else None
        latest_image["received_at"] = (received_at or datetime.now(timezone.utc)).isoformat()
        with activity.activity("burst"):
            return processor.prepare(
                paths,
                received_at=received_at,
                decision_started_at=decision_started_at,
                processing_started_at=processing_started_at,
                trigger=trigger,
                idempotency_key=idempotency_key,
                stillness=stillness,
            )

    def process(paths, received_at=None, decision_started_at=None,
                processing_started_at=None, *, trigger=None,
                idempotency_key=None, prepared=None):
        latest_image["path"] = str(paths[0]) if paths else None
        latest_image["received_at"] = (received_at or datetime.now(timezone.utc)).isoformat()
        # Held for the whole burst: recognition, the decision and the relay
        # pulse. A frame from the FTP path never reaches trigger_capture's
        # span, so this is where that path claims the link.
        with activity.activity("burst"):
            result = processor.process(
                paths,
                received_at=received_at,
                decision_started_at=decision_started_at,
                processing_started_at=processing_started_at,
                trigger=trigger,
                idempotency_key=idempotency_key,
                prepared=prepared,
            )
        # Counted after the burst has answered and released the gate, from the
        # telemetry the processor already built. The decision path is not
        # touched, and a metric can never delay a relay pulse.
        #
        # Counted here, but credited to the minute the burst *started*, which
        # the telemetry carries: recording is what happens late, and a burst
        # that runs past a minute boundary is the slow one whose counters most
        # want to be in the right minute. Same rule, and the same refusals, as
        # the 429 counter in `MetricsRing._absorb_retry_counts_locked`.
        if metrics is not None:
            metrics.record_processing_result(result)
        return result

    def record_skipped(paths, reason, received_at, decision_started_at=None,
                       processing_started_at=None, *, trigger=None):
        logging.getLogger(__name__).warning("image_burst_skipped reason=%s count=%d", reason,
                                            len(paths))
        return processor.record_skipped(
            paths,
            reason,
            received_at,
            decision_started_at=decision_started_at,
            processing_started_at=processing_started_at,
            trigger=trigger,
        )

    def record_error(paths, error, received_at, *, trigger=None):
        logging.getLogger(__name__).exception(
            "image_burst_failed count=%d error=%s", len(paths), error,
            exc_info=(type(error), error, error.__traceback__),
        )
        try:
            processor.record_skipped(
                paths, "processing_error", received_at, trigger=trigger,
            )
        except Exception:
            logging.getLogger(__name__).exception("processing_error_event_failed")

    net_probe = next(
        (worker for worker in background_workers if isinstance(worker, NetProbeWorker)),
        None,
    )

    def shutdown():
        return _shutdown_controller_with_hot_stream(
            hot_stream, processor, relay, trigger_capture=trigger_capture,
            clear_keyframes=clear_keyframes, net_probe=net_probe,
            audio_capture=audio_capture,
        )

    run_worker(
        arguments.directory, process, quiet_window=arguments.quiet_window,
        background_workers=background_workers,
        max_image_age=max_image_age,
        on_skipped=record_skipped,
        on_timed_skipped=record_skipped,
        on_error=record_error,
        shutdown=shutdown,
        max_burst_candidates=max_burst_candidates,
        max_candidate_bytes=max_candidate_bytes,
        trigger_resolver=trigger_correlator.correlate,
        hot_frame_provider=hot_stream,
        trigger_capture=trigger_capture,
        prepare=prepare,
    )


def _corpus_quiet_seconds(environment) -> float:
    """How long the link must be idle before the corpus may use it."""
    try:
        return bounded_quiet_seconds(
            environment.get("GATE_CORPUS_QUIET_SECONDS", DEFAULT_QUIET_SECONDS)
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"GATE_CORPUS_QUIET_SECONDS is invalid: {error}") from error


def _corpus_upload_worker(environment, corpus, client, controller_id, activity):
    """The background worker that moves the corpus into R2, or None.

    Built only when there is a corpus to ship and a cloud to ship it to. It
    is deliberately the last worker in the list: nothing else waits on it,
    and its failures are its own.
    """
    if corpus is None or client is None:
        return None
    config = load_corpus_upload_config(environment)
    if not config.enabled:
        return None
    return CorpusUploadWorker(
        corpus, CloudflareCorpusSender(client, controller_id), activity,
        config=config, controller_id=controller_id,
    )


def _training_corpus(environment):
    """A bounded on-device corpus of OCR frames and answers, or None when unset."""
    directory = (environment.get("GATE_TRAINING_CORPUS_DIR") or "").strip()
    if not directory:
        return None
    path = Path(directory)
    if not path.is_absolute():
        raise ValueError("GATE_TRAINING_CORPUS_DIR must be an absolute path")
    raw = str(environment.get("GATE_TRAINING_CORPUS_MAX_BYTES", "")).strip()
    from .corpus import DEFAULT_MAX_BYTES, TrainingCorpus
    try:
        max_bytes = int(raw) if raw else DEFAULT_MAX_BYTES
    except ValueError as error:
        raise ValueError("GATE_TRAINING_CORPUS_MAX_BYTES must be an integer") from error
    return TrainingCorpus(path, max_bytes=max_bytes)



def _clear_stream_source(config):
    """The clear-stream frame source for webhook capture, or None when off.

    "compressed" (default) records packets and decodes only for events;
    "decoded" keeps the older continuously decoding keyframe ring.
    """
    if not (config.enabled and config.hot_keyframes):
        return None
    if config.clear_stream_mode == "decoded":
        return ClearKeyframeBuffer(config)
    from .clear_stream_source import ClearStreamSource
    from .trigger_capture import decoder_filters, decoder_input_arguments
    return ClearStreamSource(
        config.source_url,
        decoder_arguments=decoder_input_arguments(config),
        filters=decoder_filters(config, sample=False),
        max_frame_bytes=config.max_frame_bytes,
        session_fps=config.session_fps,
        session_seconds=config.session_seconds,
        source_fps=config.source_fps,
    )



def _camera_event_handler(trigger_capture, recognizer, audio_capture=None):
    """Warm the OCR connection the instant the camera fires, then capture.

    The prewarm is fire-and-forget and must never delay or break capture, and
    so is the audio request: it only puts a note in a slot and sets an event,
    and its return value is deliberately ignored so nothing about audio can
    change what the frame path does.
    """
    prewarm = getattr(recognizer, "prewarm", None)
    capture = trigger_capture.on_camera_event if trigger_capture is not None else None
    audio = audio_capture.on_camera_event if audio_capture is not None else None
    if capture is None and audio is None and not callable(prewarm):
        return None

    def handle(event):
        if callable(prewarm):
            try:
                prewarm()
            except Exception:
                pass
        if audio is not None:
            try:
                audio(event)
            except Exception:
                pass
        if capture is not None:
            return capture(event)
        return None

    return handle


def _audio_capture_recorder(environment):
    """A bounded recorder of gate audio around each event, or None when off.

    When the switch is off this returns None and the controller runs exactly as
    it did: no thread, no child process, nothing recorded. The configuration is
    still parsed, so a malformed setting is refused at startup rather than
    silently ignored until somebody switches capture on.
    """
    from .audio_capture import AudioClipRecorder, load_audio_capture_config

    corpus_directory = (environment.get("GATE_TRAINING_CORPUS_DIR") or "").strip()
    config = load_audio_capture_config(environment, corpus_directory or None)
    return AudioClipRecorder(config) if config.enabled else None



def build_reolink_trigger_pipeline(environment=None, *, on_accepted=None):
    environment = os.environ if environment is None else environment
    correlator = ReolinkEventCorrelator()
    config = load_reolink_webhook_config(environment)
    workers = (
        (ReolinkWebhookWorker(config, correlator, on_accepted=on_accepted),)
        if config.enabled else ()
    )
    return correlator, workers


def _min_cloud_request_seconds(environment):
    """How much decision budget a cloud lookup must have to be worth billing.

    Unset keeps the shipped floor. An unreadable value is not an error worth
    refusing to start over: the processor validates it again and falls back to
    that same floor, which only ever sends more requests, never fewer.
    """
    raw = str(environment.get("GATE_OCR_MIN_REQUEST_SECONDS", "") or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        logging.getLogger(__name__).warning(
            "gate_ocr key=GATE_OCR_MIN_REQUEST_SECONDS status=rejected"
        )
        return None


def _cloud_skip_stillness(environment):
    """Above this stillness a frame the device found no plate in skips the cloud.

    Unset keeps the shipped threshold; ``0`` disables the rule. An unreadable
    value is not worth refusing to start over: the processor validates it
    again and keeps the shipped threshold.
    """
    raw = str(environment.get("GATE_OCR_CLOUD_SKIP_MOVING_STILLNESS", "") or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        logging.getLogger(__name__).warning(
            "gate_ocr key=GATE_OCR_CLOUD_SKIP_MOVING_STILLNESS status=rejected"
        )
        return None


def _ocr_upload_width(environment) -> int:
    """0 disables downscaling; otherwise the widest frame uploaded to OCR."""
    raw = environment.get("GATE_OCR_MAX_UPLOAD_WIDTH", "0")
    try:
        width = int(raw)
    except (TypeError, ValueError) as error:
        raise ValueError("GATE_OCR_MAX_UPLOAD_WIDTH must be an integer") from error
    if width == 0:
        return 0
    if not MIN_UPLOAD_WIDTH <= width <= MAX_UPLOAD_WIDTH:
        raise ValueError(
            f"GATE_OCR_MAX_UPLOAD_WIDTH must be 0 or between {MIN_UPLOAD_WIDTH} and {MAX_UPLOAD_WIDTH}"
        )
    return width


def _shutdown_controller(processor, relay, *, relay_timeout: float = 0.5,
                         processor_timeout: float = 1.0) -> bool:
    begin_shutdown = getattr(relay, "begin_shutdown", None)
    relay_latched = False
    if callable(begin_shutdown):
        latch_completed, latch_result = _bounded_shutdown_call(begin_shutdown, relay_timeout)
        relay_latched = latch_completed and latch_result is not False
    processor_completed = False
    relay_completed = False
    relay_safe = False
    if relay_latched:
        processor_completed, _ = _bounded_shutdown_call(processor.close, processor_timeout)
        relay_completed, relay_safe = _bounded_shutdown_call(relay.shutdown, relay_timeout)
    return relay_latched and processor_completed and relay_completed and relay_safe is True


def _shutdown_controller_with_hot_stream(hot_stream, processor, relay,
                                         trigger_capture=None, clear_keyframes=None,
                                         net_probe=None, audio_capture=None) -> bool:
    try:
        if audio_capture is not None:
            audio_capture.close()
    except BaseException:
        logging.getLogger(__name__).warning("audio_capture_close_failed", exc_info=True)
    try:
        if net_probe is not None:
            net_probe.close()
    except BaseException:
        logging.getLogger(__name__).warning("net_probe_close_failed", exc_info=True)
    try:
        if trigger_capture is not None:
            trigger_capture.close()
    except BaseException:
        logging.getLogger(__name__).warning("trigger_capture_close_failed", exc_info=True)
    try:
        if clear_keyframes is not None:
            clear_keyframes.close()
    except BaseException:
        logging.getLogger(__name__).warning("clear_keyframes_close_failed", exc_info=True)
    try:
        if hot_stream is not None:
            hot_stream.close()
    except BaseException:
        logging.getLogger(__name__).warning("hot_stream_close_failed", exc_info=True)
    return _shutdown_controller(processor, relay)


def _bounded_shutdown_call(operation, timeout: float) -> tuple[bool, object | None]:
    result = []

    def invoke():
        try:
            result.append(operation())
        except BaseException:
            result.append(False)

    worker = Thread(target=invoke, name="gate-controller-shutdown", daemon=True)
    worker.start()
    worker.join(timeout)
    return not worker.is_alive(), result[0] if result else None


def _run_telemetry_export(arguments: list[str]) -> None:
    _, database_default = default_runtime_paths(os.environ)
    parser = argparse.ArgumentParser(description="Export local gate telemetry")
    parser.add_argument("--database", type=Path, default=database_default)
    parser.add_argument("--format", choices=("json", "csv"), required=True)
    parser.add_argument("--since", type=_iso8601, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parsed = parser.parse_args(arguments)
    export_telemetry(
        LocalStore(parsed.database),
        format=parsed.format,
        since=parsed.since,
        output=parsed.output,
    )


def _iso8601(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO8601 timestamp") from error
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("must include a timezone")
    return parsed.astimezone(timezone.utc)


def _quiet_window(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "quiet window must be between 0.1 and 2 seconds"
        ) from error
    if not math.isfinite(seconds) or not (
        MIN_QUIET_WINDOW_SECONDS <= seconds <= MAX_QUIET_WINDOW_SECONDS
    ):
        raise argparse.ArgumentTypeError(
            "quiet window must be between 0.1 and 2 seconds"
        )
    return seconds


def build_background_workers(store, relay, *, environment=None, latest_image=None,
                             coordinator=None, authorised=None, camera_directory=None,
                             hot_stream=None, match_policy=None,
                             local_recognizer=None, trigger_capture=None,
                             corpus=None, activity=None, metrics=None):
    environment = os.environ if environment is None else environment
    activity = activity if activity is not None else ActivityGate(
        quiet_seconds=_corpus_quiet_seconds(environment),
        pending_events=lambda: store.pending_outbox_count(),
    )
    latest_image = latest_image if latest_image is not None else {}
    prompt_player = PromptPlayer(_configured_prompts(environment))
    camera_stale_seconds = float(environment.get("GATE_CAMERA_STALE_SECONDS", "60"))
    if camera_stale_seconds <= 0:
        raise ValueError("GATE_CAMERA_STALE_SECONDS must be greater than zero")
    telemetry_retention_days = _telemetry_retention_days(environment)
    net_probe_config = load_net_probe_config(environment)
    net_probe = NetProbeWorker(net_probe_config) if net_probe_config.enabled else None
    workers = []
    controller_id = environment.get("GATE_CONTROLLER_ID") or "primary"
    if coordinator is not None:
        workers.append(CommandServerWorker(DirectCommandExecutor(
            controller_id, coordinator, store, prompt_player=prompt_player,
        )))
    cloudflare_configured = _cloudflare_configured(environment)
    if cloudflare_configured:
        cloudflare_client = CloudflareServiceClient(
            environment["GATE_CLOUDFLARE_API_URL"].strip(),
            environment["GATE_CLOUDFLARE_ACCESS_CLIENT_ID"].strip(),
            environment["GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET"].strip(),
        )
        workers.append(OutboxWorker(
            store,
            CloudflareOutboxSender(cloudflare_client, controller_id),
            controller_id=controller_id,
            telemetry_retention_days=telemetry_retention_days,
        ))
        plates_worker = None
        if authorised is not None:
            plates_worker = AuthorisationRefreshWorker(
                authorised, CloudflarePlateFetcher(cloudflare_client, controller_id),
                poll_interval=float(environment.get("GATE_AUTHORISATION_REFRESH_SECONDS", "30")),
            )
            workers.append(plates_worker)
        if match_policy is not None:
            workers.append(SettingsRefreshWorker(
                match_policy,
                CloudflareSettingsFetcher(cloudflare_client, controller_id),
                poll_interval=float(
                    environment.get("GATE_SETTINGS_REFRESH_SECONDS", "60")
                ),
            ))
        corpus_upload = _corpus_upload_worker(
            environment, corpus, cloudflare_client, controller_id, activity,
        )
        # heartbeat_worker is late-bound on purpose: the status it reports
        # includes the round trip of the POST the worker itself makes.
        heartbeat_worker = None
        status = lambda: _controller_status(
            store, prompt_player, latest_image, authorised, relay=relay,
            camera_directory=camera_directory,
            camera_stale_seconds=camera_stale_seconds,
            hot_stream=hot_stream, match_policy=match_policy,
            local_recognizer=local_recognizer,
            trigger_capture=trigger_capture,
            net_probe=net_probe, heartbeat=heartbeat_worker, plates=plates_worker,
            corpus=corpus, corpus_upload=corpus_upload, activity=activity,
            metrics=metrics,
        )
        heartbeat_worker = HeartbeatWorker(
            CloudflareStatusReporter(cloudflare_client, controller_id), status,
            metrics=metrics,
        )
        workers.append(heartbeat_worker)
        # Only when a ring was handed in: the ring is fed by the burst
        # pipeline in `main`, and a rollup worker posting an empty ring
        # nobody feeds would be a POST that says nothing.
        if metrics is not None:
            workers.append(MetricsRollupWorker(
                metrics,
                CloudflareMetricsReporter(cloudflare_client, controller_id).send,
                controller_id=controller_id,
                poll_interval=metrics_rollup_seconds(environment),
                activity=activity,
            ))
        if net_probe is not None:
            workers.append(net_probe)
        if corpus_upload is not None:
            workers.append(corpus_upload)
        return tuple(workers), prompt_player, status
    outbox_url = (environment.get("GATE_OUTBOX_URL") or "").strip()
    if outbox_url:
        bearer_token = _validated_outbox_token(
            outbox_url, environment.get("GATE_OUTBOX_BEARER_TOKEN")
        )
        workers.append(OutboxWorker(
            store,
            HttpOutboxSender(
                outbox_url, bearer_token=bearer_token, controller_id=controller_id,
            ),
            controller_id=controller_id,
            telemetry_retention_days=telemetry_retention_days,
        ))
    else:
        workers.append(TelemetryRetentionWorker(
            store, retention_days=telemetry_retention_days,
        ))
    if net_probe is not None:
        workers.append(net_probe)
    return tuple(workers), prompt_player, lambda: _controller_status(
        store, prompt_player, latest_image, relay=relay,
        camera_directory=camera_directory,
        camera_stale_seconds=camera_stale_seconds,
        hot_stream=hot_stream, local_recognizer=local_recognizer,
        trigger_capture=trigger_capture, net_probe=net_probe,
        corpus=corpus, activity=activity,
    )


def _telemetry_retention_days(environment) -> int:
    configured = environment.get("GATE_TELEMETRY_RETENTION_DAYS", "30")
    try:
        days = int(configured)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "GATE_TELEMETRY_RETENTION_DAYS must be an integer between 1 and 3650"
        ) from error
    if not 1 <= days <= 3650:
        raise ValueError(
            "GATE_TELEMETRY_RETENTION_DAYS must be an integer between 1 and 3650"
        )
    return days


def _configured_prompts(environment) -> dict[str, Path]:
    prompt_environment = {
        "arrival": "GATE_PROMPT_ARRIVAL",
        "access_denied": "GATE_PROMPT_ACCESS_DENIED",
    }
    return {
        key: Path(environment[value])
        for key, value in prompt_environment.items()
        if environment.get(value)
    }


def image_runtime_limits(environment) -> tuple[int, int]:
    try:
        max_candidates = int(environment.get(
            "GATE_MAX_BURST_CANDIDATES", str(DEFAULT_MAX_BURST_CANDIDATES)
        ))
        max_bytes = int(environment.get(
            "GATE_MAX_CANDIDATE_IMAGE_BYTES", str(DEFAULT_MAX_CANDIDATE_BYTES)
        ))
    except (TypeError, ValueError) as error:
        raise ValueError("image runtime limits must be integers") from error
    if max_candidates <= 0 or max_bytes <= 0:
        raise ValueError("image runtime limits must be greater than zero")
    if max_candidates > MAX_BURST_CANDIDATES or max_bytes > MAX_CANDIDATE_BYTES:
        raise ValueError("image runtime limits exceed the safe maximum")
    return max_candidates, max_bytes


def _controller_status(store, prompt_player, latest_image, authorised=None, *, relay=None,
                       camera_directory=None, camera_stale_seconds: float = 60.0,
                       hot_stream=None, match_policy=None, local_recognizer=None,
                       trigger_capture=None, net_probe=None,
                       heartbeat=None, plates=None,
                       corpus=None, corpus_upload=None, activity=None, metrics=None,
                       media_capabilities_path=Path("/run/gate-media/capabilities.json"),
                       camera_control_state_path=CAMERA_CONTROL_STATE_PATH,
                       module_path=Path(__file__),
                       managed_releases_root=MANAGED_RELEASES_ROOT,
                       host_metrics=read_host_metrics, clock=None) -> dict:
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    camera_upload_recent = _camera_is_fresh(
        latest_image.get("received_at"), now, camera_stale_seconds
    )
    camera_configured = camera_directory is not None
    camera_upload_ready = camera_configured and Path(camera_directory).is_dir()
    status = {
        "last_seen_at": now.isoformat(),
        # The absolute path of the latest frame is deliberately not sent: a
        # filesystem path has no business in D1. A boolean and an age carry
        # everything the app needs.
        "latest_camera_image_available": bool(latest_image.get("path")),
        "latest_camera_image_age_seconds": _age_seconds(
            latest_image.get("received_at"), now
        ),
        "last_camera_upload_at": latest_image.get("received_at"),
        "queue_depth": store.pending_outbox_count(),
        "audio_available": prompt_player.available,
        "camera_configured": camera_configured,
        "camera_upload_ready": camera_upload_ready,
        "camera_upload_recent": camera_upload_recent,
        "camera_connection_probed": False,
        "camera_connected": None,
        "relay": _relay_status(relay),
        "media": read_media_capabilities(media_capabilities_path),
        "camera_control": read_camera_control_state(camera_control_state_path),
        "recognition": {
            "hot_stream": _hot_stream_status(hot_stream),
            "local_shadow": _local_recognizer_status(local_recognizer),
        },
    }
    trigger_capture_status = _trigger_capture_status(trigger_capture)
    if trigger_capture_status is not None:
        status["recognition"]["trigger_capture"] = trigger_capture_status
    host = _host_status(host_metrics, net_probe)
    if host:
        status["host"] = host
    network = _network_status(net_probe)
    if network is not None:
        status["network"] = network
    status["cloud"] = _cloud_status(store, heartbeat, plates, now, metrics=metrics)
    corpus_status = _corpus_status(corpus, corpus_upload, activity)
    if corpus_status is not None:
        status["corpus"] = corpus_status
    release_sha = _managed_release_sha(
        module_path, releases_root=managed_releases_root
    )
    if release_sha is not None:
        status["software"] = {"release_sha": release_sha}
    if authorised is not None:
        status["authorisation"] = authorised.status()
    if match_policy is not None:
        status["match_policy"] = match_policy.status()
    return status


def _corpus_status(corpus, corpus_upload, activity) -> dict | None:
    """How far behind the corpus is, and what is holding it up.

    `pending` climbing with `last_success_at` standing still is the shape of a
    buffer that is filling because uploads are failing -- the one thing that
    turns a bounded local cache back into a single point of loss.
    """
    if corpus is None:
        return None
    measured: dict = {"local": _bounded_status(corpus)}
    if corpus_upload is not None:
        measured["upload"] = _bounded_status(corpus_upload)
    if activity is not None:
        measured["backpressure"] = _bounded_status(activity)
    return measured


def _bounded_status(source) -> dict:
    read_status = getattr(source, "status", None)
    if not callable(read_status):
        return {}
    try:
        measured = read_status()
    except Exception:
        return {}
    return measured if isinstance(measured, dict) else {}


def _local_recognizer_status(local_recognizer) -> dict:
    """The on-device recogniser's counters, or the disabled placeholder."""
    default = {"mode": "disabled", "ready": False}
    if local_recognizer is None:
        return default
    try:
        measured = local_recognizer.status()
    except Exception:
        return default
    if not isinstance(measured, dict):
        return default
    measured["ready"] = measured.get("state") == "ready"
    return measured


def _age_seconds(timestamp: str | None, now: datetime) -> float | None:
    if not timestamp:
        return None
    try:
        observed_at = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    age = (now.astimezone(timezone.utc) - observed_at.astimezone(timezone.utc))
    return round(max(0.0, age.total_seconds()), 1)


def _trigger_capture_status(trigger_capture) -> dict | None:
    """The presence and skip counters that already exist on the Pi.

    Eleven `gate_presence stage=unresolved` warnings in one week, each a
    vehicle at a gate that stayed shut, were being counted and discarded.
    """
    read_status = getattr(trigger_capture, "status", None)
    if not callable(read_status):
        return None
    try:
        measured = read_status()
    except Exception:
        return None
    return measured if isinstance(measured, dict) else None


def _host_status(host_metrics, net_probe) -> dict:
    """Bounded /proc and /sys reads; an absent field means the read failed."""
    throttled = None
    read_throttled = getattr(net_probe, "throttled_flags", None)
    if callable(read_throttled):
        try:
            throttled = read_throttled()
        except Exception:
            throttled = None
    try:
        measured = host_metrics(throttled=throttled)
    except Exception:
        return {}
    return measured if isinstance(measured, dict) else {}


def _network_status(net_probe) -> dict | None:
    read_status = getattr(net_probe, "status", None)
    if not callable(read_status):
        return None
    try:
        measured = read_status()
    except Exception:
        return None
    return measured if isinstance(measured, dict) else None


def _cloud_status(store, heartbeat, plates, now: datetime, *, metrics=None) -> dict:
    cloud: dict = {
        "heartbeat_rtt_ms": None,
        "heartbeat_consecutive_failures": None,
        "plates_consecutive_failures": None,
        "oldest_pending_outbox_age_s": None,
    }
    read_metrics = getattr(heartbeat, "metrics", None)
    if callable(read_metrics):
        try:
            measured = read_metrics()
        except Exception:
            measured = None
        if isinstance(measured, dict):
            cloud["heartbeat_rtt_ms"] = measured.get("heartbeat_rtt_ms")
            cloud["heartbeat_consecutive_failures"] = measured.get(
                "heartbeat_consecutive_failures"
            )
    failures = getattr(plates, "consecutive_failures", None)
    if isinstance(failures, int):
        cloud["plates_consecutive_failures"] = failures
    try:
        cloud["oldest_pending_outbox_age_s"] = store.oldest_pending_outbox_age_seconds(now=now)
    except Exception:
        cloud["oldest_pending_outbox_age_s"] = None
    # The quota pair rides the 15 s heartbeat as well as the five-minute
    # rollup: the heartbeat's `cloud` block already allow-lists both keys, so
    # the burn-down is live rather than up to five minutes stale. Nothing new
    # is invented here -- an unknown heartbeat key is dropped silently, which
    # is exactly the failure this phase exists to end.
    quota = getattr(metrics, "quota_status", None)
    if callable(quota):
        try:
            measured = quota()
        except Exception:
            measured = None
        if isinstance(measured, dict):
            for key, value in measured.items():
                if isinstance(value, int) and not isinstance(value, bool):
                    cloud[key] = value
    return cloud


def _hot_stream_status(hot_stream) -> dict:
    default = {
        "enabled": False,
        "ready": False,
        "stream": "fluent",
        "sample_fps": 5.0,
        "source_profile": {
            "codec": "h264", "width": 640, "height": 360, "fps": 10,
        },
        "latest_frame_age_ms": None,
        "buffered_frames": 0,
        "restart_count": 0,
    }
    if hot_stream is None:
        return default
    try:
        measured = hot_stream.status()
    except Exception:
        return default
    if not isinstance(measured, dict):
        return default
    return {key: measured.get(key, value) for key, value in default.items()}


def _managed_release_sha(
    module_path: Path, *, releases_root=MANAGED_RELEASES_ROOT
) -> str | None:
    try:
        resolved_module = Path(module_path).resolve(strict=True)
        resolved_releases = Path(releases_root).resolve(strict=True)
        relative_module = resolved_module.relative_to(resolved_releases)
    except (OSError, RuntimeError, ValueError):
        return None
    if len(relative_module.parts) < 2:
        return None
    release_sha = relative_module.parts[0]
    release = resolved_releases / release_sha
    if (MANAGED_RELEASE_SHA_PATTERN.fullmatch(release_sha) is None
            or release.is_symlink() or not release.is_dir()):
        return None
    return release_sha


def _relay_status(relay) -> dict:
    read_status = getattr(relay, "status", None)
    if not callable(read_status):
        return {"ready": None, "last_outcome": None, "last_outcome_at": None}
    try:
        measured = read_status()
    except Exception:
        return {"ready": None, "last_outcome": None, "last_outcome_at": None}
    return {
        "ready": measured.get("ready"),
        "last_outcome": measured.get("last_outcome"),
        "last_outcome_at": measured.get("last_outcome_at"),
    }


def _camera_is_fresh(timestamp: str | None, now: datetime, stale_seconds: float) -> bool:
    if not timestamp:
        return False
    try:
        observed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return False
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    age = now.astimezone(timezone.utc) - observed_at.astimezone(timezone.utc)
    return timedelta(0) <= age <= timedelta(seconds=stale_seconds)


def _cloudflare_configured(environment) -> bool:
    legacy_variables = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")
    if any(bool((environment.get(variable) or "").strip()) for variable in legacy_variables):
        raise ValueError(
            "legacy Supabase credentials must not be present in the active controller environment"
        )
    variables = (
        "GATE_CLOUDFLARE_API_URL",
        "GATE_CLOUDFLARE_ACCESS_CLIENT_ID",
        "GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET",
    )
    configured = [bool((environment.get(variable) or "").strip()) for variable in variables]
    if any(configured) and not all(configured):
        raise ValueError("GATE_CLOUDFLARE_API_URL, GATE_CLOUDFLARE_ACCESS_CLIENT_ID, and GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET must be configured together")
    return all(configured)


DEFAULT_AUTHORISATION_MAX_STALENESS_SECONDS = 14 * 24 * 60 * 60


def authorisation_max_staleness(environment) -> timedelta | None:
    """How old the cloud plate snapshot may grow before recognition fails closed.

    Plate lists change rarely, so the default keeps the last good snapshot in
    use for two weeks of cloud outage. A value of zero or less disables the
    bound entirely; recognition then keeps the last snapshot indefinitely.
    """
    raw = str(environment.get("GATE_AUTHORISATION_MAX_STALENESS_SECONDS", "")).strip()
    seconds = float(raw) if raw else float(DEFAULT_AUTHORISATION_MAX_STALENESS_SECONDS)
    if seconds != seconds or seconds in (float("inf"), float("-inf")):
        raise ValueError("GATE_AUTHORISATION_MAX_STALENESS_SECONDS must be finite")
    if seconds <= 0:
        return None
    return timedelta(seconds=seconds)


def _cloud_configured(environment) -> bool:
    return _cloudflare_configured(environment)


def _validated_outbox_token(url: str, token: str | None) -> str:
    token = (token or "").strip()
    if not token:
        raise ValueError("GATE_OUTBOX_BEARER_TOKEN is required when GATE_OUTBOX_URL is set")
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError("GATE_OUTBOX_URL must be an absolute HTTPS URL")
    if parsed.scheme == "https":
        return token
    if parsed.scheme == "http" and _is_loopback_host(parsed.hostname):
        return token
    raise ValueError("GATE_OUTBOX_URL must use HTTPS except for explicit loopback URLs")


def _is_loopback_host(hostname: str) -> bool:
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def default_runtime_paths(environment) -> tuple[Path, Path]:
    state_directory = Path("/var/lib/gate-controller")
    return (
        Path(environment.get(
            "GATE_AUTHORISED_PLATES", state_directory / "authorised_licence_plates.csv"
        )),
        Path(environment.get("GATE_DATABASE", state_directory / "gate-controller.db")),
    )


if __name__ == "__main__":
    main()
