"""The report that decides whether the early trigger may be switched `on`."""
import array
import importlib.util
import json
import math
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path

from gate_controller.early_trigger import EarlyTriggerStore

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "early_trigger_report.py"
spec = importlib.util.spec_from_file_location("early_trigger_report", SCRIPT)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)

BASE = 1_790_000_000.0


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "early-trigger.db"
        self.store = EarlyTriggerStore(self.path)

    def would(self, at, light="day", features=None, layers=None, sweep=None):
        row = self.store.record(kind="would_trigger", source="vision", at_epoch=at, mode="shadow",
                                light=light, detector_state="armed",
                                features={"light": light, **(features or {})})
        self.store.annotate(row, layers=layers or {}, **({"sweep": sweep} if sweep else {}))
        return row

    def alarm(self, at, light="day", state="armed"):
        return self.store.record(kind="camera_alarm", source="camera", at_epoch=at, mode="shadow",
                                 light=light, detector_state=state, features={"light": light})

    def summary(self):
        self.store.correlate(BASE + 100_000)
        with closing(sqlite3.connect(str(self.path))) as connection:
            return report.summarise(report.load_rows(connection))

    def test_leads_misses_and_false_triggers_an_hour_split_by_day_and_night(self):
        for index, lead in enumerate((1.0, 2.0, 3.0)):
            self.would(BASE + index * 600, layers={"clip": {"status": "ok", "empty": 0.01}})
            self.alarm(BASE + index * 600 + lead)
        self.alarm(BASE + 2400)                                   # missed by day
        self.alarm(BASE + 2500, state="paused")                   # missed, but we were not looking
        self.would(BASE + 3000, features={"scatter": 0.09},
                   layers={"clip": {"status": "ok", "empty": 0.93}})   # false by day, foliage
        self.would(BASE + 3600)                                   # closes the hour of day record
        self.would(BASE + 7200, light="night", features={"shift": 44, "blob_cells": 9})
        self.would(BASE + 7800, light="night", features={"track_dx": 0.6, "blob_cells": 6})
        self.would(BASE + 10800, light="night", features={"blob_cells": 2})
        summary = self.summary()
        day, night = summary["by_light"]["day"], summary["by_light"]["night"]
        self.assertEqual((day["passages"], day["led"], day["missed"]), (5, 3, 2))
        self.assertEqual(day["missed_while_paused"], 1)
        self.assertAlmostEqual(day["lead_median"], 2.0)
        self.assertAlmostEqual(day["lead_p10"], 1.2)
        self.assertAlmostEqual(day["lead_p90"], 2.8)
        self.assertEqual((day["true"], day["false"]), (3, 2))
        self.assertAlmostEqual(day["hours"], 2.0, places=1)
        self.assertAlmostEqual(day["false_per_hour"], 1.0, places=1)
        self.assertEqual(night["false"], 3)
        self.assertAlmostEqual(night["false_per_hour"], 3.0, places=1)
        self.assertEqual(sorted(night["false_buckets"]), [
            "night: a small bright source (distant lamp, rain in a beam)",
            "night: a source moving fast across the patch (passing headlights)",
            "night: whole patch lit (floodlight, or a beam flooding the lens)",
        ])
        self.assertIn("day: change scattered over the patch (foliage, rain, dappled shade)",
                      day["false_buckets"])

    def test_the_layer_table_says_what_each_layer_removes_and_what_it_costs(self):
        vehicle = {"clip": {"status": "ok", "empty": 0.02},
                   "plate_look": {"status": "ok", "plate_box": True}}
        empty = {"clip": {"status": "ok", "empty": 0.9},
                 "plate_look": {"status": "ok", "plate_box": False}}
        no_plate_yet = {"clip": {"status": "ok", "empty": 0.1},
                        "plate_look": {"status": "ok", "plate_box": False}}
        self.would(BASE, layers=vehicle)
        self.alarm(BASE + 2)
        self.would(BASE + 600, layers=no_plate_yet)
        self.alarm(BASE + 603)
        self.would(BASE + 1200, layers=empty)
        self.would(BASE + 1800, layers=empty)
        self.would(BASE + 2400, layers={"clip": {"status": "skipped_busy"}})
        table = {line["rule"]: line["day"] for line in self.summary()["layers"]}
        alone = table["vision alone"]
        self.assertEqual((alone["false_total"], alone["true_total"], alone["false_removed"]), (3, 2, 0))
        clip = table["vision and CLIP sees a vehicle"]
        self.assertEqual((clip["false_removed"], clip["false_total"], clip["true_lost"]), (2, 2, 0))
        self.assertEqual(clip["unjudged"], 1, "a skipped look is not an opinion")
        plate = table["vision and a plate box in the first looks"]
        self.assertEqual((plate["false_removed"], plate["true_lost"]), (2, 1))
        self.assertAlmostEqual(plate["median_lead"], 2.0)
        self.assertEqual(table["vision and a vehicle sound rising (audio-armed vision)"]["judged"], 0)

    def test_in_on_mode_the_early_sweeps_own_reads_are_the_plate_look(self):
        self.would(BASE, sweep={"plate_reads": 3, "reason": "opened"})
        self.alarm(BASE + 1)
        self.would(BASE + 900, sweep={"plate_reads": 0, "reason": "early_abort"})
        table = {line["rule"]: line["day"] for line in self.summary()["layers"]}
        plate = table["vision and a plate box in the first looks"]
        self.assertEqual((plate["false_removed"], plate["true_lost"], plate["judged"]), (1, 0, 2))

    def test_seconds_saved_is_bounded_by_the_lead_and_by_how_fast_a_read_can_be(self):
        row = {"lead_seconds": 2.0, "camera_alarm_at": "2026-09-21T10:00:02+00:00",
               "first_read_at": "2026-09-21T10:00:02.800+00:00"}
        self.assertAlmostEqual(report.seconds_saved(row), 1.6)   # 0.8 + 2.0 - 1.2
        row["first_read_at"] = "2026-09-21T10:00:09+00:00"
        self.assertAlmostEqual(report.seconds_saved(row), 2.0)   # never more than the lead
        self.assertIsNone(report.seconds_saved({"lead_seconds": 2.0, "first_read_at": None}))

    def test_a_vehicle_sound_rising_is_told_from_wind_and_from_hiss(self):
        quiet = [-70.0, -60.0, -62.0, -66.0, -68.0]

        def window(rise_mid=0.0, rise_wind=0.0, rise_hiss=0.0, seconds=8):
            rows = [list(quiet) for _ in range(30 - seconds)]
            for _ in range(seconds):
                rows.append([quiet[0] + rise_wind, quiet[1] + rise_mid, quiet[2] + rise_mid,
                             quiet[3], quiet[4] + rise_hiss])
            return rows

        self.assertEqual(report.vehicle_onset(window(rise_mid=12))["onset_lead_seconds"], 8.0)
        self.assertIsNone(report.vehicle_onset(window())["onset_lead_seconds"])
        gust = report.vehicle_onset(window(rise_mid=8, rise_wind=25))
        self.assertEqual((gust["onset_lead_seconds"], gust["masked_by"]), (None, "wind"))
        rain = report.vehicle_onset(window(rise_mid=7, rise_hiss=15))
        self.assertEqual((rain["onset_lead_seconds"], rain["masked_by"]), (None, "hiss"))
        self.assertIsNone(report.vehicle_onset(window(rise_mid=12, seconds=1))["onset_lead_seconds"])

    def test_band_levels_find_a_tone_in_its_band(self):
        samples = array.array("h", (
            int(8000 * math.sin(2 * math.pi * 300 * index / report.SAMPLE_RATE))
            for index in range(report.SAMPLE_RATE * 2)
        ))
        levels = report.band_levels(samples)
        self.assertEqual(len(levels), 2)
        self.assertEqual(max(range(5), key=lambda band: levels[0][band]), 1, "300 Hz is band 150-400")

    def test_segments_are_found_by_the_time_in_their_names_and_cut_with_ffmpeg(self):
        directory = Path(self.directory.name) / "segments"
        directory.mkdir()
        (directory / "gate-20260921T100000Z.aac").write_bytes(b"x")
        (directory / "gate-20260921T100500Z.aac").write_bytes(b"x")
        index = report.segment_index(directory)
        self.assertEqual(len(index), 2)
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return type("Done", (), {"stdout": array.array("h", [0] * 16000).tobytes()})()

        samples = report.read_pcm(index, index[0][0] + 100, index[0][0] + 130, run=run)
        self.assertEqual(len(samples), 16000)
        self.assertIn("100.00", calls[0])
        self.assertIsNone(report.read_pcm(index, index[0][0] - 500, index[0][0] - 470, run=run))

    def test_it_prints_and_never_writes_to_the_record_it_reads(self):
        an_hour_ago = time.time() - 3600.0
        self.would(an_hour_ago)
        self.alarm(an_hour_ago + 1.5)
        before = self.path.read_bytes()
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(report.main(["--database", str(self.path)]), 0)
        self.assertEqual(self.path.read_bytes(), before)
        text = output.getvalue()
        self.assertIn("== DAY", text)
        self.assertIn("== NIGHT", text)
        self.assertIn("== LAYERS", text)
        self.assertIn("lead over the camera: median 1.5 s", text)
        with redirect_stdout(StringIO()) as raw:
            report.main(["--database", str(self.path), "--json"])
        self.assertEqual(json.loads(raw.getvalue())["by_light"]["day"]["led"], 1)


if __name__ == "__main__":
    unittest.main()
