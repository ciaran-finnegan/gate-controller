#!/usr/bin/env python3
"""What the early trigger would have bought, from what it recorded on live traffic.

Reads the early trigger's own SQLite file (``early-trigger.db``, beside the
controller's database) and prints, by day and by night:

* passages, and the lead of the would-trigger over the camera's alarm
  (median, p10, p90);
* misses: camera alarms with no would-trigger in the six seconds before them;
* false triggers an hour: would-triggers with no camera alarm within a minute,
  split into those the confirmation layer removed and those that *passed* it
  (the number that matters for ``on``);
* the false triggers bucketed by the detector's own evidence, so that passing
  headlights, rain and foliage can be told apart from the numbers;
* an estimate of the seconds saved to the first good local read;
* the layer table: what each confirmation layer, and each pair, would have
  removed of the false triggers and cost of the true ones, and the lead left.

"Day" and "night" are the detector's own: the measured luma of the patch's
background, not the clock and not the match policy's schedule. The question
that matters is whether the patch was black.

The hours a rate is over come from the journal (``--journal``: the
``stage=status`` lines' cumulative sample counts, which cover every armed
second, quiet ones included) or from ``--hours-day`` / ``--hours-night``;
without either they are estimated from the rows alone, which leaves out every
quiet stretch and so overstates the rate. The first shadow day's 3.18 an hour
was over 2.2 rows-only hours; over the ~7 daylight hours actually watched it
was under 1.5.

With ``--audio-segments`` it also reads the recorder's AAC segments and says,
for every would-trigger and every camera alarm, whether a vehicle-like sound
was already rising and for how long; with ``--events-database`` whether the
gate's motor had just run un-commanded (somebody leaving, not arriving); and
with ``--dump-audio-features`` it writes the band levels of the ten seconds
before every camera alarm, and of quiet moments, for a later "on the gravel or
on the road" model. Nothing is trained here.

Read-only on everything but its own output.
"""
from __future__ import annotations

import argparse
import array
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random
import re
import sqlite3
import subprocess
import sys

try:
    import gate_controller  # noqa: F401
except ImportError:
    # Run straight from a checkout. Appended, not inserted: the repository's
    # root holds modules whose names a test run must not find first.
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from gate_controller.early_trigger import (  # noqa: E402
    DEFAULT_FPS, KIND_CAMERA_ALARM, KIND_WOULD_TRIGGER, USEFUL_LEAD_SECONDS, clip_sees_vehicle,
    correlate, ensure_schema,
)
from gate_controller.gate_audio_detect import BANDS  # noqa: E402

#: The session decoder takes about a second to deliver its first frame and a
#: read about 0.2 s, so a sweep started at the would-trigger cannot have a read
#: sooner than this after it.
MIN_READ_SECONDS = 1.2
SAMPLE_RATE = 16000
ONSET_RISE_DB = 6.0
ONSET_HOLD_SECONDS = 2.0
ONSET_WINDOW_SECONDS = 30.0
VEHICLE_BANDS = (1, 2)  # 150-400 and 400-1200 Hz: tyres on gravel and engine, above the wind
WIND_BAND, HISS_BAND = 0, 4
UNCOMMANDED_BEFORE_SECONDS = 90.0
_SEGMENT = re.compile(r"(\d{8}T\d{6})Z")
_STATUS = re.compile(r"stage=status (\{.*\})\s*$")
_CONFIGURED_FPS = re.compile(r"stage=configured .*\bfps=([0-9.]+)")
_JOURNAL_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:?\d{2}|Z)?)")
LIGHTS = ("day", "night")


def percentile(values, share: float):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * share
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def load_rows(connection) -> list[dict]:
    connection.row_factory = sqlite3.Row
    rows = []
    for row in connection.execute("SELECT * FROM early_trigger_observations ORDER BY at_epoch"):
        item = dict(row)
        for name in ("features", "layers", "sweep"):
            try:
                item[name] = json.loads(item[name]) if item[name] else {}
            except ValueError:
                item[name] = {}
        rows.append(item)
    return rows


def observed_hours(rows, light: str) -> float:
    """Hours the record covers in one light, from the rows themselves.

    Every row says which detector was running, so a span is attributed to the
    light of the row that starts it, and a gap of over an hour counts as one.
    A quiet stretch with no rows at all is therefore under-counted, which
    makes the false-an-hour figure err high; ``--hours-day`` / ``--hours-night``
    override it from the journal's ``stage=status`` lines when that matters.
    """
    total = 0.0
    for first, second in zip(rows, rows[1:]):
        if first.get("light") == light:
            total += max(0.0, min(3600.0, second["at_epoch"] - first["at_epoch"]))
    return total / 3600.0


def journal_hours(lines, *, fps: float | None = None, since: float | None = None) -> dict:
    """Hours watched in each light, from the journal's ``stage=status`` lines.

    Each line carries the worker's cumulative sample count and the light the
    detector was in; the samples since the previous line, at the sampling
    rate (``fps=`` from the ``stage=configured`` line, or given), are the
    seconds watched, credited to the light the line reports. A count lower
    than the last is a restart and counts from zero. Only armed time is
    counted, which is the time a false trigger could have happened in.
    """
    hours = {light: 0.0 for light in LIGHTS}
    rate = fps
    previous = None
    for line in lines:
        if rate is None:
            configured = _CONFIGURED_FPS.search(line)
            if configured:
                rate = float(configured.group(1))
        found = _STATUS.search(line)
        if not found:
            continue
        if since is not None:
            stamp = _JOURNAL_STAMP.match(line)
            if stamp:
                try:
                    at = datetime.fromisoformat(stamp.group(1).replace("Z", "+00:00"))
                    if at.tzinfo is None:
                        at = at.replace(tzinfo=timezone.utc)
                    if at.timestamp() < since:
                        continue
                except ValueError:
                    pass
        try:
            status = json.loads(found.group(1))
            samples = int(status.get("samples") or 0)
        except (ValueError, TypeError):
            continue
        light = status.get("light")
        delta = samples if previous is None or samples < previous else samples - previous
        previous = samples
        if light in hours:
            hours[light] += delta / (rate or DEFAULT_FPS) / 3600.0
    return {light: round(value, 3) for light, value in hours.items()}


def bucket(features: dict) -> str:
    """Name the likely cause of a false trigger from its evidence numbers."""
    if features.get("light") == "night":
        if (features.get("shift") or 0) >= 20:
            return "night: whole patch lit (floodlight, or a beam flooding the lens)"
        if abs(features.get("track_dx") or 0) >= 0.3 or (features.get("speed") or 0) >= 0.6:
            return "night: a source moving fast across the patch (passing headlights)"
        if (features.get("blob_cells") or 0) <= 3:
            return "night: a small bright source (distant lamp, rain in a beam)"
        return "night: a persistent bright source"
    if (features.get("scatter") or 0) >= 0.06:
        return "day: change scattered over the patch (foliage, rain, dappled shade)"
    if abs(features.get("luma_jump") or 0) >= 15:
        return "day: the patch changed brightness (sun, shade, exposure)"
    if (features.get("blob_fraction") or 0) >= 0.3:
        return "day: a large region at once"
    return "day: one compact region (animal, person, vehicle the camera ignored)"


def layer_votes(row: dict) -> dict:
    """Each confirmation layer's opinion of one would-trigger: True, False or None (no opinion)."""
    layers = row.get("layers") or {}
    clip = layers.get("clip") or {}
    plate = layers.get("plate_look") or {}
    sweep = row.get("sweep") or {}
    audio = row.get("audio") or {}
    votes = {"clip": clip_sees_vehicle(clip), "plate": None, "audio": None, "either": None}
    if plate.get("status") == "ok":
        votes["plate"] = bool(plate.get("plate_box"))
    elif sweep:
        votes["plate"] = bool(sweep.get("plate_reads"))
    if audio.get("status") == "ok":
        votes["audio"] = audio.get("onset_lead_seconds") is not None
    judged = [votes[name] for name in ("clip", "plate") if votes[name] is not None]
    if judged:
        votes["either"] = any(judged)
    return votes


def decision_of(row: dict) -> str:
    """What the confirmation layer made of one would-trigger: ``confirmed``, ``removed`` or ``unjudged``.

    The worker's own decision when it recorded one (every row since the
    layers went on the decision path); before that, what the recorded looks
    would have decided, by the same vote.
    """
    decision = (row.get("layers") or {}).get("decision") or {}
    status = decision.get("status")
    if status == "confirmed":
        return "confirmed"
    if status == "unconfirmed":
        return "removed"
    if status and status.startswith("cancelled"):
        # The camera spoke during the wait: nothing to remove, nothing led.
        return "unjudged"
    either = layer_votes(row)["either"]
    if either is None:
        return "unjudged"
    return "confirmed" if either else "removed"


RULES = (
    ("vision alone", ()),
    ("vision and a confirming look, CLIP or a plate box (the shipped rule)", ("either",)),
    ("vision and CLIP sees a vehicle", ("clip",)),
    ("vision and a plate box in the first looks", ("plate",)),
    ("vision and a vehicle sound rising (audio-armed vision)", ("audio",)),
    ("vision and CLIP and plate box", ("clip", "plate")),
    ("vision and CLIP and audio", ("clip", "audio")),
    ("vision and plate box and audio", ("plate", "audio")),
)


def layer_table(would_triggers) -> list[dict]:
    table = []
    for name, needs in RULES:
        line = {"rule": name}
        for light in LIGHTS:
            rows = [row for row in would_triggers if row.get("light") == light]
            judged = [row for row in rows
                      if all(layer_votes(row)[layer] is not None for layer in needs)]
            kept = [row for row in judged if all(layer_votes(row)[layer] for layer in needs)]
            false_all = [row for row in judged if row["verdict"] == "false"]
            true_all = [row for row in judged if row["verdict"] == "true"]
            false_kept = [row for row in kept if row["verdict"] == "false"]
            true_kept = [row for row in kept if row["verdict"] == "true"]
            leads = [row["lead_seconds"] for row in true_kept if row["lead_seconds"] is not None]
            line[light] = {
                "judged": len(judged), "unjudged": len(rows) - len(judged),
                "false_removed": len(false_all) - len(false_kept), "false_total": len(false_all),
                "true_lost": len(true_all) - len(true_kept), "true_total": len(true_all),
                "median_lead": percentile(leads, 0.5),
            }
        table.append(line)
    return table


def seconds_saved(row: dict):
    """Upper bound on what starting the sweep at the would-trigger saves to the first good read.

    The read that happened ``read_delay`` after the alarm could have happened
    at most ``lead`` sooner, and no sooner than ``MIN_READ_SECONDS`` after the
    would-trigger. It assumes the plate was already legible that much earlier,
    which a 192 px plate in the first second of visibility has been measured to
    be -- once. None when the passage had no good local read at all.
    """
    if row.get("first_read_at") is None or row.get("lead_seconds") is None:
        return None
    try:
        read_at = datetime.fromisoformat(row["first_read_at"]).timestamp()
        alarm_at = datetime.fromisoformat(row["camera_alarm_at"]).timestamp()
    except (TypeError, ValueError):
        return None
    lead = row["lead_seconds"]
    return round(max(0.0, min(lead, read_at - alarm_at + lead - MIN_READ_SECONDS)), 2)


def summarise(rows, *, hours=None, hours_source: str | None = None) -> dict:
    would = [row for row in rows if row["kind"] == KIND_WOULD_TRIGGER and row["verdict"] != "pending"]
    alarms = [row for row in rows if row["kind"] == KIND_CAMERA_ALARM and row["verdict"] != "pending"]
    summary = {"pending": sum(1 for row in rows if row["verdict"] == "pending"), "by_light": {}}
    for light in LIGHTS:
        mine = [row for row in would if row.get("light") == light]
        theirs = [row for row in alarms if row.get("light") == light]
        true = [row for row in mine if row["verdict"] == "true"]
        false = [row for row in mine if row["verdict"] == "false"]
        useful = [row["lead_seconds"] for row in true
                  if row["lead_seconds"] is not None and 0 <= row["lead_seconds"] <= USEFUL_LEAD_SECONDS]
        given = (hours or {}).get(light)
        span = given or observed_hours(rows, light)
        source = (hours_source or "given") if given else "rows"
        decided = {row["id"]: decision_of(row) for row in mine}
        false_removed = sum(1 for row in false if decided[row["id"]] == "removed")
        false_passed = sum(1 for row in false if decided[row["id"]] == "confirmed")
        false_unjudged = len(false) - false_removed - false_passed
        true_removed = sum(1 for row in true if decided[row["id"]] == "removed")
        saved = [value for value in (seconds_saved(row) for row in true) if value is not None]
        buckets: dict[str, int] = {}
        for row in false:
            name = bucket(row["features"])
            buckets[name] = buckets.get(name, 0) + 1
        summary["by_light"][light] = {
            "passages": len(theirs),
            "led": sum(1 for row in theirs if row["verdict"] == "led"),
            "late_lead": sum(1 for row in theirs if row["verdict"] == "late_lead"),
            "missed": sum(1 for row in theirs if row["verdict"] == "missed"),
            "missed_while_paused": sum(
                1 for row in theirs
                if row["verdict"] == "missed" and row.get("detector_state") != "armed"),
            "would_triggers": len(mine), "true": len(true), "false": len(false),
            "lead_median": percentile(useful, 0.5), "lead_p10": percentile(useful, 0.1),
            "lead_p90": percentile(useful, 0.9),
            "hours": round(span, 2), "hours_source": source,
            "false_per_hour": None if span <= 0 else round(len(false) / span, 2),
            # The confirmation layer's account: false triggers it removed,
            # false triggers that got past it (what `on` would have swept),
            # and true ones it cost.
            "false_removed": false_removed, "false_passed": false_passed,
            "false_unjudged": false_unjudged, "true_removed": true_removed,
            "false_passed_per_hour": None if span <= 0 else round(false_passed / span, 2),
            "false_buckets": buckets,
            "saved_median": percentile(saved, 0.5), "saved_known": len(saved),
        }
    summary["layers"] = layer_table(would)
    return summary


# -- audio, offline -----------------------------------------------------------

def segment_index(directory: Path) -> list[tuple[float, Path]]:
    index = []
    for path in sorted(Path(directory).glob("*.aac")):
        found = _SEGMENT.search(path.name)
        if found:
            start = datetime.strptime(found.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
            index.append((start.timestamp(), path))
    return index


def read_pcm(index, start: float, end: float, *, run=subprocess.run):
    """Signed 16-bit mono samples for ``[start, end)``, or None when no segment holds it."""
    for position, (segment_start, path) in enumerate(index):
        segment_end = index[position + 1][0] if position + 1 < len(index) else segment_start + 300.0
        if segment_start <= start and end <= segment_end + 1.0:
            try:
                answer = run(
                    ["ffmpeg", "-v", "error", "-ss", f"{start - segment_start:.2f}",
                     "-t", f"{end - start:.2f}", "-i", str(path),
                     "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1"],
                    capture_output=True, timeout=30,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            data = answer.stdout
            if not data:
                return None
            samples = array.array("h")
            samples.frombytes(data[:len(data) - len(data) % 2])
            return samples
    return None


def band_levels(samples) -> list[list[float]]:
    """dB per band, one row a second. numpy when there is one; the detector's own FFT when not."""
    seconds = len(samples) // SAMPLE_RATE
    rows = []
    try:
        import numpy as np
    except ImportError:
        np = None
    for second in range(seconds):
        chunk = samples[second * SAMPLE_RATE:(second + 1) * SAMPLE_RATE]
        if np is not None:
            values = np.asarray(chunk, dtype=np.float64) / 32768.0
            power = np.abs(np.fft.rfft(values * np.hanning(len(values)))) ** 2
            frequencies = np.fft.rfftfreq(len(values), 1.0 / SAMPLE_RATE)
            energy = [float(power[(frequencies >= low) & (frequencies < high)].sum())
                      for low, high in BANDS]
        else:
            from gate_controller.gate_audio_detect import FRAME_SAMPLES, _fft

            energy = [0.0] * len(BANDS)
            for start in range(0, len(chunk) - FRAME_SAMPLES, FRAME_SAMPLES * 4):
                frame = [complex(chunk[start + i] / 32768.0, 0) for i in range(FRAME_SAMPLES)]
                power = [abs(c) ** 2 for c in _fft(frame)[:FRAME_SAMPLES // 2]]
                for band, (low, high) in enumerate(BANDS):
                    a = int(low * FRAME_SAMPLES / SAMPLE_RATE)
                    b = max(a + 1, int(high * FRAME_SAMPLES / SAMPLE_RATE))
                    energy[band] += sum(power[a:b])
        rows.append([10 * math.log10(value) if value > 1e-12 else -120.0 for value in energy])
    return rows


def vehicle_onset(levels) -> dict:
    """Was a vehicle-like sound already rising before the last second of ``levels``?

    A rise of ``ONSET_RISE_DB`` in the 150-1200 Hz bands over the quiet first
    half of the window, held for ``ONSET_HOLD_SECONDS`` and still there at the
    end. Wind lives under 150 Hz and rain is a broadband hiss that lifts the
    floor rather than rising over it, so a rise that is mostly in those bands
    is reported as what it is and is not an onset. A heuristic: nothing here
    has been fitted to labelled passages yet.
    """
    if len(levels) < 12:
        return {"status": "too_short"}
    half = len(levels) // 2

    def mid(row):
        return max(row[band] for band in VEHICLE_BANDS)

    floor = percentile([mid(row) for row in levels[:half]], 0.5)
    wind_floor = percentile([row[WIND_BAND] for row in levels[:half]], 0.5)
    hiss_floor = percentile([row[HISS_BAND] for row in levels[:half]], 0.5)
    onset = None
    for index in range(len(levels) - 1, half - 1, -1):
        if mid(levels[index]) >= floor + ONSET_RISE_DB:
            onset = index
        else:
            break
    tail = levels[-3:]
    rise = percentile([mid(row) for row in tail], 0.5) - floor
    wind_rise = percentile([row[WIND_BAND] for row in tail], 0.5) - wind_floor
    hiss_rise = percentile([row[HISS_BAND] for row in tail], 0.5) - hiss_floor
    answer = {
        "status": "ok", "rise_db": round(rise, 1), "wind_rise_db": round(wind_rise, 1),
        "hiss_rise_db": round(hiss_rise, 1), "onset_lead_seconds": None, "masked_by": None,
    }
    if onset is None or len(levels) - onset < ONSET_HOLD_SECONDS:
        return answer
    if wind_rise > rise:
        answer["masked_by"] = "wind"
    elif hiss_rise > rise:
        answer["masked_by"] = "hiss"
    else:
        answer["onset_lead_seconds"] = float(len(levels) - onset)
    return answer


def annotate_audio(rows, index, *, run=subprocess.run) -> None:
    for row in rows:
        samples = read_pcm(index, row["at_epoch"] - ONSET_WINDOW_SECONDS, row["at_epoch"], run=run)
        row["audio"] = (
            {"status": "no_segment"} if samples is None else vehicle_onset(band_levels(samples))
        )


def annotate_uncommanded(rows, events_database: Path) -> None:
    """Did the gate's motor run un-commanded just before? Then somebody is leaving."""
    try:
        with closing(sqlite3.connect(f"file:{events_database}?mode=ro", uri=True)) as connection:
            movements = [
                datetime.fromisoformat(started).timestamp()
                for (started,) in connection.execute(
                    "SELECT started_at FROM gate_movements WHERE uncommanded = 1")
            ]
    except (sqlite3.Error, ValueError):
        return
    for row in rows:
        row["uncommanded_gate_before"] = any(
            0.0 <= row["at_epoch"] - started <= UNCOMMANDED_BEFORE_SECONDS for started in movements)


def dump_audio_features(rows, index, output: Path, *, negatives: int = 200, seed: int = 1,
                        run=subprocess.run) -> int:
    """Band levels of the ten seconds before each camera alarm, and of quiet moments."""
    alarms = [row["at_epoch"] for row in rows if row["kind"] == KIND_CAMERA_ALARM]
    busy = [row["at_epoch"] for row in rows]
    written = 0
    rng = random.Random(seed)
    with open(output, "w", encoding="utf-8") as handle:
        def write(at, label):
            nonlocal written
            samples = read_pcm(index, at - 10.0, at, run=run)
            if samples is None:
                return
            handle.write(json.dumps({
                "at": datetime.fromtimestamp(at, timezone.utc).isoformat(), "label": label,
                "bands_hz": BANDS, "levels_db": band_levels(samples),
            }) + "\n")
            written += 1

        for at in alarms:
            write(at, "camera_vehicle_alarm")
        if index:
            first, last = index[0][0] + 15.0, index[-1][0] + 280.0
            for _ in range(negatives * 5):
                if written >= len(alarms) + negatives or last <= first:
                    break
                at = rng.uniform(first, last)
                if all(abs(at - other) > 120.0 for other in busy):
                    write(at, "nothing_recorded")
    return written


# -- printing -------------------------------------------------------------------

def _seconds(value) -> str:
    return "-" if value is None else f"{value:.1f} s"


def render(summary: dict) -> str:
    lines = []
    for light in LIGHTS:
        block = summary["by_light"][light]
        source = {"journal": "from the journal's status lines", "given": "given",
                  "rows": "from the rows alone: quiet time is not counted, so the rates err high"}
        lines += [
            f"== {light.upper()} ({block['hours']} h watched, {source.get(block['hours_source'], block['hours_source'])})",
            f"passages (camera vehicle alarms): {block['passages']}",
            f"  led by a would-trigger within {USEFUL_LEAD_SECONDS:g} s: {block['led']}"
            f"   led too early to help: {block['late_lead']}   MISSED: {block['missed']}"
            f" (of which {block['missed_while_paused']} while paused or warming)",
            f"  lead over the camera: median {_seconds(block['lead_median'])}"
            f"  p10 {_seconds(block['lead_p10'])}  p90 {_seconds(block['lead_p90'])}",
            f"would-triggers: {block['would_triggers']}  true {block['true']}  FALSE {block['false']}"
            f"  = {block['false_per_hour']} false an hour (the vision rule alone)",
            f"  false removed by the confirmation layer: {block['false_removed']}"
            f"   false that PASSED it: {block['false_passed']}"
            f" = {block['false_passed_per_hour']} an hour"
            f"   unjudged: {block['false_unjudged']}"
            f"   true it cost: {block['true_removed']}",
        ]
        for name, count in sorted(block["false_buckets"].items(), key=lambda item: -item[1]):
            lines.append(f"    {count:4d}  {name}")
        lines.append(
            f"  seconds saved to the first good local read (upper bound): median "
            f"{_seconds(block['saved_median'])} over {block['saved_known']} passages with one")
        lines.append("")
    lines.append("== LAYERS: false triggers removed / true triggers lost / median lead kept")
    for line in summary["layers"]:
        cells = []
        for light in LIGHTS:
            block = line[light]
            cells.append(
                f"{light}: -{block['false_removed']}/{block['false_total']} false, "
                f"-{block['true_lost']}/{block['true_total']} true, lead "
                f"{_seconds(block['median_lead'])} ({block['unjudged']} unjudged)")
        lines.append(f"{line['rule']}\n    " + "\n    ".join(cells))
    if summary["pending"]:
        lines.append(f"\n{summary['pending']} rows are under a minute old and not yet settled.")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", type=Path,
                        default=Path("/var/lib/gate-controller/early-trigger.db"))
    parser.add_argument("--events-database", type=Path,
                        help="the controller's database, for first reads and un-commanded gate runs")
    parser.add_argument("--audio-segments", type=Path, help="the recorder's AAC segment directory")
    parser.add_argument("--dump-audio-features", type=Path,
                        help="write band levels before each alarm, and of quiet moments, as JSONL")
    parser.add_argument("--since", help="ISO time; rows before it are ignored")
    parser.add_argument("--journal", type=Path,
                        help="the controller's journal text; hours watched come from its status lines")
    parser.add_argument("--fps", type=float,
                        help="the sampling rate, when the journal's stage=configured line is missing")
    parser.add_argument("--hours-day", type=float, help="hours watched by day, overriding the journal")
    parser.add_argument("--hours-night", type=float, help="hours watched by night, overriding the journal")
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args(argv)
    if not arguments.database.exists():
        print(f"no record at {arguments.database}", file=sys.stderr)
        return 2
    # A private copy is settled, so reading the report never writes to the Pi's file.
    with closing(sqlite3.connect(":memory:")) as connection:
        with closing(sqlite3.connect(f"file:{arguments.database}?mode=ro", uri=True)) as source:
            source.backup(connection)
        ensure_schema(connection)
        correlate(connection, datetime.now(timezone.utc).timestamp(),
                  events_database=arguments.events_database)
        rows = load_rows(connection)
    since = None
    if arguments.since:
        floor = datetime.fromisoformat(arguments.since)
        if floor.tzinfo is None:
            floor = floor.replace(tzinfo=timezone.utc)
        since = floor.timestamp()
        rows = [row for row in rows if row["at_epoch"] >= since]
    hours = {"day": arguments.hours_day, "night": arguments.hours_night}
    hours_source = "given"
    if arguments.journal:
        with open(arguments.journal, encoding="utf-8", errors="replace") as handle:
            watched = journal_hours(handle, fps=arguments.fps, since=since)
        for light in LIGHTS:
            if hours[light] is None and watched[light] > 0:
                hours[light] = watched[light]
                hours_source = "journal"
    if arguments.events_database:
        annotate_uncommanded(rows, arguments.events_database)
    index = segment_index(arguments.audio_segments) if arguments.audio_segments else []
    if index:
        annotate_audio(rows, index)
    summary = summarise(rows, hours=hours, hours_source=hours_source)
    if arguments.events_database:
        summary["uncommanded_gate_before"] = {
            "would_triggers": sum(1 for row in rows if row["kind"] == KIND_WOULD_TRIGGER
                                  and row.get("uncommanded_gate_before")),
            "camera_alarms": sum(1 for row in rows if row["kind"] == KIND_CAMERA_ALARM
                                 and row.get("uncommanded_gate_before")),
        }
    if arguments.dump_audio_features and index:
        summary["audio_features_written"] = dump_audio_features(
            rows, index, arguments.dump_audio_features)
    if arguments.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(render(summary))
        if "uncommanded_gate_before" in summary:
            print(f"\ngate heard running un-commanded in the {UNCOMMANDED_BEFORE_SECONDS:g} s before: "
                  f"{summary['uncommanded_gate_before']}")
        if not index:
            print("\n(no --audio-segments: the audio layer is unjudged everywhere above)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
