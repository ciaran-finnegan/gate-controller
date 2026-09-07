"""Cloud-managed controller settings.

Today the only managed setting is the plate matching schedule, but the channel
is deliberately an envelope rather than a bare policy document so a later
setting can join it without a second endpoint.

The controller reads ``GET /api/controller/settings?controller_id=...`` and
expects::

    {
      "controller_id": "primary",
      "settings_version": 1,
      "updated_at": "2026-09-07T09:00:00Z",
      "plate_matching": {
        "schema_version": 1,
        "timezone": "Europe/Dublin",
        "bands": [
          {"start": "08:00", "end": "22:00", "level": "standard"},
          {"start": "22:00", "end": "08:00", "level": "strict"}
        ]
      }
    }

Like the authorised-plate snapshot, a good document is cached on disk so a
restart without the cloud keeps the schedule the owner configured. Unlike that
snapshot, a *missing* document is not an error: it means no schedule has been
configured and the controller keeps its shipped behaviour.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock
from urllib.parse import urlencode

from .cloud_health import TransitionLogger
from .match_policy import (
    DEFAULT_POLICY,
    STRICT_POLICY,
    MatchPolicy,
    MatchPolicyError,
    parse_policy,
)

LOGGER = logging.getLogger(__name__)

MAX_SETTINGS_BYTES = 16 * 1024
SETTINGS_VERSION = 1


class SettingsError(RuntimeError):
    pass


class MatchPolicyCache:
    """Hold the active matching policy, surviving restarts and cloud outages."""

    def __init__(self, path: Path | None = None, *, clock=None):
        self._path = Path(path) if path is not None else None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = Lock()
        self._policy = DEFAULT_POLICY
        self._configured = False
        self._refreshed_at: datetime | None = None
        self._last_error: str | None = None
        self._load_cached()

    def get(self) -> MatchPolicy:
        with self._lock:
            return self._policy

    def replace(self, document: object) -> None:
        """Adopt a settings envelope, persisting it for the next restart.

        A document the controller cannot read is not silently ignored: the
        policy fails closed to exact matches until a readable one arrives.
        """
        try:
            policy = policy_from_settings(document)
        except MatchPolicyError as error:
            with self._lock:
                self._policy = STRICT_POLICY
                self._configured = True
                self._last_error = str(error)
            LOGGER.warning(
                "controller settings rejected (%s); matching is exact-only until "
                "a valid schedule arrives", error,
            )
            return
        self._persist(document)
        with self._lock:
            self._policy = policy
            self._configured = True
            self._refreshed_at = self._clock()
            self._last_error = None

    def mark_refresh_error(self, error: Exception) -> None:
        with self._lock:
            self._last_error = str(error)

    def status(self) -> dict:
        with self._lock:
            return {
                "configured": self._configured,
                "level_bands": [band.to_wire() for band in self._policy.bands],
                "timezone": self._policy.timezone_name,
                "refreshed_at": (
                    self._refreshed_at.isoformat() if self._refreshed_at else None
                ),
                "last_error": self._last_error,
            }

    def _load_cached(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            if self._path.stat().st_size > MAX_SETTINGS_BYTES:
                raise ValueError("cached controller settings exceed the size limit")
            document = json.loads(self._path.read_text(encoding="utf-8"))
            policy = policy_from_settings(document)
        except (OSError, ValueError, MatchPolicyError) as error:
            LOGGER.warning("cached controller settings are unusable: %s", error)
            return
        self._policy = policy
        self._configured = True
        self._refreshed_at = datetime.fromtimestamp(
            self._path.stat().st_mtime, timezone.utc
        )

    def _persist(self, document: object) -> None:
        if self._path is None:
            return
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_SETTINGS_BYTES:
            LOGGER.warning("controller settings too large to cache locally")
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                dir=self._path.parent, prefix=f".{self._path.name}.", text=True
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                    target.write(encoded)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, self._path)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        except OSError as error:
            LOGGER.warning("could not cache controller settings: %s", error)


def policy_from_settings(document: object) -> MatchPolicy:
    """Extract the matching policy from a settings envelope."""
    if not isinstance(document, dict):
        raise MatchPolicyError("controller settings must be an object")
    version = document.get("settings_version", SETTINGS_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise MatchPolicyError("controller settings_version must be an integer")
    if version > SETTINGS_VERSION:
        raise MatchPolicyError(
            f"controller settings_version {version} is newer than {SETTINGS_VERSION}"
        )
    plate_matching = document.get("plate_matching")
    if plate_matching is None:
        # No schedule configured: keep the behaviour the controller ships with.
        return DEFAULT_POLICY
    return parse_policy(plate_matching)


class CloudflareSettingsFetcher:
    def __init__(self, client, controller_id):
        self.client = client
        self._controller_id = controller_id

    def __call__(self) -> dict:
        payload = self.client.get_json(
            "/api/controller/settings?"
            + urlencode({"controller_id": self._controller_id}),
            max_response_bytes=MAX_SETTINGS_BYTES,
        )
        if (not isinstance(payload, dict)
                or payload.get("controller_id") != self._controller_id):
            raise SettingsError(
                "Cloudflare settings returned a document for another controller"
            )
        return payload


class SettingsRefreshWorker:
    def __init__(self, cache: MatchPolicyCache, fetch, poll_interval: float = 60.0, *,
                 health: TransitionLogger | None = None):
        self._cache = cache
        self._fetch = fetch
        self._poll_interval = poll_interval
        self._health = health or TransitionLogger(LOGGER, "settings_refresh")

    def run_once(self) -> bool:
        try:
            document = self._fetch()
            if not isinstance(document, dict):
                raise SettingsError("controller settings refresh was malformed")
            self._cache.replace(document)
        except Exception as error:
            self._cache.mark_refresh_error(error)
            self._health.failure(error)
            return False
        self._health.success()
        return True

    def run_forever(self, stop_event: Event) -> None:
        while not stop_event.is_set():
            self.run_once()
            stop_event.wait(self._poll_interval)
