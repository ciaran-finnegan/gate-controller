"""Say when the gate may have been left standing open. Notify only.

On 2026-10-09 at 21:41 IST the operator's own exit loop opened the gate for a
departing car, the auto-close started, the car broke the beam and the gate
reversed open again -- and then nothing. It stood open for 39 minutes, until
the owner drove in through it, and nothing in the system said a word.

This module reads the rows the sound scanner has already written
(``gate_movements`` and ``gate_listening``) and answers one question for the
heartbeat: has the gate been heard moving, and then *not* heard shutting, for
longer than the owner's threshold, while the microphone was actually
listening?

It is deliberately incapable of doing anything about it. It imports nothing
that can reach the relay, the actuation coordinator or the command server; it
opens the database read-only through the heartbeat's own connection; and the
only thing it returns is a small dictionary. The one closing action anywhere
in the system is a person pressing the app's existing gate button, which goes
through the existing command path, role check and cooldown. CLAUDE.md rules
1-3 are why: a pulse into a gate whose state is not known can stop or reverse
it, and an automatic "recovery" pulse is what jammed it on 2026-09-21.

Why the rule is what it is -- measured on the Pi, 2026-10-10, over every row
the retrained detector (yamnet-linear-v2) has written since 2026-09-21: 198
runs over 2.72 weeks of heard span, and 45 relay firings as ground truth
(docs/gate-left-open.md has the full tables):

* **The latch cannot be used to say "shut".** Of 22 relay-commanded cycles
  whose whole cycle was recorded, a latch was heard on 10. Of 122 bursts of
  movement, 41 ended with one.
* **A missing closing run cannot be used to say "open".** Of those 22
  commanded cycles, 5 produced a single run and 3 none at all. "The last
  movement was not confirmed shut, and nothing since" fires 8.1 times a week
  at a 10-minute threshold -- nearly every one a missed closing.
* **An odd burst of three or more is reliable.** Open, close, reverse open --
  and then silence -- is the physical signature of an interrupted auto-close
  that never re-ran. Over the same history it fired four times: 2026-09-21
  08:07 (the remote test that jammed the leaves), 2026-10-09 21:42 (the gate
  that stood open for 39 minutes), and two unverified (2026-10-04 16:51,
  2026-10-08 17:30, each followed by hours of silence). At most 0.7 false
  alerts a week, and the same four at every threshold from 5 to 30 minutes.

So the heartbeat says ``likely`` only for that signature, which is what the
app pushes, and ``possible`` for the ordinary "not confirmed shut", which it
shows and does not push.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

#: The shipped switch and threshold. Ten minutes is well past a full cycle
#: (opening ~20 s, hold 16-27 s, closing ~23 s, latched by about +70 s) and
#: well inside the 39 minutes the gate stood open on 2026-10-09. The rule's
#: replay over history is flat from 5 to 30 minutes, so the threshold sets how
#: soon the owner hears, not how often.
DEFAULT_ENABLED = True
DEFAULT_THRESHOLD_MINUTES = 10
#: Below three minutes the gate is still inside its own cycle; above four
#: hours the alert would arrive after anyone could act on it. The Worker
#: enforces the same bounds (`GATE_LEFT_OPEN_MIN_MINUTES` in access-gate-ui).
MIN_THRESHOLD_MINUTES = 3
MAX_THRESHOLD_MINUTES = 240

#: Runs this close together belong to one passage. A cycle's opening and
#: closing are 16-27 s apart (the hold), and a reversal follows its closing
#: within seconds; three minutes is far past both and far short of the next
#: car.
BURST_GAP_SECONDS = 180
#: How many recent movements are read to reconstruct the last burst. The
#: longest burst in history is four runs; twelve leaves room and keeps the
#: heartbeat's read bounded.
MOVEMENT_LOOKBACK = 12
#: Silence from a recorder that was not recording is not silence at the gate.
#: Over the measured history the recorder held 70% of the wall clock, so the
#: threshold must have been *heard*, not merely elapsed.
MIN_HEARD_FRACTION = 0.8

LIKELY = "likely"
POSSIBLE = "possible"

ENABLED_ENV = "GATE_LEFT_OPEN_ALERT_ENABLED"
THRESHOLD_ENV = "GATE_LEFT_OPEN_MINUTES"


@dataclass(frozen=True)
class LeftOpenConfig:
    enabled: bool = DEFAULT_ENABLED
    threshold_minutes: int = DEFAULT_THRESHOLD_MINUTES
    #: ``default``, ``environment`` or ``app``: where the values came from,
    #: so the dashboard can tell a setting the owner chose from a fallback.
    source: str = "default"


def _bounded_minutes(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            return None
    if not isinstance(value, int):
        return None
    if MIN_THRESHOLD_MINUTES <= value <= MAX_THRESHOLD_MINUTES:
        return value
    return None


def load_config(environment) -> LeftOpenConfig:
    """The controller's own defaults, from its environment. Never raises.

    A value that cannot be read falls back to the shipped default rather than
    failing the controller's start: this is a notification, and nothing about
    the gate's operation may depend on it parsing.
    """
    environment = environment or {}
    raw_enabled = str(environment.get(ENABLED_ENV, "")).strip().lower()
    raw_minutes = environment.get(THRESHOLD_ENV)
    enabled = DEFAULT_ENABLED
    source = "default"
    if raw_enabled in {"0", "false", "no", "off"}:
        enabled, source = False, "environment"
    elif raw_enabled in {"1", "true", "yes", "on"}:
        enabled, source = True, "environment"
    minutes = _bounded_minutes(raw_minutes) if raw_minutes not in (None, "") else None
    if minutes is not None:
        source = "environment"
    return LeftOpenConfig(
        enabled=enabled,
        threshold_minutes=minutes if minutes is not None else DEFAULT_THRESHOLD_MINUTES,
        source=source,
    )


def config_from_settings(section, fallback: LeftOpenConfig) -> LeftOpenConfig:
    """The owner's switch and threshold from the settings envelope, if valid.

    ``section`` is the envelope's ``gate_left_open`` object. Anything missing
    or malformed leaves the fallback in force; nothing here raises, so a bad
    document can never reach the plate-matching policy that shares the
    envelope.
    """
    if not isinstance(section, dict):
        return fallback
    enabled = section.get("enabled")
    minutes = _bounded_minutes(section.get("threshold_minutes"))
    if not isinstance(enabled, bool) or minutes is None:
        return fallback
    return LeftOpenConfig(enabled=enabled, threshold_minutes=minutes, source="app")


def _parse(stamp) -> datetime | None:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _heard(connection, since: datetime, until: datetime) -> tuple[float, datetime | None]:
    """Seconds of audio on the card between two instants, and how far the scan reaches.

    ``gate_listening`` has one row per scanned segment: the span it covers and
    the audio actually in it. Where in a short segment the missing audio fell
    is not knowable (docs/gate-audio.md 2g), so a partial overlap is credited
    pro rata. The second value is the end of the newest scanned span: past it
    nothing has been listened to yet, so nothing past it is silence.
    """
    rows = connection.execute(
        "SELECT started_at, span_seconds, audio_seconds FROM gate_listening"
        " WHERE started_at >= ? ORDER BY started_at LIMIT 500",
        ((since - timedelta(minutes=10)).isoformat(),),
    ).fetchall()
    heard = 0.0
    reach = None
    for started, span, audio in rows:
        start = _parse(started)
        if start is None or not span or span <= 0:
            continue
        end = start + timedelta(seconds=span)
        reach = end if reach is None or end > reach else reach
        low, high = max(since, start), min(until, end)
        if high > low:
            heard += (high - low).total_seconds() * max(0.0, min(audio, span)) / span
    return heard, reach


def evaluate(connection, config: LeftOpenConfig, *, now: datetime | None = None) -> dict:
    """The heartbeat's ``gate.left_open`` block. Read-only; never raises.

    ``state`` is one of:

    * ``open`` -- the gate was last heard moving at ``since`` and has not been
      heard shutting for ``threshold_minutes`` of listened time.
      ``confidence`` says how much that is worth: ``likely`` for the
      interrupted-closing signature, ``possible`` for a movement merely not
      confirmed shut.
    * ``clear`` -- a latch was heard at the end of the last movement, or the
      threshold has not been reached yet.
    * ``unknown`` -- nothing has been heard, or the microphone was not
      listening for enough of the threshold to say.
    * ``off`` -- the owner switched it off.

    ``since`` is the episode key: it stays the same for as long as the gate
    stays silent and changes the moment it is heard moving again, which is
    what clears an alert.
    """
    moment = now or datetime.now(timezone.utc)
    block = {"threshold_minutes": config.threshold_minutes, "config_source": config.source}
    if not config.enabled:
        return {**block, "state": "off"}
    try:
        rows = connection.execute(
            "SELECT started_at, ended_at, latch_at, outcome FROM gate_movements"
            " ORDER BY started_at DESC LIMIT ?", (MOVEMENT_LOOKBACK,),
        ).fetchall()
    except Exception:
        return {**block, "state": "unknown", "reason": "no_table"}
    runs = []
    for started, ended, latch, outcome in rows:
        start, end = _parse(started), _parse(ended)
        if start is None or end is None:
            continue
        runs.append((start, end, latch, outcome))
    if not runs:
        return {**block, "state": "unknown", "reason": "nothing_heard"}

    # The burst the last movement belongs to: every run chained to it by gaps
    # shorter than a passage.
    burst = [runs[0]]
    for run in runs[1:]:
        if (burst[-1][0] - run[1]).total_seconds() > BURST_GAP_SECONDS:
            break
        burst.append(run)
    burst.reverse()
    last = burst[-1]
    since = last[1]
    block.update({
        "since": since.isoformat(),
        "burst_started_at": burst[0][0].isoformat(),
        "runs": len(burst),
    })

    if last[2] or last[3] == "shut":
        return {**block, "state": "clear", "reason": "latched"}

    threshold = timedelta(minutes=config.threshold_minutes)
    try:
        heard, reach = _heard(connection, since, since + threshold)
    except Exception:
        return {**block, "state": "unknown", "reason": "no_listening"}
    quiet = max(0.0, ((reach or since) - since).total_seconds())
    block["quiet_seconds"] = round(quiet)
    block["heard_seconds"] = round(heard)
    if moment - since < threshold or quiet < threshold.total_seconds():
        # Either it has not been long enough, or the scan has not reached that
        # far yet. A movement after `since` may be sitting in audio nobody has
        # read; the scanner is on a quarter-hour timer and skips the segment
        # still being written.
        return {**block, "state": "clear", "reason": "within_threshold"}
    if heard < MIN_HEARD_FRACTION * threshold.total_seconds():
        return {**block, "state": "unknown", "reason": "not_listening"}

    interrupted = len(burst) >= 3 and len(burst) % 2 == 1
    return {
        **block,
        "state": "open",
        "confidence": LIKELY if interrupted else POSSIBLE,
        "reason": "closing_interrupted" if interrupted else "not_heard_shutting",
    }
