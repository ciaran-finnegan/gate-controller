import argparse
import ipaddress
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from .audio import PromptPlayer
from .actuation import ActuationCoordinator
from .authorisation import (
    AuthorisationRefreshWorker, AuthorisedPlateCache, SupabasePlateFetcher,
)
from .control_plane import CommandWorker, HeartbeatWorker, SupabaseControlPlane
from .ocr import PlateRecognizerClient
from .outbox import HttpOutboxSender, OutboxWorker
from .processor import GateProcessor
from .relay import PiRelayAdapter, RelayController
from .store import LocalStore
from .worker import (
    DEFAULT_MAX_BURST_CANDIDATES, DEFAULT_MAX_CANDIDATE_BYTES,
    MAX_BURST_CANDIDATES, MAX_CANDIDATE_BYTES, run_worker,
)
from .runtime import require_python_version


def main() -> None:
    require_python_version()
    logging.basicConfig(
        level=os.environ.get("GATE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="Watch completed gate-camera uploads")
    authorised_default, database_default = default_runtime_paths(os.environ)
    parser.add_argument("--directory", type=Path,
                        default=Path(os.environ.get("GATE_WATCH_DIRECTORY", "/home/ftp-user")))
    parser.add_argument("--authorised-plates", type=Path,
                        default=authorised_default)
    parser.add_argument("--database", type=Path,
                        default=database_default)
    parser.add_argument("--quiet-window", type=float, default=0.5)
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
    authorisation_staleness = timedelta(
        seconds=float(os.environ.get("GATE_AUTHORISATION_MAX_STALENESS_SECONDS", "300"))
    )
    authorised = AuthorisedPlateCache(
        arguments.authorised_plates,
        max_staleness=authorisation_staleness if _supabase_configured(os.environ) else None,
    )
    latest_image = {"path": None, "received_at": None}
    background_workers, _, _ = build_background_workers(
        store, relay, latest_image=latest_image, coordinator=coordinator,
        authorised=authorised, camera_directory=arguments.directory,
    )
    outbox = next((worker for worker in background_workers if isinstance(worker, OutboxWorker)), None)
    processor = GateProcessor(
        recognizer=PlateRecognizerClient(token),
        store=store,
        relay=relay,
        authorised=authorised.get,
        cooldown=timedelta(seconds=20),
        outbox=outbox,
        coordinator=coordinator,
        max_image_age=timedelta(seconds=max_image_age),
        decision_timeout=decision_timeout,
    )

    def process(paths, received_at=None, decision_started_at=None):
        latest_image["path"] = str(paths[0]) if paths else None
        latest_image["received_at"] = (received_at or datetime.now(timezone.utc)).isoformat()
        return processor.process(
            paths, received_at=received_at, decision_started_at=decision_started_at
        )

    def record_skipped(paths, reason, received_at):
        logging.getLogger(__name__).warning("image_burst_skipped reason=%s count=%d", reason,
                                            len(paths))
        return processor.record_skipped(paths, reason, received_at)

    def record_error(paths, error, received_at):
        logging.getLogger(__name__).exception(
            "image_burst_failed count=%d error=%s", len(paths), error,
            exc_info=(type(error), error, error.__traceback__),
        )
        try:
            processor.record_skipped(paths, "processing_error", received_at)
        except Exception:
            logging.getLogger(__name__).exception("processing_error_event_failed")

    run_worker(
        arguments.directory, process, quiet_window=arguments.quiet_window,
        background_workers=background_workers,
        max_image_age=max_image_age,
        on_skipped=record_skipped,
        on_error=record_error,
        shutdown=relay.shutdown,
        max_burst_candidates=max_burst_candidates,
        max_candidate_bytes=max_candidate_bytes,
    )


def build_background_workers(store, relay, *, environment=None, latest_image=None,
                             coordinator=None, authorised=None, camera_directory=None):
    environment = os.environ if environment is None else environment
    latest_image = latest_image if latest_image is not None else {}
    prompt_player = PromptPlayer(_configured_prompts(environment))
    camera_stale_seconds = float(environment.get("GATE_CAMERA_STALE_SECONDS", "60"))
    if camera_stale_seconds <= 0:
        raise ValueError("GATE_CAMERA_STALE_SECONDS must be greater than zero")
    workers = []
    controller_id = environment.get("GATE_CONTROLLER_ID") or "primary"
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
        ))
    if not _supabase_configured(environment):
        return tuple(workers), prompt_player, lambda: _controller_status(
            store, prompt_player, latest_image, relay=relay,
            camera_directory=camera_directory,
            camera_stale_seconds=camera_stale_seconds,
        )
    supabase_url = environment["SUPABASE_URL"].strip()
    service_key = environment["SUPABASE_SERVICE_ROLE_KEY"].strip()
    control_plane = SupabaseControlPlane(supabase_url, service_key, controller_id)
    if authorised is not None:
        workers.append(AuthorisationRefreshWorker(
            authorised, SupabasePlateFetcher(supabase_url, service_key),
            poll_interval=float(environment.get("GATE_AUTHORISATION_REFRESH_SECONDS", "30")),
        ))
    status = lambda: _controller_status(
        store, prompt_player, latest_image, authorised, relay=relay,
        camera_directory=camera_directory,
        camera_stale_seconds=camera_stale_seconds,
    )
    workers.extend((
        CommandWorker(control_plane, relay, store, prompt_player=prompt_player, coordinator=coordinator),
        HeartbeatWorker(control_plane, status),
    ))
    return tuple(workers), prompt_player, status


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
                       clock=None) -> dict:
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
    }
    if authorised is not None:
        status["authorisation"] = authorised.status()
    return status


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


def _supabase_configured(environment) -> bool:
    url = (environment.get("SUPABASE_URL") or "").strip()
    service_key = (environment.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if bool(url) != bool(service_key):
        raise ValueError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be configured together"
        )
    return bool(url)


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
