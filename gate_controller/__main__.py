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
from .authorisation import (
    AuthorisationRefreshWorker, AuthorisedPlateCache, CloudflarePlateFetcher,
)
from .cloudflare_client import CloudflareServiceClient, CloudflareStatusReporter
from .command_server import CommandServerWorker, DirectCommandExecutor
from .control_plane import HeartbeatWorker
from .hot_stream import HotStreamBuffer, load_hot_stream_config
from .ocr import MAX_UPLOAD_WIDTH, MIN_UPLOAD_WIDTH
from .plate_region import parse_plate_region
from .trigger_capture import (
    ClearKeyframeBuffer, TriggerFrameCapture, load_trigger_capture_config,
)
from .media_capabilities import read_media_capabilities
from .ocr import PlateRecognizerClient
from .outbox import (
    CloudflareOutboxSender, HttpOutboxSender, OutboxWorker,
    TelemetryRetentionWorker,
)
from .processor import GateProcessor
from .relay import PiRelayAdapter, RelayController
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

    relay = RelayController(PiRelayAdapter())
    store = LocalStore(arguments.database)
    store.recover_interrupted_actuations()
    coordinator = ActuationCoordinator(store, relay, timedelta(seconds=20))
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
    background_workers, _, _ = build_background_workers(
        store, relay, latest_image=latest_image, coordinator=coordinator,
        authorised=authorised, camera_directory=arguments.directory,
        hot_stream=hot_stream,
    )
    trigger_capture_config = load_trigger_capture_config(
        os.environ, Path(arguments.database).resolve().parent,
        webhook_enabled=load_reolink_webhook_config(os.environ).enabled,
    )
    clear_keyframes = (
        ClearKeyframeBuffer(trigger_capture_config)
        if trigger_capture_config.enabled and trigger_capture_config.hot_keyframes
        else None
    )
    trigger_capture = (
        TriggerFrameCapture(trigger_capture_config, frame_source=clear_keyframes)
        if trigger_capture_config.enabled else None
    )
    recognizer = PlateRecognizerClient(
        token, max_upload_width=_ocr_upload_width(os.environ),
        plate_region=parse_plate_region(os.environ.get("GATE_PLATE_REGION")),
        # Frames the keyframe decoder already cropped must not be cropped again.
        precropped_directory=(
            trigger_capture_config.output_directory
            if trigger_capture_config.plate_region is not None else None
        ),
    )
    trigger_correlator, trigger_workers = build_reolink_trigger_pipeline(
        os.environ,
        on_accepted=_camera_event_handler(trigger_capture, recognizer),
    )
    background_workers = tuple(background_workers) + tuple(trigger_workers)
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
    )

    def process(paths, received_at=None, decision_started_at=None,
                processing_started_at=None, *, trigger=None,
                idempotency_key=None):
        latest_image["path"] = str(paths[0]) if paths else None
        latest_image["received_at"] = (received_at or datetime.now(timezone.utc)).isoformat()
        return processor.process(
            paths,
            received_at=received_at,
            decision_started_at=decision_started_at,
            processing_started_at=processing_started_at,
            trigger=trigger,
            idempotency_key=idempotency_key,
        )

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

    def shutdown():
        return _shutdown_controller_with_hot_stream(
            hot_stream, processor, relay, trigger_capture=trigger_capture,
            clear_keyframes=clear_keyframes,
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
    )


def _camera_event_handler(trigger_capture, recognizer):
    """Warm the OCR connection the instant the camera fires, then capture.

    The prewarm is fire-and-forget and must never delay or break capture.
    """
    prewarm = getattr(recognizer, "prewarm", None)
    capture = trigger_capture.on_camera_event if trigger_capture is not None else None
    if capture is None and not callable(prewarm):
        return None

    def handle(event):
        if callable(prewarm):
            try:
                prewarm()
            except Exception:
                pass
        if capture is not None:
            return capture(event)
        return None

    return handle



def build_reolink_trigger_pipeline(environment=None, *, on_accepted=None):
    environment = os.environ if environment is None else environment
    correlator = ReolinkEventCorrelator()
    config = load_reolink_webhook_config(environment)
    workers = (
        (ReolinkWebhookWorker(config, correlator, on_accepted=on_accepted),)
        if config.enabled else ()
    )
    return correlator, workers


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
                                         trigger_capture=None, clear_keyframes=None) -> bool:
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
                             hot_stream=None):
    environment = os.environ if environment is None else environment
    latest_image = latest_image if latest_image is not None else {}
    prompt_player = PromptPlayer(_configured_prompts(environment))
    camera_stale_seconds = float(environment.get("GATE_CAMERA_STALE_SECONDS", "60"))
    if camera_stale_seconds <= 0:
        raise ValueError("GATE_CAMERA_STALE_SECONDS must be greater than zero")
    telemetry_retention_days = _telemetry_retention_days(environment)
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
        if authorised is not None:
            workers.append(AuthorisationRefreshWorker(
                authorised, CloudflarePlateFetcher(cloudflare_client, controller_id),
                poll_interval=float(environment.get("GATE_AUTHORISATION_REFRESH_SECONDS", "30")),
            ))
        status = lambda: _controller_status(
            store, prompt_player, latest_image, authorised, relay=relay,
            camera_directory=camera_directory,
            camera_stale_seconds=camera_stale_seconds,
            hot_stream=hot_stream,
        )
        workers.append(HeartbeatWorker(
            CloudflareStatusReporter(cloudflare_client, controller_id), status,
        ))
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
    return tuple(workers), prompt_player, lambda: _controller_status(
        store, prompt_player, latest_image, relay=relay,
        camera_directory=camera_directory,
        camera_stale_seconds=camera_stale_seconds,
        hot_stream=hot_stream,
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
                       hot_stream=None,
                       media_capabilities_path=Path("/run/gate-media/capabilities.json"),
                       module_path=Path(__file__),
                       managed_releases_root=MANAGED_RELEASES_ROOT, clock=None) -> dict:
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    camera_upload_recent = _camera_is_fresh(
        latest_image.get("received_at"), now, camera_stale_seconds
    )
    camera_configured = camera_directory is not None
    camera_upload_ready = camera_configured and Path(camera_directory).is_dir()
    status = {
        "last_seen_at": now.isoformat(),
        "latest_camera_image": latest_image.get("path"),
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
        "recognition": {
            "hot_stream": _hot_stream_status(hot_stream),
            "local_shadow": {"mode": "disabled", "ready": False},
        },
    }
    release_sha = _managed_release_sha(
        module_path, releases_root=managed_releases_root
    )
    if release_sha is not None:
        status["software"] = {"release_sha": release_sha}
    if authorised is not None:
        status["authorisation"] = authorised.status()
    return status


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
