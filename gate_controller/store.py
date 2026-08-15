import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .models import ActuationClaim, GateEvent, TerminalOutcome


class LocalStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def was_opened_since(self, cutoff: datetime) -> bool:
        with closing(self._connect()) as connection:
            return self._was_opened_since(connection, cutoff)

    def claim_actuation(self, idempotency_key: str, claimed_at: datetime,
                        cooldown_cutoff: datetime | None = None, *,
                        monotonic_cutoff: float | None = None,
                        boot_id: str | None = None, event: GateEvent | None = None,
                        outbox_payload: dict | None = None,
                        command_ack: tuple[str, datetime] | None = None) -> ActuationClaim:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id, state, claimed_at FROM actuation_claims WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                connection.commit()
                status = "indeterminate_claim" if existing[1] == "claimed" else "duplicate_event"
                return ActuationClaim(idempotency_key, status, _parse_timestamp(existing[2]), existing[0])
            if cooldown_cutoff is not None and self._was_opened_since(
                connection, cooldown_cutoff, monotonic_cutoff, boot_id
            ):
                connection.commit()
                return ActuationClaim(idempotency_key, "cooldown")
            cursor = connection.execute(
                """
                INSERT INTO actuation_claims (
                    idempotency_key, claimed_at, state, activation_attempt_at,
                    pending_event, pending_outbox_payload, pending_command_id,
                    pending_command_created_at
                ) VALUES (?, ?, 'claimed', NULL, ?, ?, ?, ?)
                """,
                (
                    idempotency_key, _timestamp(claimed_at),
                    _encode_pending_event(event) if event is not None else None,
                    _encode_optional_json(outbox_payload),
                    command_ack[0] if command_ack is not None else None,
                    _timestamp(command_ack[1]) if command_ack is not None else None,
                ),
            )
            connection.commit()
            return ActuationClaim(idempotency_key, "claimed", _as_utc(claimed_at), cursor.lastrowid)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_actuation_attempt(self, claim: ActuationClaim, attempted_at: datetime, *,
                               event: GateEvent, outbox_payload: dict | None = None,
                               command_ack: tuple[str, datetime] | None = None,
                               attempted_monotonic: float | None = None,
                               boot_id: str | None = None) -> None:
        """Persist recovery data and the global inhibit before GPIO can be energized."""
        if claim.status != "claimed" or claim.claim_id is None:
            raise ValueError("actuation attempt requires an active claim")
        pending_command_id = command_ack[0] if command_ack is not None else None
        pending_command_created_at = (
            _timestamp(command_ack[1]) if command_ack is not None else None
        )
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE actuation_claims
                SET activation_attempt_at = ?, pending_event = ?,
                    pending_outbox_payload = ?, pending_command_id = ?,
                    pending_command_created_at = ?, activation_monotonic = ?, boot_id = ?
                WHERE id = ? AND state = 'claimed' AND activation_attempt_at IS NULL
                """,
                (
                    _timestamp(attempted_at), _encode_pending_event(event),
                    _encode_optional_json(outbox_payload), pending_command_id,
                    pending_command_created_at, attempted_monotonic, boot_id,
                    claim.claim_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("actuation claim is no longer safe to execute")

    def finalize_actuation(self, claim: ActuationClaim, event: GateEvent, *,
                           terminal_status: str | None = None,
                           terminal_detail: str | None = None,
                           outbox_payload: dict | None = None,
                           command_ack: tuple[str, datetime] | None = None,
                           retain_activation_attempt: bool = True) -> int:
        if claim.status != "claimed" or event.idempotency_key != claim.idempotency_key:
            raise ValueError("event does not match active actuation claim")
        status = terminal_status or ("completed" if event.opened else "failed")
        keep_attempt = retain_activation_attempt and status != "expired"
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM actuation_claims WHERE id = ?", (claim.claim_id,)
            ).fetchone()
            if row is None or row[0] != "claimed":
                raise RuntimeError("actuation claim is no longer active")
            event_id = self._insert_event(connection, event)
            self._ensure_outbox(connection, event_id, outbox_payload)
            self._ensure_command_ack(connection, command_ack, status, terminal_detail)
            connection.execute(
                """
                UPDATE actuation_claims
                SET state = 'finalized', event_id = ?, terminal_status = ?, terminal_detail = ?,
                    activation_attempt_at = CASE
                        WHEN ? THEN activation_attempt_at ELSE NULL
                    END, activation_monotonic = CASE
                        WHEN ? THEN activation_monotonic ELSE NULL
                    END, boot_id = CASE WHEN ? THEN boot_id ELSE NULL END,
                    pending_event = NULL, pending_outbox_payload = NULL,
                    pending_command_id = NULL, pending_command_created_at = NULL
                WHERE id = ?
                """,
                (
                    event_id, status, terminal_detail, int(keep_attempt), int(keep_attempt),
                    int(keep_attempt), claim.claim_id,
                ),
            )
            connection.commit()
            return event_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_terminal_outcome(self, event: GateEvent, *, status: str, detail: str | None,
                                outbox_payload: dict | None = None,
                                command_ack: tuple[str, datetime] | None = None) -> int:
        """Record a no-pulse terminal decision, including command cooldown failures."""
        if not event.idempotency_key:
            return self.record_event_with_outbox(event, outbox_payload)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT event_id FROM actuation_claims WHERE idempotency_key = ?",
                (event.idempotency_key,),
            ).fetchone()
            if existing:
                if existing[0] is not None:
                    self._ensure_outbox(connection, existing[0], outbox_payload)
                self._ensure_command_ack(connection, command_ack, status, detail)
                connection.commit()
                return existing[0]
            event_id = self._insert_event(connection, event)
            self._ensure_outbox(connection, event_id, outbox_payload)
            self._ensure_command_ack(connection, command_ack, status, detail)
            connection.execute(
                """
                INSERT INTO actuation_claims (
                    idempotency_key, claimed_at, state, event_id, terminal_status, terminal_detail
                ) VALUES (?, ?, 'finalized', ?, ?, ?)
                """,
                (event.idempotency_key, _timestamp(event.decision_at or event.received_at),
                 event_id, status, detail),
            )
            connection.commit()
            return event_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_event_with_outbox(self, event: GateEvent,
                                 outbox_payload: dict | None = None) -> int:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                event_id = self._insert_event(connection, event)
            except sqlite3.IntegrityError:
                if not event.idempotency_key:
                    raise
                row = connection.execute(
                    "SELECT id FROM events WHERE idempotency_key = ?", (event.idempotency_key,)
                ).fetchone()
                if row is None:
                    raise
                event_id = row[0]
            self._ensure_outbox(connection, event_id, outbox_payload)
            connection.commit()
            return event_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_event(self, event: GateEvent) -> int:
        return self.record_event_with_outbox(event)

    def recover_interrupted_actuations(self) -> int:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            recovered = self._recover_interrupted_actuations(connection)
            connection.commit()
            return recovered
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def terminal_outcome(self, idempotency_key: str) -> TerminalOutcome | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT terminal_status, terminal_detail, event_id, state
                FROM actuation_claims WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
        if row is None or row[3] == "claimed":
            return None
        return TerminalOutcome(row[0] or "failed", row[1], row[2])

    def event_exists(self, idempotency_key: str) -> bool:
        return self.actuation_claim_status(idempotency_key) is not None or self._event_id(idempotency_key) is not None

    def event_id(self, idempotency_key: str) -> int | None:
        return self._event_id(idempotency_key)

    def actuation_claim_status(self, idempotency_key: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT state FROM actuation_claims WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        if row is None:
            return None
        return "indeterminate_claim" if row[0] == "claimed" else "duplicate_event"

    def ensure_outbox(self, event_id: int, payload: dict) -> int | None:
        with closing(self._connect()) as connection, connection:
            return self._ensure_outbox(connection, event_id, payload)

    def queue_outbox(self, event_id: int, payload: dict) -> int:
        return self.ensure_outbox(event_id, payload) or self._outbox_id(event_id)

    def pending_outbox_count(self) -> int:
        with closing(self._connect()) as connection:
            return connection.execute("SELECT COUNT(*) FROM outbox WHERE completed_at IS NULL").fetchone()[0]

    def pending_outbox_items(self, limit: int = 20) -> list[tuple[int, dict]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT id, payload FROM outbox WHERE completed_at IS NULL ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
        return [(row[0], json.loads(row[1])) for row in rows]

    def pending_evidence_digests(self) -> set[str]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT payload FROM outbox WHERE completed_at IS NULL"
            ).fetchall()
            rows.extend(connection.execute(
                """
                SELECT pending_outbox_payload FROM actuation_claims
                WHERE state = 'claimed' AND pending_outbox_payload IS NOT NULL
                """
            ).fetchall())
        digests = set()
        for (payload_text,) in rows:
            try:
                digest = json.loads(payload_text).get("image_sha256")
            except (AttributeError, TypeError, ValueError):
                continue
            if isinstance(digest, str):
                digests.add(digest)
        return digests

    def bind_pending_outbox_controller(self, controller_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT id, payload FROM outbox WHERE completed_at IS NULL"
            ).fetchall()
            for outbox_id, payload_text in rows:
                try:
                    payload = json.loads(payload_text)
                except (TypeError, ValueError):
                    payload = {}
                changed = False
                legacy_path = payload.pop("_local_image_path", None)
                if legacy_path is not None:
                    changed = True
                    if "image_sha256" not in payload:
                        payload["image_status"] = "legacy_evidence_unavailable"
                if not payload.get("controller_id"):
                    payload["controller_id"] = controller_id
                    changed = True
                if not changed:
                    continue
                connection.execute(
                    "UPDATE outbox SET payload = ? WHERE id = ?",
                    (json.dumps(payload, sort_keys=True), outbox_id),
                )

    def event_payload(self, event_id: int) -> dict:
        with closing(self._connect()) as connection:
            payload = self._event_payload(connection, event_id)
        if payload is None:
            raise KeyError(f"unknown event {event_id}")
        return payload

    def queue_command_ack(self, command_id: str, status: str, detail: str | None,
                          created_at: datetime) -> None:
        with closing(self._connect()) as connection, connection:
            self._ensure_command_ack(connection, (command_id, created_at), status, detail)

    def pending_command_acks(self, limit: int = 20) -> list[tuple[str, str, str | None]]:
        with closing(self._connect()) as connection:
            return connection.execute(
                """
                SELECT command_id, status, detail FROM command_ack_outbox
                ORDER BY created_at LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def complete_command_ack(self, command_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM command_ack_outbox WHERE command_id = ?", (command_id,))

    def complete_outbox_item(self, item_id: int, completed_at: datetime | None = None) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE outbox SET completed_at = ? WHERE id = ?",
                (_timestamp(completed_at or datetime.now(timezone.utc)), item_id),
            )

    def _event_id(self, idempotency_key: str) -> int | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT id FROM events WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        return row[0] if row else None

    def _outbox_id(self, event_id: int) -> int:
        with closing(self._connect()) as connection:
            return connection.execute("SELECT id FROM outbox WHERE event_id = ?", (event_id,)).fetchone()[0]

    @staticmethod
    def _ensure_command_ack(connection: sqlite3.Connection,
                            command_ack: tuple[str, datetime] | None,
                            status: str, detail: str | None) -> None:
        if command_ack is None:
            return
        command_id, created_at = command_ack
        connection.execute(
            """
            INSERT INTO command_ack_outbox (command_id, status, detail, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(command_id) DO UPDATE SET
                status = excluded.status, detail = excluded.detail
            """,
            (command_id, status, detail, _timestamp(created_at)),
        )

    @staticmethod
    def _ensure_outbox(connection: sqlite3.Connection, event_id: int,
                       payload: dict | None) -> int | None:
        if payload is None:
            return None
        encoded = LocalStore._event_payload(connection, event_id) or {}
        encoded.update(payload)
        if encoded.get("event_id") is None:
            encoded["event_id"] = event_id
        cursor = connection.execute(
            "INSERT OR IGNORE INTO outbox (event_id, payload, created_at) VALUES (?, ?, ?)",
            (event_id, json.dumps(encoded, sort_keys=True), _timestamp(datetime.now(timezone.utc))),
        )
        return cursor.lastrowid or None

    @staticmethod
    def _was_opened_since(connection: sqlite3.Connection, cutoff: datetime,
                          monotonic_cutoff: float | None = None,
                          boot_id: str | None = None) -> bool:
        if monotonic_cutoff is not None and boot_id is not None:
            monotonic_attempt = connection.execute(
                """
                SELECT 1 FROM actuation_claims
                WHERE boot_id = ? AND activation_monotonic >= ? LIMIT 1
                """,
                (boot_id, monotonic_cutoff),
            ).fetchone()
            if monotonic_attempt is not None:
                return True
            previous_boot = connection.execute(
                """
                SELECT boot_id FROM actuation_claims
                WHERE activation_attempt_at IS NOT NULL AND boot_id IS NOT NULL
                ORDER BY activation_attempt_at DESC LIMIT 1
                """
            ).fetchone()
            if (previous_boot is not None and previous_boot[0] != boot_id
                    and monotonic_cutoff < 0):
                return True
        cutoff_text = _timestamp(cutoff)
        event = connection.execute(
            """
            SELECT 1 FROM events WHERE (
                relay_activated_at >= ? OR
                (opened = 1 AND relay_activated_at IS NULL AND received_at >= ?)
            ) LIMIT 1
            """, (cutoff_text, cutoff_text),
        ).fetchone()
        if event is not None:
            return True
        return connection.execute(
            """
            SELECT 1 FROM actuation_claims
            WHERE activation_attempt_at >= ? LIMIT 1
            """,
            (cutoff_text,),
        ).fetchone() is not None

    @staticmethod
    def _event_payload(connection: sqlite3.Connection, event_id: int) -> dict | None:
        row = connection.execute(
            """
            SELECT id, received_at, decision_at, relay_activated_at, source, reason,
                   opened, idempotency_key, authorised_plate, observed_plate,
                   ocr_confidence
            FROM events WHERE id = ?
            """,
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        keys = (
            "event_id", "received_at", "decision_at", "relay_activated_at", "source",
            "reason", "opened", "idempotency_key", "authorised_plate",
            "observed_plate", "ocr_confidence",
        )
        payload = dict(zip(keys, row))
        payload["schema_version"] = 2
        payload["opened"] = bool(payload["opened"])
        return payload

    @staticmethod
    def _insert_event(connection: sqlite3.Connection, event: GateEvent) -> int:
        cursor = connection.execute(
            """
            INSERT INTO events (
                received_at, decision_at, relay_activated_at, source, reason, opened,
                idempotency_key, authorised_plate, observed_plate, ocr_confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (_timestamp(event.received_at), _optional_timestamp(event.decision_at),
             _optional_timestamp(event.relay_activated_at), event.source, event.reason,
             int(event.opened), event.idempotency_key, event.authorised_plate,
             event.observed_plate, event.ocr_confidence),
        )
        return cursor.lastrowid

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def _migrate(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, received_at TEXT NOT NULL, decision_at TEXT,
                    relay_activated_at TEXT, source TEXT NOT NULL, reason TEXT NOT NULL,
                    opened INTEGER NOT NULL, idempotency_key TEXT UNIQUE, authorised_plate TEXT,
                    observed_plate TEXT, ocr_confidence REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS actuation_claims (
                    id INTEGER PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                    claimed_at TEXT NOT NULL, state TEXT NOT NULL,
                    event_id INTEGER REFERENCES events(id), detail TEXT,
                    terminal_status TEXT, terminal_detail TEXT,
                    activation_attempt_at TEXT, activation_monotonic REAL, boot_id TEXT,
                    pending_event TEXT,
                    pending_outbox_payload TEXT, pending_command_id TEXT,
                    pending_command_created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES events(id),
                    payload TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS command_ack_outbox (
                    command_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                    detail TEXT, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_relay_cooldown ON events (opened, relay_activated_at);
                CREATE INDEX IF NOT EXISTS events_received_cooldown ON events (opened, received_at);
                CREATE UNIQUE INDEX IF NOT EXISTS outbox_one_per_event ON outbox (event_id);
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(actuation_claims)")}
            for name in (
                "terminal_status", "terminal_detail", "activation_attempt_at",
                "activation_monotonic", "boot_id",
                "pending_event", "pending_outbox_payload", "pending_command_id",
                "pending_command_created_at",
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE actuation_claims ADD COLUMN {name} TEXT")
            applied = connection.execute("SELECT 1 FROM schema_migrations WHERE name = 'legacy_log_import'").fetchone()
            if not applied and self._legacy_log_exists(connection):
                self._import_legacy_log(connection)
            connection.execute("INSERT OR IGNORE INTO schema_migrations (name) VALUES ('legacy_log_import')")
            enriched = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE name = 'outbox_payload_v1'"
            ).fetchone()
            if not enriched:
                for outbox_id, event_id, payload_text, completed_at in connection.execute(
                    "SELECT id, event_id, payload, completed_at FROM outbox"
                ).fetchall():
                    if completed_at is not None:
                        continue
                    try:
                        payload = json.loads(payload_text)
                    except (TypeError, ValueError):
                        payload = {}
                    event_payload = self._event_payload(connection, event_id) or {}
                    event_payload.update(payload)
                    connection.execute(
                        "UPDATE outbox SET payload = ? WHERE id = ?",
                        (json.dumps(event_payload, sort_keys=True), outbox_id),
                    )
                connection.execute(
                    "INSERT INTO schema_migrations (name) VALUES ('outbox_payload_v1')"
                )
            evidence_v2 = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE name = 'outbox_evidence_v2'"
            ).fetchone()
            if not evidence_v2:
                for outbox_id, event_id, payload_text in connection.execute(
                    "SELECT id, event_id, payload FROM outbox"
                ).fetchall():
                    try:
                        payload = json.loads(payload_text)
                    except (TypeError, ValueError):
                        payload = {}
                    legacy_path = payload.pop("_local_image_path", None)
                    if legacy_path is not None and "image_sha256" not in payload:
                        payload["image_status"] = "legacy_evidence_unavailable"
                    event_payload = self._event_payload(connection, event_id) or {}
                    event_payload.update(payload)
                    connection.execute(
                        "UPDATE outbox SET payload = ? WHERE id = ?",
                        (json.dumps(event_payload, sort_keys=True), outbox_id),
                    )
                connection.execute(
                    "INSERT INTO schema_migrations (name) VALUES ('outbox_evidence_v2')"
                )
    def _recover_interrupted_actuations(self, connection: sqlite3.Connection) -> int:
        rows = connection.execute(
            """
            SELECT id, pending_event, pending_outbox_payload, pending_command_id,
                   pending_command_created_at, activation_attempt_at
            FROM actuation_claims
            WHERE state = 'claimed' AND pending_event IS NOT NULL
            """
        ).fetchall()
        recovered_count = 0
        for (claim_id, event_text, outbox_text, command_id, command_created_at,
             activation_attempt_at) in rows:
            try:
                event = _decode_pending_event(event_text)
                outbox_payload = _decode_optional_json(outbox_text)
                command_ack = (
                    (command_id, _parse_timestamp(command_created_at))
                    if command_id is not None and command_created_at is not None else None
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            recovery_reason = (
                "indeterminate_claim" if activation_attempt_at is not None
                else "interrupted_before_activation"
            )
            recovered = GateEvent(
                source=event.source, reason=recovery_reason, opened=False,
                idempotency_key=event.idempotency_key, received_at=event.received_at,
                decision_at=event.decision_at, relay_activated_at=None,
                authorised_plate=event.authorised_plate,
                observed_plate=event.observed_plate,
                ocr_confidence=event.ocr_confidence,
            )
            try:
                event_id = self._insert_event(connection, recovered)
            except sqlite3.IntegrityError:
                row = connection.execute(
                    "SELECT id FROM events WHERE idempotency_key = ?",
                    (event.idempotency_key,),
                ).fetchone()
                if row is None:
                    raise
                event_id = row[0]
            self._ensure_outbox(connection, event_id, outbox_payload)
            self._ensure_command_ack(
                connection, command_ack, "failed", recovery_reason
            )
            connection.execute(
                """
                UPDATE actuation_claims
                SET state = 'finalized', event_id = ?, terminal_status = 'failed',
                    terminal_detail = ?, pending_event = NULL,
                    pending_outbox_payload = NULL, pending_command_id = NULL,
                    pending_command_created_at = NULL
                WHERE id = ? AND state = 'claimed'
                """,
                (event_id, recovery_reason, claim_id),
            )
            recovered_count += 1
        return recovered_count

    @staticmethod
    def _legacy_log_exists(connection: sqlite3.Connection) -> bool:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(log)")}
        return {"timestamp", "gate_opened"}.issubset(columns)

    @staticmethod
    def _import_legacy_log(connection: sqlite3.Connection) -> None:
        for timestamp, opened in connection.execute("SELECT timestamp, gate_opened FROM log"):
            connection.execute(
                "INSERT INTO events (received_at, source, reason, opened) VALUES (?, 'legacy', 'legacy_log_import', ?)",
                (_timestamp(_parse_timestamp(timestamp)), int(_legacy_true(opened))),
            )


def _legacy_true(value) -> bool:
    return value if isinstance(value, bool) else (value != 0 if isinstance(value, (int, float)) else isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "y", "on"})


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _encode_pending_event(event: GateEvent) -> str:
    return json.dumps({
        "source": event.source,
        "reason": event.reason,
        "idempotency_key": event.idempotency_key,
        "received_at": _timestamp(event.received_at),
        "decision_at": _optional_timestamp(event.decision_at),
        "authorised_plate": event.authorised_plate,
        "observed_plate": event.observed_plate,
        "ocr_confidence": event.ocr_confidence,
    }, sort_keys=True)


def _decode_pending_event(encoded: str) -> GateEvent:
    payload = json.loads(encoded)
    if not isinstance(payload, dict):
        raise ValueError("pending event is not an object")
    return GateEvent(
        source=payload["source"], reason=payload["reason"], opened=False,
        idempotency_key=payload["idempotency_key"],
        received_at=_parse_timestamp(payload["received_at"]),
        decision_at=(
            _parse_timestamp(payload["decision_at"])
            if payload.get("decision_at") is not None else None
        ),
        authorised_plate=payload.get("authorised_plate"),
        observed_plate=payload.get("observed_plate"),
        ocr_confidence=float(payload.get("ocr_confidence", 0.0)),
    )


def _encode_optional_json(payload: dict | None) -> str | None:
    return json.dumps(payload, sort_keys=True) if payload is not None else None


def _decode_optional_json(encoded: str | None) -> dict | None:
    if encoded is None:
        return None
    payload = json.loads(encoded)
    if not isinstance(payload, dict):
        raise ValueError("pending payload is not an object")
    return payload


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _optional_timestamp(value: datetime | None) -> str | None:
    return _timestamp(value) if value is not None else None


def _parse_timestamp(value) -> datetime:
    try:
        return _as_utc(value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (AttributeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
