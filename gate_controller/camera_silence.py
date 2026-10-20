"""Say when no camera alarm has reached the Pi for a long time. Report only.

From 2026-10-18 12:52 UTC to 2026-10-20 11:26 UTC no AI-vehicle alarm from the
RLC-811A reached the Pi: no webhook and no FTP upload, both silent and back
at the same time. Every passage the app shows starts with a camera alarm, so
Gate Mate showed two quiet days and nothing said why. Why the alarms stopped
is not settled here. The camera may have stopped alarming, or there may have
been no traffic: the gate operator may not have been working, so nobody drove
up. The early trigger's thumbnails do show vehicles on 19 Oct at 09:02 and
22:17 UTC. Either way, from the app a silent camera and an empty drive look
the same, and this check says which numbers to look at.

The early trigger is the independent witness. It watches the small stream on
its own and writes every vision would-trigger and every camera alarm to
``early-trigger.db`` (``early_trigger_observations``, kinds ``would_trigger``
and ``camera_alarm``). On an ordinary day the camera alarms 0.4-0.7 times per
would-trigger; on 19 Oct it was 0 for 21. This module reads that record for
the heartbeat and answers one question: has no alarm come for
``GATE_CAMERA_ALARM_SILENCE_HOURS`` while the vision trigger fired at least
``GATE_CAMERA_ALARM_SILENCE_WOULD_TRIGGERS`` times since its last alarm?

Why 20 hours and 16 -- replayed over every row from 2026-09-22 to 2026-10-20
(docs/early-trigger.md, "Camera alarm silence"):

* **Nights are long and not empty.** The camera's longest ordinary silence
  was 23.1 h (Sunday 27-28 Sep) and an ordinary night can hold 15
  would-triggers (10-11 Oct, 17.7 h), so neither a time nor a count alone
  separates a dead camera from a quiet night.
* **Together they do.** No ordinary gap reached 20 h with more than 10
  would-triggers. All three silences on record do: 30 Sep 20:52 (36.6 h, 57
  would-triggers, the degraded network), 2 Oct 16:03 (42.9 h, 226) and
  18 Oct 12:52 (46.6 h, 35) -- flagged 23.8 h, 20.0 h and 25.3 h in, each by
  the next afternoon, and each long before it ended.

It is deliberately incapable of doing anything about it (CLAUDE.md rule 3,
invariant 11). It imports nothing that can reach the relay, the actuation
coordinator or the command server; it opens the record read-only on the
heartbeat's thread, never the burst thread; and the only thing it returns is a
small dictionary. It does not restart, reconfigure or poke the camera: a
person looks at it.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import logging
import math
from pathlib import Path
import sqlite3
from threading import Lock
from time import monotonic

from .early_trigger import KIND_CAMERA_ALARM, KIND_WOULD_TRIGGER

LOGGER = logging.getLogger(__name__)

ENV_HOURS = "GATE_CAMERA_ALARM_SILENCE_HOURS"
ENV_WOULD_TRIGGERS = "GATE_CAMERA_ALARM_SILENCE_WOULD_TRIGGERS"

#: See the module docstring: past the longest ordinary overnight silence that
#: still held many would-triggers, and inside a day.
DEFAULT_HOURS = 20.0
#: One more than the busiest ordinary night on record (15).
DEFAULT_WOULD_TRIGGERS = 16
#: Below two hours an ordinary afternoon trips it; above a week it says
#: nothing anyone could still act on.
MIN_HOURS, MAX_HOURS = 2.0, 168.0
MIN_WOULD_TRIGGERS, MAX_WOULD_TRIGGERS = 1, 1000
#: The heartbeat posts every 15 s; the record changes a few dozen times a
#: day. Reading it once a minute is ample and keeps the reader out of the way
#: of the alarm's own write.
EVALUATE_EVERY_SECONDS = 60.0

CLEAR, SILENT, UNKNOWN = "clear", "silent", "unknown"


def _bounded(environment, name: str, default, low, high):
    raw = environment.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = math.nan
    if not math.isfinite(value) or not low <= value <= high:
        LOGGER.warning("gate_camera_alarms %s=%r status=rejected using=%s", name, raw, default)
        return default
    return value


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def evaluate(path: Path, now_epoch: float, *, hours: float = DEFAULT_HOURS,
             would_triggers: int = DEFAULT_WOULD_TRIGGERS) -> dict:
    """The heartbeat's ``camera_alarms`` block, from the early trigger's record.

    ``state`` is one of:

    * ``silent`` -- no camera alarm for ``threshold_hours`` and at least
      ``threshold_would_triggers`` vision would-triggers since the last one.
      ``silent_since`` is the last alarm (or, with none on record, the first
      observation), and it is the episode key: it stays put while the camera
      is quiet and is cleared by the next alarm.
    * ``clear`` -- the camera has alarmed recently enough, or the vision
      trigger has not seen enough to say otherwise.
    * ``unknown`` -- there is no record to read.

    Read-only; never raises.
    """
    block = {
        "threshold_hours": hours, "threshold_would_triggers": would_triggers,
        "silent_since": None,
    }
    path = Path(path)
    if not path.is_file():
        return {**block, "state": UNKNOWN, "reason": "no_record"}
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)) as db:
            # Newest first by the time index, stopping at the first alarm.
            row = db.execute(
                "SELECT at_epoch FROM early_trigger_observations WHERE kind = ?"
                " ORDER BY at_epoch DESC LIMIT 1", (KIND_CAMERA_ALARM,),
            ).fetchone()
            last_alarm = row[0] if row else None
            if last_alarm is None:
                first = db.execute(
                    "SELECT MIN(at_epoch) FROM early_trigger_observations").fetchone()[0]
                start = first
                count = db.execute(
                    "SELECT COUNT(*) FROM early_trigger_observations WHERE kind = ?",
                    (KIND_WOULD_TRIGGER,),
                ).fetchone()[0]
            else:
                start = last_alarm
                count = db.execute(
                    "SELECT COUNT(*) FROM early_trigger_observations"
                    " WHERE at_epoch > ? AND kind = ?", (last_alarm, KIND_WOULD_TRIGGER),
                ).fetchone()[0]
    except Exception:
        return {**block, "state": UNKNOWN, "reason": "unreadable"}
    if start is None:
        return {**block, "state": UNKNOWN, "reason": "nothing_recorded"}
    quiet_hours = max(0.0, (now_epoch - start) / 3600.0)
    block.update({
        "last_alarm_at": _iso(last_alarm),
        "hours_without_alarm": round(quiet_hours, 1),
        "would_triggers_without_alarm": int(count),
    })
    if quiet_hours >= hours and count >= would_triggers:
        return {**block, "state": SILENT, "silent_since": _iso(start)}
    return {**block, "state": CLEAR}


class CameraAlarmSilence:
    """The heartbeat's view of :func:`evaluate`, and its journal lines.

    One WARNING when a silence is first seen and one when it clears, so the
    journal says when it started without saying so every 15 s. The state is
    held in memory: after a restart in the middle of a silence the first
    heartbeat journals it again, which is the right thing to say anyway.
    """

    def __init__(self, path: Path, *, hours: float = DEFAULT_HOURS,
                 would_triggers: int = DEFAULT_WOULD_TRIGGERS,
                 every_seconds: float = EVALUATE_EVERY_SECONDS,
                 clock=monotonic, wall_clock=None):
        self.path = Path(path)
        self.hours = hours
        self.would_triggers = int(would_triggers)
        self._every = every_seconds
        self._clock = clock
        self._wall = wall_clock or (lambda: datetime.now(timezone.utc).timestamp())
        self._lock = Lock()
        self._evaluated_at: float | None = None
        self._block: dict | None = None
        self._silent_since: str | None = None

    def status(self) -> dict:
        """The current block; re-read from the record at most once a minute. Never raises."""
        try:
            with self._lock:
                now = self._clock()
                if (self._block is not None and self._evaluated_at is not None
                        and now - self._evaluated_at < self._every):
                    return dict(self._block)
                block = evaluate(self.path, self._wall(), hours=self.hours,
                                 would_triggers=self.would_triggers)
                self._journal(block)
                self._evaluated_at, self._block = now, block
                return dict(block)
        except Exception:
            return {"state": UNKNOWN, "reason": "error", "silent_since": None}

    def _journal(self, block: dict) -> None:
        state = block.get("state")
        if state == SILENT and block["silent_since"] != self._silent_since:
            self._silent_since = block["silent_since"]
            LOGGER.warning(
                "gate_camera_alarms stage=silent silent_since=%s hours=%.1f would_triggers=%d "
                "threshold_hours=%g threshold_would_triggers=%d -- the camera has raised no "
                "vehicle alarm that reached the Pi while the early trigger kept firing; check the "
                "camera, its webhook and whether anything is using the drive",
                block["silent_since"], block["hours_without_alarm"],
                block["would_triggers_without_alarm"], self.hours, self.would_triggers,
            )
        elif state == CLEAR and self._silent_since is not None:
            LOGGER.warning(
                "gate_camera_alarms stage=cleared silent_since=%s last_alarm_at=%s",
                self._silent_since, block.get("last_alarm_at"),
            )
            self._silent_since = None
        # `unknown` says nothing either way: a record that cannot be read for
        # a minute neither starts nor ends a silence.


def build_monitor(environment, early_trigger, *, clock=monotonic,
                  wall_clock=None) -> CameraAlarmSilence | None:
    """The check the heartbeat reports, or None when the early trigger is off.

    Without the early trigger there is no independent witness: nothing else
    on the Pi sees a vehicle the camera did not report, so there is nothing
    to compare the camera's silence with.
    """
    path = getattr(early_trigger, "record_path", None)
    if path is None:
        return None
    environment = environment or {}
    return CameraAlarmSilence(
        path,
        hours=_bounded(environment, ENV_HOURS, DEFAULT_HOURS, MIN_HOURS, MAX_HOURS),
        would_triggers=int(_bounded(environment, ENV_WOULD_TRIGGERS, DEFAULT_WOULD_TRIGGERS,
                                    MIN_WOULD_TRIGGERS, MAX_WOULD_TRIGGERS)),
        clock=clock, wall_clock=wall_clock,
    )
