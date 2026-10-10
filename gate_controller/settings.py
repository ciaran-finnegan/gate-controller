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

The envelope may also carry ``"gate_left_open": {"enabled": true,
"threshold_minutes": 10}``, the owner's switch for the notify-only left-open
alert (docs/gate-left-open.md), and ``"automatic_open": {"enabled": false}``,
the owner's pause switch against automatic opening (automatic_open.py,
docs/invariants.md 12). Each is kept raw and parsed only when read; a
malformed one is ignored and can never change or reject the schedule.

Like the authorised-plate snapshot, a good document is cached on disk so a
restart without the cloud keeps the schedule the owner configured. Unlike that
snapshot, a *missing* document is not an error: it means no schedule has been
configured and the controller keeps its shipped behaviour.

A *rejected* document leaves a marker file beside the cache. Without it, a
restart would quietly restore the last good schedule while the cloud is still
serving the document that was refused, undoing the fail-closed state the
rejection created. The marker survives the restart, the policy stays
exact-only, and the first readable document clears both.
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

from .automatic_open import is_valid_section as _valid_automatic_open
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
#: The envelope's side sections and the file each is cached in beside the
#: schedule's cache. Each is adopted independently of the schedule and of the
#: others, and parsed only by its own module when read.
SIDE_SECTIONS = {
    "gate_left_open": "gate-left-open.json",
    "automatic_open": "automatic-open.json",
}
#: A section with a validator is adopted only when it is well formed: one
#: that is present and malformed keeps the previous value rather than
#: falling back, because for a switch that can only withhold pulses a typo
#: must not be the thing that resumes them. An absent section still means
#: "no setting" and falls back to the board's environment.
SECTION_VALIDATORS = {
    "automatic_open": _valid_automatic_open,
}
#: The heartbeat carries the rejection reason so the owner can see *why* the
#: gate fell closed. It is bounded because the heartbeat payload is.
MAX_ERROR_LENGTH = 200


def _bounded_error(error: str | None) -> str | None:
    if error is None:
        return None
    collapsed = " ".join(str(error).split())
    if not collapsed:
        return None
    return collapsed[:MAX_ERROR_LENGTH]


class SettingsError(RuntimeError):
    pass


class MatchPolicyCache:
    """Hold the active matching policy, surviving restarts and cloud outages."""

    def __init__(self, path: Path | None = None, *, clock=None):
        self._path = Path(path) if path is not None else None
        self._rejection_path = (
            self._path.with_name(self._path.name + ".rejected")
            if self._path is not None else None
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = Lock()
        self._policy = DEFAULT_POLICY
        self._configured = False
        self._refreshed_at: datetime | None = None
        self._last_error: str | None = None
        # The envelope's side sections -- `gate_left_open` and
        # `automatic_open` -- kept raw and parsed only when read
        # (gate_left_open.config_from_settings, automatic_open.config_from_settings).
        # They ride the same poll as the schedule and must never be able to
        # change it: nothing about them is validated here, and nothing here
        # raises over them. Each is persisted in its own file beside the
        # schedule's cache, so a restart keeps it.
        self._sections: dict[str, object] = {name: None for name in SIDE_SECTIONS}
        self._section_paths = {
            name: (self._path.with_name(filename) if self._path is not None else None)
            for name, filename in SIDE_SECTIONS.items()
        }
        self._load_cached()

    def get(self) -> MatchPolicy:
        with self._lock:
            return self._policy

    def replace(self, document: object) -> None:
        """Adopt a settings envelope, persisting it for the next restart.

        A document the controller cannot read is not silently ignored: the
        policy fails closed to exact matches until a readable one arrives, and
        a marker on disk carries that state across a restart. The cached good
        document is deliberately left where it is — it is the thing to go back
        to once the cloud serves something readable again, and the marker, not
        its absence, is what keeps the gate closed in the meantime.
        """
        for name in SIDE_SECTIONS:
            if not self._keep_section(document, name) and isinstance(document, dict):
                # The section was refused; the schedule beside it is judged,
                # and cached, without it, so an oversized setting can neither
                # stick nor stop a good schedule surviving a restart.
                document = {key: value for key, value in document.items() if key != name}
        try:
            policy = policy_from_settings(document)
        except MatchPolicyError as error:
            self._persist_rejection(str(error))
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
        self._clear_rejection()
        with self._lock:
            self._policy = policy
            self._configured = True
            self._refreshed_at = self._clock()
            self._last_error = None

    def _keep_section(self, document: object, name: str) -> bool:
        """Adopt one of the envelope's side sections, and keep it for a restart.

        Persisted on its own, beside the schedule's cache, because the two are
        accepted independently: an envelope whose schedule is refused is not
        cached, and restoring the switch from that older cache would undo a
        change the owner made in the same envelope.
        """
        present = isinstance(document, dict) and name in document
        raw = document.get(name) if present else None
        validator = SECTION_VALIDATORS.get(name)
        if validator is not None and present and not validator(raw):
            LOGGER.warning("%s settings malformed; keeping the previous ones", name)
            # Refused, so the caller strips it from the envelope before the
            # schedule is cached: the malformed section must not be what a
            # restart reads back. And the value being kept must outlive the
            # restart on its own: a board whose only copy was the schedule's
            # cached envelope (no side file yet) would otherwise lose it to
            # that very strip. Materialise it now.
            with self._lock:
                retained = self._sections.get(name)
            path = self._section_paths.get(name)
            if retained is not None and path is not None and not path.exists():
                _write_atomically(
                    path, json.dumps({name: retained}, sort_keys=True, separators=(",", ":")),
                    f"cache the {name} settings",
                )
            return False
        section = raw if isinstance(raw, dict) else None
        encoded = json.dumps({name: section}, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_SETTINGS_BYTES:
            # Not adopted at all: one that cannot be kept would be undone by
            # the next restart. The real fields are a few dozen bytes.
            LOGGER.warning("%s settings too large; keeping the previous ones", name)
            return False
        with self._lock:
            changed = section != self._sections.get(name)
            self._sections[name] = section
        path = self._section_paths.get(name)
        if changed and path is not None:
            _write_atomically(path, encoded, f"cache the {name} settings")
        return True

    def _section(self, name: str):
        with self._lock:
            return self._sections.get(name)

    def gate_left_open(self, fallback):
        """The left-open alert's switch and threshold, or ``fallback``.

        Notify only, and independent of the schedule: a malformed section
        leaves ``fallback`` in force and the plate-matching policy untouched.
        """
        from .gate_left_open import config_from_settings

        return config_from_settings(self._section("gate_left_open"), fallback)

    def automatic_open(self, fallback):
        """The owner's pause switch against automatic opening, or ``fallback``.

        Independent of the schedule in the same way: a malformed section
        leaves ``fallback`` -- the board's environment -- in force, and the
        plate-matching policy untouched. It can only ever withhold a pulse.
        """
        from .automatic_open import config_from_settings

        return config_from_settings(self._section("automatic_open"), fallback)

    def mark_refresh_error(self, error: Exception) -> None:
        with self._lock:
            self._last_error = str(error)

    def status(self) -> dict:
        """The heartbeat's view of the schedule actually in force.

        The key names here are a contract with the Gate Mate Worker, which
        narrows the heartbeat against an allowlist and silently drops anything
        it does not recognise. ``bands``, ``configured`` and ``last_error`` are
        each read by the Settings page; renaming one here without renaming it
        in ``worker/routes/controller.ts`` makes the app show a healthy badge
        for a controller that has fallen closed.
        """
        with self._lock:
            return {
                "configured": self._configured,
                "bands": [band.to_wire() for band in self._policy.bands],
                "timezone": self._policy.timezone_name,
                "refreshed_at": (
                    self._refreshed_at.isoformat() if self._refreshed_at else None
                ),
                "last_error": _bounded_error(self._last_error),
            }

    def _load_cached(self) -> None:
        for name in SIDE_SECTIONS:
            self._load_cached_section(name)
        rejection = self._read_rejection()
        if rejection is not None:
            # The cloud was serving a document this controller refused when it
            # last ran. Restoring the cached schedule here would hand the gate
            # back the fuzziness the rejection took away, so stay closed until
            # a readable document arrives.
            LOGGER.warning(
                "controller settings were rejected before this restart (%s); "
                "matching stays exact-only until a valid schedule arrives",
                rejection,
            )
            self._policy = STRICT_POLICY
            self._configured = True
            self._last_error = rejection
            return
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

    def _load_cached_section(self, name: str) -> None:
        """The last side section received, so a restart keeps it.

        Its own file first; the schedule's cached envelope only for a board
        that has never written one.
        """
        for path in (self._section_paths.get(name), self._path):
            if path is None or not path.exists():
                continue
            try:
                if path.stat().st_size > MAX_SETTINGS_BYTES:
                    continue
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            section = document.get(name) if isinstance(document, dict) else None
            self._sections[name] = section if isinstance(section, dict) else None
            return

    def _persist(self, document: object) -> None:
        if self._path is None:
            return
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_SETTINGS_BYTES:
            LOGGER.warning("controller settings too large to cache locally")
            return
        _write_atomically(self._path, encoded, "cache controller settings")

    def _persist_rejection(self, error: str) -> None:
        """Record that the cloud is serving a document this controller refused."""
        if self._rejection_path is None:
            return
        _write_atomically(
            self._rejection_path,
            json.dumps({
                "rejected_at": self._clock().isoformat(),
                "error": _bounded_error(error),
            }, sort_keys=True, separators=(",", ":")),
            "record the controller settings rejection",
        )

    def _clear_rejection(self) -> None:
        if self._rejection_path is None:
            return
        try:
            self._rejection_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            LOGGER.warning("could not clear the settings rejection marker: %s", error)

    def _read_rejection(self) -> str | None:
        """Return the recorded rejection reason, or ``None`` if there is none.

        A marker that cannot be read is still a marker: the gate stays closed
        and the reason simply becomes generic. The alternative — treating an
        unreadable marker as no marker — would restore the cached schedule,
        which is the failure this whole mechanism exists to prevent.
        """
        if self._rejection_path is None or not self._rejection_path.exists():
            return None
        try:
            if self._rejection_path.stat().st_size > MAX_SETTINGS_BYTES:
                raise ValueError("rejection marker exceeds the size limit")
            marker = json.loads(self._rejection_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            LOGGER.warning("settings rejection marker is unreadable: %s", error)
            return "controller settings were rejected"
        reason = marker.get("error") if isinstance(marker, dict) else None
        return _bounded_error(reason) or "controller settings were rejected"


def _write_atomically(path: Path, contents: str, purpose: str) -> None:
    """Replace ``path`` with ``contents``, or log and leave it as it was."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", text=True
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                target.write(contents)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    except OSError as error:
        LOGGER.warning("could not %s: %s", purpose, error)


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
