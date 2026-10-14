"""The camera's own alarm sound, which the scanner recorded as the gate.

Until 2026-10-10 15:27 IST the camera played an alarm through its speaker at
every vehicle detection (`GetAudioAlarmV20`: `AI_VEHICLE`, every hour of the
week, speaker volume 92). It started 0.2-1.9 s after the alarm, lasted 9-15 s,
and drove the microphone to clipping -- about 35 dB louder than the gate's
motor. The sound scanner (`gate_sound_scan`, yamnet-linear-v2) wrote it into
``gate_movements`` as a motor run at every one of them: all twelve alarms
between 9 Oct 21:30 and 10 Oct 11:20 produced one
(docs/reviews/2026-10-10-gate-jam.md).

The rows stay in the table. Deleting them would make the history unreadable,
and the classification is a judgement that may be revised. Instead every
reader of ``gate_movements`` asks :func:`is_alarm_sound` and sets those rows
aside, so the left-open check, the heartbeat's counts, the direction scan and
the scanner's own starting state all see the same history.

The rule is deliberately narrow:

* **Only before the sound was switched off** (``ALARM_SOUND_OFF_AT``). After
  it, the minutes following an alarm are exactly when the real motor runs:
  the Pi pulses about 5 s after an alarm and the opening follows 1.3 s later.
  A blanket "near an alarm" exclusion would delete every commanded opening.
* **Only a run that starts within ``ALARM_SOUND_WITHIN_SECONDS`` of an
  alarm**, either side. The scanner places a run's start to within one 0.96 s
  embedding patch, so a sound that began 0.2-1.9 s after the alarm is written
  as starting anywhere from about a second before it to two after.

Where the alarm times come from: the event telemetry the processor already
keeps. Every frame a camera alarm produced carries the alarm in its
``trigger`` block (``source=reolink_webhook``, ``event_type=vehicle``) with
``delta_ms``, how long after the alarm the frame was received. Received time
less that delta is the instant the Pi received the alarm: over the 1001 such
frames on the Pi on 2026-10-14 it agrees with the early-trigger log's own
record of each alarm to the millisecond, and lands 0.05-1.43 s after the
camera's whole-second ``event_at``. An alarm that produced no frame at all
leaves nothing here; four of 236 since 2026-09-16 did (all on 2026-10-02).

Read-only, and nothing here can reach the gate (invariant 11).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

#: 2026-10-10 15:27 IST, when the camera's vehicle audio alarm was switched
#: off with the owner's OK (docs/reolink-rlc-811a.md). No row that starts
#: after this is ever set aside.
ALARM_SOUND_OFF_AT = datetime(2026, 10, 10, 14, 27, tzinfo=timezone.utc)
#: How close to an alarm a run must start to be the alarm sound.
ALARM_SOUND_WITHIN_SECONDS = 2.0
#: How long after an alarm a frame carrying it can be received. The longest
#: on record is 67 s (a waiting car's read); twice that leaves room.
FRAME_LAG_SECONDS = 150.0

ALARM_SOUND = "camera_alarm_sound"


def _parse(stamp) -> datetime | None:
    if isinstance(stamp, datetime):
        moment = stamp
    else:
        if not stamp:
            return None
        try:
            moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _stamp(moment: datetime) -> str:
    # Always with microseconds: `events.received_at` is compared as text, and
    # "09:04:13+00:00" sorts *before* "09:04:13.79+00:00".
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def alarm_instants(connection, since: datetime, until: datetime) -> list[datetime]:
    """When the Pi received each camera vehicle alarm between two instants.

    Read from the frames those alarms produced; see the module docstring.
    Returns an empty list on a database without the tables.
    """
    try:
        rows = connection.execute(
            "SELECT e.received_at, json_extract(t.payload, '$.trigger.delta_ms'),"
            " json_extract(t.payload, '$.trigger.event_at')"
            " FROM events e JOIN event_telemetry t ON t.event_id = e.id"
            " WHERE e.received_at >= ? AND e.received_at <= ?"
            " AND json_extract(t.payload, '$.trigger.source') = 'reolink_webhook'"
            " AND json_extract(t.payload, '$.trigger.event_type') = 'vehicle'",
            (_stamp(since), _stamp(until + timedelta(seconds=FRAME_LAG_SECONDS))),
        ).fetchall()
    except Exception:
        return []
    instants = []
    for received, delta_ms, event_at in rows:
        moment = None
        received_at = _parse(received)
        if received_at is not None and isinstance(delta_ms, (int, float)) and delta_ms >= 0:
            moment = received_at - timedelta(milliseconds=delta_ms)
        if moment is None:
            # No delta: the camera's own whole-second stamp is the next best.
            moment = _parse(event_at)
        if moment is not None and since <= moment <= until:
            instants.append(moment)
    return sorted(instants)


def is_alarm_sound(connection, started_at) -> bool:
    """Is the ``gate_movements`` row starting here the camera's alarm sound?

    True only for a row that starts before ``ALARM_SOUND_OFF_AT`` and within
    ``ALARM_SOUND_WITHIN_SECONDS`` of a camera vehicle alarm. Never raises.
    """
    start = _parse(started_at)
    if start is None or start >= ALARM_SOUND_OFF_AT:
        return False
    window = timedelta(seconds=ALARM_SOUND_WITHIN_SECONDS)
    return bool(alarm_instants(connection, start - window, start + window))


def heard_rows(connection, rows, *, started_at_index: int = 0):
    """``rows`` without the ones that are the camera's alarm sound, lazily.

    ``rows`` is any iterable of ``gate_movements`` rows (a cursor will do);
    ``started_at_index`` is where each row keeps its ``started_at``. Lazy so
    that a reader wanting the newest N can stop as soon as it has them.
    """
    for row in rows:
        if not is_alarm_sound(connection, row[started_at_index]):
            yield row
