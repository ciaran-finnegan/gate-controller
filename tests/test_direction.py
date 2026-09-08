"""The shadow vehicle-direction estimator, against the labelled passages.

The replay fixture,
``tests/fixtures/vehicle-direction-2026-09-08-passages.json``, is the
42-passage table from
``gate-controller-data/analysis/vehicle-direction-2026-09-08.md`` with the
plate strings and D1 event ids removed (JSON rather than the original CSV
because ``.gitignore`` refuses ``*.csv``, and that rule is what keeps
authorised-plate lists out of this repository). It carries, per hand-labelled
passage, the number of boxed frames, the span they cover, and the
least-squares slope of ``log(box width)`` over them, which is exactly what
the estimator computes. The raw per-frame boxes live in R2 and D1, not in the
CSV and not in this repository, so the replay reconstructs each passage's
width series log-linearly from its measured slope, span and frame count: the
fit recovers the recorded slope, and the gate and the thresholds then meet
the real measured values rather than invented ones.
"""

import dataclasses
import inspect
import json
import logging
import math
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from gate_controller.direction import (
    DirectionConfig,
    DirectionConfigError,
    DirectionEstimate,
    DirectionTracker,
    MAX_SAMPLES,
    MAX_TRACKED_EVENTS,
    METHOD_BOX_WIDTH,
    METHOD_NONE,
    PASSAGE_TTL_SECONDS,
    SOURCE_LOCAL_PLATE_BOX,
    SOURCE_PLATE_BOX,
    SOURCE_VEHICLE_BOX,
    VERDICT_ENTERING,
    VERDICT_EXITING,
    VERDICT_STATIONARY,
    VERDICT_UNKNOWN,
    estimate_direction,
    load_direction_config,
    passage_key,
)
import gate_controller.__main__
import gate_controller.processor as processor_module
import gate_controller.telemetry_export as telemetry_export
from gate_controller.direction import PASSAGE_SESSION_GAP_SECONDS
from gate_controller.models import PlateObservation, RelayResult
from gate_controller.ocr import PlateRecognizerClient
from gate_controller.processor import GateProcessor
from gate_controller.store import LocalStore
from gate_controller.telemetry import TriggerTelemetry


PASSAGES = Path(__file__).with_name("fixtures") / (
    "vehicle-direction-2026-09-08-passages.json"
)


def labelled_passages():
    document = json.loads(PASSAGES.read_text(encoding="utf-8"))
    return [
        {
            "pid": row["pid"],
            "label": row["label"],
            "note": row["note"],
            "frames": row["boxed_frames"],
            "span": row["span_s"],
            "slope": row["logw_slope_per_s"],
            "gated": row["gated"],
        }
        for row in document["passages"]
    ]


def replay_samples(passage, *, start_width: float = 0.2):
    """A width series with the passage's measured slope, span and frames."""
    frames = passage["frames"]
    span = passage["span"]
    step = span / (frames - 1) if frames > 1 else 0.0
    return [
        (index * step, start_width * math.exp(passage["slope"] * index * step))
        for index in range(frames)
    ]


class ReplayOfTheLabelledPassagesTests(unittest.TestCase):
    """The operating point, checked against the passages it was chosen on."""

    def setUp(self):
        self.passages = labelled_passages()
        self.config = DirectionConfig()

    def verdicts(self, *, config=None, only_gated: bool):
        config = config or self.config
        results = {}
        for passage in self.passages:
            if only_gated and not passage["gated"]:
                continue
            results[passage["pid"]] = (
                passage["label"],
                estimate_direction(replay_samples(passage), config).verdict,
            )
        return results

    def test_the_fixture_is_the_analysis_sample_it_claims_to_be(self):
        labels = [passage["label"] for passage in self.passages]

        self.assertEqual(len(self.passages), 42)
        self.assertEqual(labels.count("entering"), 22)
        self.assertEqual(labels.count("exiting"), 12)
        self.assertEqual(labels.count("stationary"), 4)
        self.assertEqual(labels.count("unknown"), 4)
        self.assertEqual(sum(1 for row in self.passages if row["gated"]), 28)

    def test_the_fit_recovers_each_passage_s_measured_slope(self):
        for passage in self.passages:
            if passage["frames"] < 2 or passage["span"] <= 0:
                continue
            with self.subTest(passage=passage["pid"]):
                permissive = DirectionConfig(min_frames=3, min_span_seconds=2.0)
                samples = replay_samples(passage)
                fitted = estimate_direction(samples, permissive).slope
                if fitted is None:  # below the gate: deliberately not fitted
                    continue
                self.assertAlmostEqual(fitted, passage["slope"], places=4)

    def test_defaults_catch_every_gated_exit_with_no_false_exit_on_an_entry(self):
        """7/7 exits and 0/15 false exits, the analysis's headline result."""
        results = self.verdicts(only_gated=True)
        exits = [pid for pid, (label, _) in results.items() if label == "exiting"]
        entries = [pid for pid, (label, _) in results.items() if label == "entering"]

        self.assertEqual(len(exits), 7)
        self.assertEqual(len(entries), 15)
        self.assertEqual(
            [pid for pid in exits if results[pid][1] != VERDICT_EXITING], [],
            "every gated exit must be called exiting at the shipped defaults",
        )
        self.assertEqual(
            [pid for pid in entries if results[pid][1] == VERDICT_EXITING], [],
            "a false exit is a household member locked out: the bar is zero",
        )

    def test_every_gated_entering_passage_is_called_entering(self):
        results = self.verdicts(only_gated=True)
        entries = {
            pid: verdict for pid, (label, verdict) in results.items()
            if label == "entering"
        }

        self.assertEqual(set(entries.values()), {VERDICT_ENTERING})

    def test_the_gate_is_what_removes_the_one_measured_false_exit(self):
        """Ungated the same rule calls an entering car an exit: 1/22, P241.

        The passage is the Audi driving *in* past the camera 58 s after it was
        granted, seen on two frames 0.8 s apart. The span gate, not the
        threshold, is what removes it, which is why the gate may be tightened
        by configuration and never loosened.
        """
        ungated = DirectionConfig(min_frames=2, min_span_seconds=0.0)
        called = {
            passage["pid"]: (
                passage["label"],
                estimate_direction(replay_samples(passage), ungated).verdict,
            )
            for passage in self.passages
        }
        false_exits = [
            pid for pid, (label, verdict) in called.items()
            if label == "entering" and verdict == VERDICT_EXITING
        ]
        caught = [
            pid for pid, (label, verdict) in called.items()
            if label == "exiting" and verdict == VERDICT_EXITING
        ]

        self.assertEqual(false_exits, ["P241"])
        self.assertEqual(len(caught), 9, "9/12 exits ungated, as measured")

        gated = {
            passage["pid"]: estimate_direction(
                replay_samples(passage), self.config
            ).verdict
            for passage in self.passages
        }
        self.assertNotEqual(gated["P241"], VERDICT_EXITING)

    def test_short_bursts_are_discarded_rather_than_fitted(self):
        """The eight pre-presence-session bursts produce nonsense slopes."""
        bursts = [
            passage for passage in self.passages
            if passage["span"] <= 1.0 and passage["frames"] == 2
        ]

        self.assertTrue(bursts)
        for passage in bursts:
            with self.subTest(passage=passage["pid"]):
                estimate = estimate_direction(replay_samples(passage), self.config)
                self.assertEqual(estimate.verdict, VERDICT_UNKNOWN)
                self.assertIsNone(estimate.slope)
                self.assertEqual(estimate.method, METHOD_BOX_WIDTH)

    def test_a_parked_vehicle_reads_as_stationary_not_as_a_direction(self):
        parked = {
            passage["pid"]: estimate_direction(
                replay_samples(passage), self.config
            ).verdict
            for passage in self.passages
            if passage["label"] == "stationary" and passage["gated"]
        }

        # P255 is the DPD van whose courier occludes the frame; the analysis
        # records it as one of the two stationary/unknown passages that read
        # as an exit at this threshold. It is not an entering car, so it costs
        # nobody an opening, and it is reported rather than hidden.
        self.assertEqual(parked["P162"], VERDICT_STATIONARY)
        self.assertEqual(parked["P165"], VERDICT_STATIONARY)
        self.assertEqual(parked["P243"], VERDICT_STATIONARY)
        self.assertEqual(parked["P255"], VERDICT_EXITING)


class EstimatorTests(unittest.TestCase):
    def test_the_gate_needs_three_frames_and_two_seconds(self):
        config = DirectionConfig()
        two_frames = [(0.0, 0.4), (4.0, 0.1)]
        fast = [(0.0, 0.4), (0.5, 0.2), (1.0, 0.1)]

        for samples in (two_frames, fast):
            with self.subTest(samples=samples):
                estimate = estimate_direction(samples, config)
                self.assertEqual(estimate.verdict, VERDICT_UNKNOWN)
                self.assertEqual(estimate.method, METHOD_BOX_WIDTH)
                self.assertIsNone(estimate.slope)
                self.assertIsNone(estimate.score)

    def test_no_samples_at_all_report_that_nothing_was_measured(self):
        estimate = estimate_direction((), DirectionConfig())

        self.assertEqual(estimate.verdict, VERDICT_UNKNOWN)
        self.assertEqual(estimate.method, METHOD_NONE)
        self.assertEqual(estimate.frames, 0)
        self.assertEqual(estimate.span_ms, 0)

    def test_a_shrinking_box_is_an_exit_and_a_growing_one_an_entry(self):
        shrinking = [(0.0, 0.40), (1.5, 0.20), (3.0, 0.10)]
        growing = [(0.0, 0.10), (1.5, 0.20), (3.0, 0.40)]

        self.assertEqual(
            estimate_direction(shrinking, DirectionConfig()).verdict, VERDICT_EXITING
        )
        self.assertEqual(
            estimate_direction(growing, DirectionConfig()).verdict, VERDICT_ENTERING
        )

    def test_a_flat_width_over_a_long_look_is_stationary(self):
        flat = [(0.0, 0.30), (2.0, 0.301), (4.0, 0.299), (6.0, 0.30)]

        self.assertEqual(
            estimate_direction(flat, DirectionConfig()).verdict, VERDICT_STATIONARY
        )

    def test_a_drift_inside_the_band_over_a_long_look_is_unknown(self):
        """In-band, but the width moved far too much to call it parked."""
        drifting = [(0.0, 0.30), (10.0, 0.24), (20.0, 0.19), (30.0, 0.15)]

        estimate = estimate_direction(drifting, DirectionConfig())

        self.assertEqual(estimate.verdict, VERDICT_UNKNOWN)
        self.assertEqual(estimate.method, METHOD_BOX_WIDTH)
        self.assertIsNotNone(estimate.slope)
        self.assertIsNone(estimate.score)

    def test_the_score_rises_with_the_margin_and_with_the_frames(self):
        config = DirectionConfig()

        def score(slope, frames, span=6.0):
            step = span / (frames - 1)
            samples = [
                (index * step, 0.3 * math.exp(slope * index * step))
                for index in range(frames)
            ]
            return estimate_direction(samples, config).score

        weak, strong = score(-0.10, 4), score(-0.50, 4)
        few, many = score(-0.50, 3), score(-0.50, 8)

        self.assertLess(weak, strong)
        self.assertLess(few, many)
        for value in (weak, strong, few, many):
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

    def test_a_verdict_sitting_on_its_threshold_scores_zero(self):
        config = DirectionConfig()
        span, frames = 6.0, 4
        step = span / (frames - 1)
        just_past = config.exit_slope - 1e-6
        samples = [
            (index * step, 0.3 * math.exp(just_past * index * step))
            for index in range(frames)
        ]

        estimate = estimate_direction(samples, config)

        self.assertEqual(estimate.verdict, VERDICT_EXITING)
        self.assertEqual(estimate.score, 0.0)

    def test_rubbish_samples_are_dropped_and_never_raise(self):
        samples = [
            (0.0, 0.4), ("no", 0.2), (1.0, None), (2.0, 0.0), (3.0, -1.0),
            (float("nan"), 0.1), (2.5, float("inf")), (4.0, 0.1), (2.0, 0.2),
        ]

        estimate = estimate_direction(samples, DirectionConfig())

        self.assertEqual(estimate.frames, 3)
        self.assertEqual(estimate.verdict, VERDICT_EXITING)

    def test_samples_are_ordered_before_the_fit(self):
        ordered = [(0.0, 0.10), (2.0, 0.20), (4.0, 0.40)]
        shuffled = [ordered[2], ordered[0], ordered[1]]

        self.assertEqual(
            estimate_direction(shuffled, DirectionConfig()).slope,
            estimate_direction(ordered, DirectionConfig()).slope,
        )

    def test_a_series_with_no_elapsed_time_is_not_fitted(self):
        estimate = estimate_direction(
            [(4.0, 0.1), (4.0, 0.2), (4.0, 0.3)], DirectionConfig(min_span_seconds=2.0)
        )

        self.assertEqual(estimate.verdict, VERDICT_UNKNOWN)
        self.assertIsNone(estimate.slope)


class ConfigurationTests(unittest.TestCase):
    def test_defaults_are_the_measured_operating_point(self):
        config = load_direction_config({})

        self.assertTrue(config.enabled)
        self.assertEqual(config.exit_slope, -0.06)
        self.assertEqual(config.enter_slope, 0.01)
        self.assertEqual(config.min_frames, 3)
        self.assertEqual(config.min_span_seconds, 2.0)
        self.assertEqual(config.min_brightness, 0.20)

    def test_the_thresholds_are_tunable(self):
        config = load_direction_config({
            "GATE_DIRECTION_EXIT_SLOPE": "-0.20",
            "GATE_DIRECTION_ENTER_SLOPE": "0.005",
            "GATE_DIRECTION_MIN_FRAMES": "4",
            "GATE_DIRECTION_MIN_SPAN_SECONDS": "3",
            "GATE_DIRECTION_MIN_BRIGHTNESS": "0.3",
            "GATE_DIRECTION_ENABLED": "false",
        })

        self.assertEqual(config.exit_slope, -0.20)
        self.assertEqual(config.enter_slope, 0.005)
        self.assertEqual(config.min_frames, 4)
        self.assertEqual(config.min_span_seconds, 3.0)
        self.assertEqual(config.min_brightness, 0.3)
        self.assertFalse(config.enabled)

    def test_the_gate_may_be_tightened_and_never_loosened(self):
        for environment in (
            {"GATE_DIRECTION_MIN_FRAMES": "2"},
            {"GATE_DIRECTION_MIN_SPAN_SECONDS": "1.0"},
        ):
            with self.subTest(environment=environment):
                with self.assertRaises(DirectionConfigError):
                    load_direction_config(environment)

    def test_only_a_loosened_gate_stops_the_controller_starting(self):
        """Everything else is a journal line and a default, not an abort.

        A mistyped knob on a shadow signal used to raise out of `main`, after
        the local recogniser's threads had started and the store had recovered
        its interrupted actuations. The gate is a safety property and stays
        fatal; nothing else here is worth a gate that will not open.
        """
        defaults = DirectionConfig()
        tolerated = (
            ({"GATE_DIRECTION_ENABLED": "ture"}, "enabled", defaults.enabled),
            ({"GATE_DIRECTION_ENABLED": "maybe"}, "enabled", defaults.enabled),
            ({"GATE_DIRECTION_EXIT_SLOPE": "-0,06"}, "exit_slope", defaults.exit_slope),
            ({"GATE_DIRECTION_EXIT_SLOPE": "nonsense"}, "exit_slope", defaults.exit_slope),
            ({"GATE_DIRECTION_EXIT_SLOPE": "nan"}, "exit_slope", defaults.exit_slope),
            ({"GATE_DIRECTION_EXIT_SLOPE": "0.05"}, "exit_slope", defaults.exit_slope),
            ({"GATE_DIRECTION_EXIT_SLOPE": "-50"}, "exit_slope", defaults.exit_slope),
            ({"GATE_DIRECTION_ENTER_SLOPE": "-0.05"}, "enter_slope", defaults.enter_slope),
            ({"GATE_DIRECTION_ENTER_SLOPE": "50"}, "enter_slope", defaults.enter_slope),
            ({"GATE_DIRECTION_MIN_BRIGHTNESS": "20"}, "min_brightness",
             defaults.min_brightness),
            ({"GATE_DIRECTION_MIN_BRIGHTNESS": "1.5"}, "min_brightness",
             defaults.min_brightness),
            ({"GATE_DIRECTION_MIN_FRAMES": "3.0"}, "min_frames", defaults.min_frames),
            ({"GATE_DIRECTION_MIN_FRAMES": "many"}, "min_frames", defaults.min_frames),
            ({"GATE_DIRECTION_MIN_FRAMES": "99"}, "min_frames", defaults.min_frames),
            ({"GATE_DIRECTION_MIN_SPAN_SECONDS": "soon"}, "min_span_seconds",
             defaults.min_span_seconds),
            ({"GATE_DIRECTION_MIN_SPAN_SECONDS": "99999"}, "min_span_seconds",
             defaults.min_span_seconds),
        )
        for environment, field, expected in tolerated:
            with self.subTest(environment=environment):
                with self.assertLogs("gate_controller.direction", "WARNING") as logs:
                    config = load_direction_config(environment)
                self.assertEqual(getattr(config, field), expected)
                key = next(iter(environment))
                self.assertIn(
                    f"gate_direction key={key} status=rejected using=",
                    "\n".join(logs.output),
                )

    def test_a_band_that_would_invert_cannot_survive_the_two_ranges(self):
        """Whatever is set, the accepted band still straddles zero."""
        with self.assertLogs("gate_controller.direction", "WARNING"):
            config = load_direction_config({
                "GATE_DIRECTION_EXIT_SLOPE": "0.05",
                "GATE_DIRECTION_ENTER_SLOPE": "-0.02",
            })

        self.assertLess(config.exit_slope, 0)
        self.assertGreater(config.enter_slope, 0)
        self.assertLess(config.exit_slope, config.enter_slope)

    def test_the_direction_config_is_read_before_any_hardware_or_thread(self):
        """`main` must reach its one fatal setting before it commits to anything.

        Asserted on the source because the alternative is booting a relay: the
        refusal used to sit below `store.recover_interrupted_actuations()` and
        `local_recognizer.start()`, so a mistyped gate took the controller down
        with threads running and the store already recovered.
        """
        source = inspect.getsource(gate_controller.__main__.main)
        loaded = source.index("load_direction_config(")
        for later in (
            "RelayController(", "LocalStore(", "recover_interrupted_actuations(",
            "local_recognizer.start()", "build_background_workers(",
        ):
            with self.subTest(after=later):
                self.assertLess(loaded, source.index(later))


class PassageKeyTests(unittest.TestCase):
    def matched(self, **overrides):
        fields = {
            "source": "reolink_webhook",
            "event_type": "vehicle",
            "rule_id": "vehicle_detected_from_front_gate",
            "correlation": "matched",
            "event_at": datetime(2026, 9, 8, 10, 7, tzinfo=timezone.utc),
            "delta_ms": 120.0,
        }
        fields.update(overrides)
        return TriggerTelemetry(**fields)

    def test_every_frame_of_one_alarm_shares_a_key(self):
        first = passage_key(self.matched(delta_ms=10.0))
        later = passage_key(self.matched(delta_ms=8_000.0))

        self.assertIsNotNone(first)
        self.assertEqual(first, later)

    def test_a_second_alarm_is_a_different_passage(self):
        first = passage_key(self.matched())
        second = passage_key(self.matched(
            event_at=datetime(2026, 9, 8, 10, 9, tzinfo=timezone.utc)
        ))

        self.assertNotEqual(first, second)

    def test_an_uncorrelated_burst_claims_no_alarm(self):
        self.assertIsNone(passage_key(None))
        self.assertIsNone(passage_key(self.matched(correlation="unverified")))
        self.assertIsNone(
            passage_key(self.matched(rule_id=None, event_at=None))
        )


class TrackerTests(unittest.TestCase):
    def tracker(self, **kwargs):
        self.now = 0.0
        return DirectionTracker(clock=lambda: self.now, **kwargs)

    def feed(self, tracker, trace_id, widths, *, step=1.5,
             source=SOURCE_VEHICLE_BOX):
        for width in widths:
            tracker.observe(trace_id, box=(0.1, 0.2, width, 0.1), source=source)
            self.now += step

    def test_one_passage_pools_the_frames_of_its_several_events(self):
        tracker = self.tracker()
        tracker.bind("trace-1", "alarm-a")
        tracker.bind("trace-2", "alarm-a")
        tracker.bind("trace-3", "alarm-a")
        self.feed(tracker, "trace-1", [0.40])
        self.feed(tracker, "trace-2", [0.20])
        self.feed(tracker, "trace-3", [0.10])

        estimate = tracker.estimate("trace-3")

        self.assertEqual(estimate.verdict, VERDICT_EXITING)
        self.assertEqual(estimate.frames, 3)
        self.assertEqual(estimate.source, SOURCE_VEHICLE_BOX)

    def test_a_second_alarm_can_never_read_the_first_one_s_boxes(self):
        tracker = self.tracker()
        tracker.bind("trace-1", "alarm-a")
        tracker.bind("trace-2", "alarm-a")
        tracker.bind("trace-3", "alarm-b")
        self.feed(tracker, "trace-1", [0.40, 0.20])
        self.feed(tracker, "trace-3", [0.10])

        self.assertEqual(tracker.estimate("trace-3").frames, 1)
        self.assertEqual(tracker.estimate("trace-3").method, METHOD_BOX_WIDTH)
        self.assertEqual(tracker.estimate("trace-3").verdict, VERDICT_UNKNOWN)
        self.assertEqual(tracker.estimate("trace-2").frames, 2)

    def test_an_unbound_trace_is_alone_and_ends_with_its_event(self):
        tracker = self.tracker()
        self.feed(tracker, "trace-1", [0.40, 0.20, 0.10])
        self.assertEqual(tracker.estimate("trace-1").verdict, VERDICT_EXITING)

        tracker.forget("trace-1")

        self.assertEqual(tracker.estimate("trace-1").method, METHOD_NONE)
        self.assertEqual(tracker.tracked(), 0)

    def test_forgetting_one_event_keeps_the_passage_for_the_next(self):
        tracker = self.tracker()
        tracker.bind("trace-1", "alarm-a")
        self.feed(tracker, "trace-1", [0.40, 0.20])
        tracker.forget("trace-1")
        tracker.bind("trace-2", "alarm-a")
        self.feed(tracker, "trace-2", [0.10])

        self.assertEqual(tracker.estimate("trace-2").frames, 3)

    def test_a_repeated_alarm_never_reads_the_previous_vehicle_s_boxes(self):
        """This camera repeats webhook bodies, and the key hashes `alarmTime`.

        Two vehicles 40 s apart under one repeated alarm identity used to be
        pooled into one series: the second passage's verdict was fitted from
        boxes the first vehicle produced. The second passage must be built
        from its own frames and nothing else.
        """
        tracker = self.tracker()
        alarm = "reolink_webhook|vehicle|gate_rule|2026-09-08T10:00:00+00:00"
        tracker.bind("trace-1", alarm)
        self.feed(tracker, "trace-1", [0.40, 0.20, 0.10])
        self.assertEqual(tracker.estimate("trace-1").verdict, VERDICT_EXITING)
        tracker.forget("trace-1")

        # The same alarmTime again, 40 s later: a different vehicle.
        self.now += 40.0
        tracker.bind("trace-2", alarm)
        self.feed(tracker, "trace-2", [0.10, 0.20, 0.40])

        estimate = tracker.estimate("trace-2")
        self.assertEqual(estimate.frames, 3, "only its own boxes")
        self.assertEqual(estimate.verdict, VERDICT_ENTERING)

    def test_a_restarted_passage_is_journalled_and_counted(self):
        tracker = self.tracker()
        tracker.bind("trace-1", "alarm-a")
        self.feed(tracker, "trace-1", [0.40])
        self.now += PASSAGE_SESSION_GAP_SECONDS + 1.0

        with self.assertLogs("gate_controller.direction", level="INFO") as logs:
            tracker.bind("trace-2", "alarm-a")

        self.assertIn("stage=passage_restarted", "\n".join(logs.output))
        self.assertEqual(tracker.counters().get("reused_key"), 1)

    def test_a_gap_inside_one_session_still_pools_its_frames(self):
        tracker = self.tracker()
        tracker.bind("trace-1", "alarm-a")
        self.feed(tracker, "trace-1", [0.40])
        tracker.forget("trace-1")
        self.now += PASSAGE_SESSION_GAP_SECONDS - 2.0
        tracker.bind("trace-2", "alarm-a")
        self.feed(tracker, "trace-2", [0.20, 0.10])

        self.assertEqual(tracker.estimate("trace-2").frames, 3)

    def test_two_sources_that_disagree_are_journalled_and_counted(self):
        tracker = self.tracker()
        tracker.bind("trace-1", "alarm-a")
        self.feed(tracker, "trace-1", [0.40, 0.20, 0.10])
        self.now = 0.0
        self.feed(tracker, "trace-1", [0.02, 0.04, 0.08],
                  source=SOURCE_LOCAL_PLATE_BOX)

        with self.assertLogs("gate_controller.direction", level="INFO") as logs:
            estimate = tracker.estimate("trace-1")

        journal = "\n".join(logs.output)
        self.assertIn(
            "gate_direction stage=source_disagreement vehicle=exiting "
            "local=entering cloud=- using=vehicle_box",
            journal,
        )
        self.assertEqual(tracker.counters().get("source_disagreement"), 1)
        # The selection rule itself is unchanged: the vehicle box still wins.
        self.assertEqual(estimate.verdict, VERDICT_EXITING)
        self.assertEqual(estimate.source, SOURCE_VEHICLE_BOX)

    def test_sources_that_agree_are_not_reported_as_a_disagreement(self):
        tracker = self.tracker()
        tracker.bind("trace-1", "alarm-a")
        self.feed(tracker, "trace-1", [0.40, 0.20, 0.10])
        self.now = 0.0
        self.feed(tracker, "trace-1", [0.08, 0.04, 0.02],
                  source=SOURCE_LOCAL_PLATE_BOX)

        tracker.estimate("trace-1")

        self.assertIsNone(tracker.counters().get("source_disagreement"))

    def test_an_event_the_tracker_never_saw_is_still_one_estimate(self):
        """`estimates` is the event count, so every counter has a denominator."""
        tracker = self.tracker()

        self.assertEqual(tracker.estimate("trace-1"), DirectionEstimate())

        counters = tracker.counters()
        self.assertEqual(counters.get("estimates"), 1)
        self.assertEqual(counters.get("no_passage"), 1)

    def test_box_sources_are_never_fitted_as_one_series(self):
        """Vehicle widths and plate widths are on different scales."""
        tracker = self.tracker()
        tracker.bind("trace-1", "alarm-a")
        self.feed(tracker, "trace-1", [0.40, 0.20, 0.10])
        self.feed(tracker, "trace-1", [0.05, 0.05, 0.05],
                  source=SOURCE_LOCAL_PLATE_BOX)

        estimate = tracker.estimate("trace-1")

        self.assertEqual(estimate.source, SOURCE_VEHICLE_BOX)
        self.assertEqual(estimate.frames, 3)
        self.assertEqual(estimate.verdict, VERDICT_EXITING)

    def test_a_plate_series_answers_when_no_vehicle_box_exists(self):
        tracker = self.tracker()
        tracker.bind("trace-1", "alarm-a")
        self.feed(tracker, "trace-1", [0.06, 0.03, 0.015],
                  source=SOURCE_PLATE_BOX)

        estimate = tracker.estimate("trace-1")

        self.assertEqual(estimate.verdict, VERDICT_EXITING)
        self.assertEqual(estimate.source, SOURCE_PLATE_BOX)

    def test_a_dark_scene_is_unmeasured_rather_than_guessed(self):
        tracker = self.tracker()
        tracker.bind("trace-1", "alarm-a")
        self.feed(tracker, "trace-1", [0.40, 0.20, 0.10])
        tracker.note_brightness("trace-1", 0.05)

        estimate = tracker.estimate("trace-1")

        self.assertEqual(estimate.verdict, VERDICT_UNKNOWN)
        self.assertEqual(estimate.method, METHOD_NONE)
        self.assertIsNone(estimate.slope)
        self.assertEqual(estimate.frames, 0)

    def test_one_lit_frame_of_the_passage_is_enough_to_measure_it(self):
        tracker = self.tracker()
        tracker.bind("trace-1", "alarm-a")
        self.feed(tracker, "trace-1", [0.40, 0.20, 0.10])
        tracker.note_brightness("trace-1", 0.05)
        tracker.note_brightness("trace-1", 0.44)

        self.assertEqual(tracker.estimate("trace-1").verdict, VERDICT_EXITING)

    def test_an_event_with_no_boxes_reports_that_nothing_ran(self):
        tracker = self.tracker()
        tracker.bind("trace-1", "alarm-a")
        tracker.note_brightness("trace-1", 0.5)

        estimate = tracker.estimate("trace-1")

        self.assertEqual(estimate.verdict, VERDICT_UNKNOWN)
        self.assertEqual(estimate.method, METHOD_NONE)

    def test_a_disabled_tracker_measures_nothing(self):
        tracker = self.tracker(config=DirectionConfig(enabled=False))
        tracker.bind("trace-1", "alarm-a")
        self.feed(tracker, "trace-1", [0.40, 0.20, 0.10])

        self.assertEqual(tracker.estimate("trace-1"), DirectionEstimate())
        self.assertEqual(tracker.tracked(), 0)

    def test_memory_is_bounded_by_the_passage_cap(self):
        tracker = self.tracker()
        for index in range(MAX_TRACKED_EVENTS * 3):
            trace = f"trace-{index}"
            tracker.bind(trace, f"alarm-{index}")
            self.feed(tracker, trace, [0.2], step=0.01)

        self.assertEqual(tracker.tracked(), MAX_TRACKED_EVENTS)
        self.assertGreater(tracker.counters().get("evicted", 0), 0)

    def test_memory_is_bounded_by_the_sample_cap(self):
        tracker = self.tracker()
        tracker.bind("trace-1", "alarm-a")
        self.feed(tracker, "trace-1", [0.2] * (MAX_SAMPLES * 2), step=0.05)

        self.assertEqual(tracker.estimate("trace-1").frames, MAX_SAMPLES)

    def test_a_passage_nobody_touches_expires(self):
        tracker = self.tracker()
        tracker.bind("trace-1", "alarm-a")
        self.feed(tracker, "trace-1", [0.4, 0.2, 0.1])
        self.assertEqual(tracker.tracked(), 1)

        self.now += PASSAGE_TTL_SECONDS + 1
        tracker.bind("trace-9", "alarm-z")
        self.feed(tracker, "trace-9", [0.2])

        self.assertEqual(tracker.tracked(), 1)
        self.assertEqual(tracker.counters().get("expired"), 1)

    def test_nothing_a_caller_can_pass_makes_the_tracker_raise(self):
        tracker = self.tracker()
        tracker.bind(None, "alarm-a")
        tracker.bind("trace-1", None)
        tracker.observe(None, width=0.2)
        tracker.observe("trace-1", width="wide")
        tracker.observe("trace-1", box=(0.1,))
        tracker.observe("trace-1", width=0.2, source="magnetometer")
        tracker.observe("trace-1")
        tracker.note_brightness("trace-1", "bright")
        tracker.note_brightness(None, 0.5)
        tracker.forget(None)

        self.assertEqual(tracker.estimate("trace-1"), DirectionEstimate())
        self.assertEqual(tracker.estimate(None), DirectionEstimate())

    def test_counters_roll_up_into_the_journal(self):
        tracker = self.tracker()
        with self.assertLogs("gate_controller.direction", level="INFO") as logs:
            for index in range(25):
                trace = f"trace-{index}"
                tracker.bind(trace, f"alarm-{index}")
                self.feed(tracker, trace, [0.4, 0.2, 0.1])
                tracker.estimate(trace)

        self.assertIn("gate_direction stage=counters", "\n".join(logs.output))
        self.assertIn("exiting=25", "\n".join(logs.output))

    def test_each_width_is_journalled_at_debug(self):
        tracker = self.tracker()
        with self.assertLogs("gate_controller.direction", level="DEBUG") as logs:
            self.feed(tracker, "trace-1", [0.4])

        self.assertIn("gate_direction stage=sample", "\n".join(logs.output))
        self.assertIn("source=vehicle_box", "\n".join(logs.output))


class _Relay:
    def trigger(self, source, idempotency_key=None, *, pre_activation_inhibit=None,
                on_activation=None):
        if pre_activation_inhibit is not None and pre_activation_inhibit() is not None:
            return RelayResult(False, "inhibited", idempotency_key)
        if on_activation is not None:
            on_activation()
        return RelayResult(True, "activated", idempotency_key)


class _Recognizer:
    """A recogniser with the direction hooks the real OCR client exposes."""

    def __init__(self, tracker, observation=None, widths=()):
        self.tracker = tracker
        self.observation = observation or PlateObservation("12D3456", 0.95)
        self.widths = list(widths)
        self.bound = []

    def recognise(self, path, *, trace_id=None, **kwargs):
        if self.widths and trace_id:
            self.tracker.observe(trace_id, width=self.widths.pop(0))
        return self.observation

    def bind_direction(self, trace_id, passage):
        self.bound.append((trace_id, passage))
        self.tracker.bind(trace_id, passage)

    def note_direction_brightness(self, trace_id, brightness):
        self.tracker.note_brightness(trace_id, brightness)

    def direction_estimate(self, trace_id):
        return self.tracker.estimate(trace_id)

    def forget_direction(self, trace_id):
        self.tracker.forget(trace_id)


#: Keys whose value is a wall clock rather than a decision: any duration in
#: milliseconds, and any timestamp. Two runs of the same frames make the same
#: decision and never the same microseconds, so these are what the shadow
#: comparison has to drop -- comparing them made it fail roughly one run in
#: seven under load.
_WALL_CLOCK_KEY = re.compile(r"(?:_ms|_at)\Z|\Aat\Z")


def _without_wall_clock(value):
    """``value`` with every wall-clock key removed, at every depth."""
    if isinstance(value, dict):
        return {
            key: _without_wall_clock(item)
            for key, item in value.items()
            if not _WALL_CLOCK_KEY.search(key)
        }
    if isinstance(value, (list, tuple)):
        return [_without_wall_clock(item) for item in value]
    return value


class ProcessorShadowTests(unittest.TestCase):
    """The block ships on every event and changes nothing about any of them."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = 0.0

    def jpeg(self, name, colour=128):
        path = self.root / name
        Image.new("L", (16, 8), color=colour).save(path, format="JPEG")
        return path

    def processor(self, recognizer, name="gate.db"):
        return GateProcessor(
            recognizer=recognizer,
            store=LocalStore(self.root / name),
            relay=_Relay(),
            authorised={"12D3456"},
            cooldown=timedelta(seconds=20),
            clock=lambda: datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc),
        )

    def trigger(self, **overrides):
        fields = {
            "source": "reolink_webhook",
            "event_type": "vehicle",
            "rule_id": "vehicle_detected_from_front_gate",
            "correlation": "matched",
            "event_at": datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc),
            "delta_ms": 100.0,
        }
        fields.update(overrides)
        return TriggerTelemetry(**fields)

    def tracker(self):
        return DirectionTracker(clock=lambda: self.now)

    def test_an_event_with_no_boxes_still_ships_unknown_and_none(self):
        recognizer = _Recognizer(self.tracker())
        processor = self.processor(recognizer)
        self.addCleanup(processor.close)

        result = processor.process((self.jpeg("a.jpg"),), trigger=self.trigger())

        block = result.telemetry.to_wire()["direction"]
        self.assertEqual(block, {
            "verdict": "unknown", "method": "none", "score": None,
            "slope": None, "frames": 0, "span_ms": 0,
        })

    def test_a_receding_vehicle_ships_an_exit_verdict_across_its_events(self):
        tracker = self.tracker()
        recognizer = _Recognizer(
            tracker, observation=PlateObservation(None, 0.0), widths=[0.4, 0.2, 0.1],
        )
        processor = self.processor(recognizer)
        self.addCleanup(processor.close)

        verdicts = []
        for index in range(3):
            result = processor.process(
                (self.jpeg(f"frame-{index}.jpg", colour=100 + index),),
                trigger=self.trigger(delta_ms=100.0 * (index + 1)),
            )
            verdicts.append(result.telemetry.to_wire()["direction"])
            self.now += 1.6

        self.assertEqual(
            [block["verdict"] for block in verdicts],
            ["unknown", "unknown", "exiting"],
        )
        self.assertEqual(verdicts[-1]["method"], "box_width")
        self.assertEqual(verdicts[-1]["frames"], 3)
        self.assertGreaterEqual(verdicts[-1]["span_ms"], 2_000)
        self.assertLess(verdicts[-1]["slope"], -0.06)
        self.assertEqual(
            {trace for trace, _ in recognizer.bound}.__len__(), 3,
            "each event binds its own trace",
        )
        self.assertEqual({passage for _, passage in recognizer.bound}.__len__(), 1)

    def test_the_verdict_is_journalled_once_per_event(self):
        recognizer = _Recognizer(self.tracker())
        processor = self.processor(recognizer)
        self.addCleanup(processor.close)

        with self.assertLogs("gate_controller.processor", level="INFO") as logs:
            processor.process((self.jpeg("a.jpg"),), trigger=self.trigger())

        lines = [line for line in logs.output if "gate_direction" in line]
        self.assertEqual(len(lines), 1)
        for field in ("verdict=", "slope=", "frames=", "span_ms=", "score="):
            self.assertIn(field, lines[0])

    def test_wall_clock_measurements_are_scrubbed_before_the_comparison(self):
        """The shadow comparison must compare the decision, not the stopwatch.

        `ocr_attempts` carries a real `duration_ms`, so comparing the two runs
        verbatim compared two wall clocks and failed under load. Every key
        ending `_ms` and every timestamp goes, at every depth, on both sides.
        """
        scrubbed = _without_wall_clock({
            "duration_ms": 41,
            "attempts": [{"outcome": "ok", "duration_ms": 7, "queued_ms": 1}],
            "stage_timestamps": {"decided_at": "2026-09-08T10:00:00Z"},
            "decision": {"reason": "authorised", "at": "2026-09-08T10:00:00Z"},
        })

        self.assertEqual(scrubbed, {
            "attempts": [{"outcome": "ok"}],
            "stage_timestamps": {},
            "decision": {"reason": "authorised"},
        })

    def test_the_decision_is_identical_with_the_estimator_on_and_off(self):
        """#94's acceptance criterion: shadow mode reaches no decision path."""
        frames = [self.jpeg("shadow-a.jpg"), self.jpeg("shadow-b.jpg", colour=90)]

        def run(recognizer, name):
            processor = self.processor(recognizer, name=name)
            self.addCleanup(processor.close)
            results = []
            for frame in frames:
                outcome = processor.process((frame,), trigger=self.trigger())
                wire = outcome.telemetry.to_wire()
                wire.pop("direction", None)
                results.append((
                    outcome.opened, outcome.reason,
                    # Every wall-clock measurement is scrubbed on both sides:
                    # what is being asserted is that the estimator changed no
                    # decision, not that two runs took the same microseconds.
                    _without_wall_clock(wire["decision"]),
                    _without_wall_clock(wire["actuation"]),
                    _without_wall_clock(wire["ocr_attempts"]),
                    _without_wall_clock(wire["frames"]),
                ))
            return results

        class _Plain:
            def __init__(self):
                self.observation = PlateObservation("12D3456", 0.95)

            def recognise(self, path, *, trace_id=None, **kwargs):
                return self.observation

        tracker = self.tracker()
        with_estimator = run(
            _Recognizer(tracker, widths=[0.4, 0.2]), "with.db",
        )
        self.now = 0.0
        without = run(_Plain(), "without.db")

        self.assertEqual(with_estimator, without)

    def test_an_estimator_that_fails_never_costs_the_event(self):
        class _Broken(_Recognizer):
            def direction_estimate(self, trace_id):
                raise RuntimeError("estimator exploded")

            def bind_direction(self, trace_id, passage):
                raise RuntimeError("binding exploded")

            def note_direction_brightness(self, trace_id, brightness):
                raise RuntimeError("brightness exploded")

        processor = self.processor(_Broken(self.tracker()))
        self.addCleanup(processor.close)

        result = processor.process((self.jpeg("a.jpg"),), trigger=self.trigger())

        self.assertTrue(result.opened)
        self.assertNotIn("direction", result.telemetry.to_wire())

    def test_the_binding_is_dropped_however_the_estimate_ends(self):
        """Five early returns used to leave the trace bound to its passage.

        A binding that outlives its event is a trace still pointing at a
        passage, holding it against the cap and able to be read again.
        """
        class _NoEstimate(_Recognizer):
            def direction_estimate(self, trace_id):
                return None

        class _Exploding(_Recognizer):
            def direction_estimate(self, trace_id):
                raise RuntimeError("estimator exploded")

        class _Unserialisable(_Recognizer):
            def direction_estimate(self, trace_id):
                class _Rotten:
                    def to_wire(self):
                        raise RuntimeError("unserialisable")
                return _Rotten()

        for index, recogniser_type in enumerate(
            (_NoEstimate, _Exploding, _Unserialisable)
        ):
            with self.subTest(recogniser=recogniser_type.__name__):
                tracker = self.tracker()
                recognizer = recogniser_type(tracker)
                processor = self.processor(recognizer, name=f"forget-{index}.db")
                self.addCleanup(processor.close)

                processor.process(
                    (self.jpeg(f"forget-{index}.jpg"),), trigger=self.trigger(),
                )

                self.assertEqual(
                    tracker._bindings, {},
                    "the event is over, so its binding is gone",
                )

    def test_a_frame_measurement_without_a_brightness_never_costs_the_event(self):
        """The whole expression belongs inside the guard, the attribute too."""
        @dataclasses.dataclass(frozen=True)
        class _NoBrightness:
            sequence: int = 0

        original = processor_module.measure_frame_quality
        processor_module.measure_frame_quality = (
            lambda path, digest=None: _NoBrightness()
        )
        self.addCleanup(
            setattr, processor_module, "measure_frame_quality", original,
        )
        processor = self.processor(_Recognizer(self.tracker()))
        self.addCleanup(processor.close)

        result = processor.process((self.jpeg("a.jpg"),), trigger=self.trigger())

        self.assertTrue(result.opened)

    def test_a_recogniser_without_the_hooks_ships_no_block_at_all(self):
        class _Old:
            def recognise(self, path, **kwargs):
                return PlateObservation("12D3456", 0.95)

        processor = self.processor(_Old())
        self.addCleanup(processor.close)

        result = processor.process((self.jpeg("a.jpg"),), trigger=self.trigger())

        self.assertTrue(result.opened)
        self.assertNotIn("direction", result.telemetry.to_wire())


class _Response:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _Session:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0) if self._responses else _Response({"results": []})

    def close(self):
        return None


def cloud_payload(*, vehicle_width, plate_width=40):
    """One Plate Recognizer answer, with the vehicle box it really returns."""
    return {"results": [{
        "plate": "12d3456",
        "score": 0.97,
        "box": {"xmin": 10, "ymin": 20, "xmax": 10 + plate_width, "ymax": 60},
        "vehicle": {
            "type": "Car", "score": 0.9,
            "box": {"xmin": 5, "ymin": 5, "xmax": 5 + vehicle_width, "ymax": 400},
        },
    }]}


class OcrClientWiringTests(unittest.TestCase):
    """The boxes really do come off the answers the pipeline already has."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = 0.0

    def jpeg(self, name):
        path = self.root / name
        Image.new("L", (640, 480), color=128).save(path, format="JPEG")
        return path

    def test_the_cloud_vehicle_box_feeds_the_shrinking_series(self):
        tracker = DirectionTracker(clock=lambda: self.now)
        session = _Session([
            _Response(cloud_payload(vehicle_width=400)),
            _Response(cloud_payload(vehicle_width=200)),
            _Response(cloud_payload(vehicle_width=100)),
        ])
        client = PlateRecognizerClient(
            "token", session=session, direction=tracker,
        )
        self.addCleanup(client.close)
        client.bind_direction("trace-1", "alarm-a")

        for index in range(3):
            client.recognise(self.jpeg(f"frame-{index}.jpg"), trace_id="trace-1")
            self.now += 1.6

        estimate = tracker.estimate("trace-1")

        self.assertEqual(estimate.verdict, VERDICT_EXITING)
        self.assertEqual(estimate.source, SOURCE_VEHICLE_BOX)
        self.assertEqual(estimate.frames, 3)

    def test_an_answer_with_no_boxes_leaves_nothing_to_fit(self):
        tracker = DirectionTracker(clock=lambda: self.now)
        client = PlateRecognizerClient(
            "token", session=_Session([_Response({"results": []})]),
            direction=tracker,
        )
        self.addCleanup(client.close)
        client.bind_direction("trace-1", "alarm-a")

        client.recognise(self.jpeg("frame.jpg"), trace_id="trace-1")

        self.assertEqual(tracker.estimate("trace-1").method, METHOD_NONE)

    def test_a_client_without_a_tracker_is_the_controller_of_yesterday(self):
        client = PlateRecognizerClient(
            "token", session=_Session([_Response(cloud_payload(vehicle_width=400))]),
        )
        self.addCleanup(client.close)

        observation = client.recognise(self.jpeg("frame.jpg"), trace_id="trace-1")

        self.assertEqual(observation.plate, "12D3456")
        self.assertIsNone(client.direction_estimate("trace-1"))
        client.bind_direction("trace-1", "alarm-a")
        client.note_direction_brightness("trace-1", 0.5)
        client.forget_direction("trace-1")


class PersistenceTests(unittest.TestCase):
    """The block has to survive the store and the export, or it never ships."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = 0.0

    def test_the_verdict_reaches_the_stored_outbound_telemetry(self):
        tracker = DirectionTracker(clock=lambda: self.now)
        recognizer = _Recognizer(
            tracker, observation=PlateObservation(None, 0.0), widths=[0.4, 0.2, 0.1],
        )
        store = LocalStore(self.root / "gate.db")
        processor = GateProcessor(
            recognizer=recognizer, store=store, relay=_Relay(),
            authorised={"12D3456"}, cooldown=timedelta(seconds=20),
            clock=lambda: datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc),
        )
        self.addCleanup(processor.close)
        trigger = TriggerTelemetry(
            source="reolink_webhook", event_type="vehicle",
            rule_id="vehicle_detected_from_front_gate", correlation="matched",
            event_at=datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc),
            delta_ms=100.0,
        )

        event_id = None
        for index in range(3):
            path = self.root / f"frame-{index}.jpg"
            Image.new("L", (16, 8), color=100 + index).save(path, format="JPEG")
            event_id = processor.process((path,), trigger=trigger).event_id
            self.now += 1.6

        stored = store.event_telemetry(event_id)

        self.assertEqual(stored["direction"]["verdict"], "exiting")
        self.assertEqual(
            set(stored["direction"]),
            {"verdict", "method", "score", "slope", "frames", "span_ms"},
        )

    def test_the_export_carries_the_block_through_its_own_allowlist(self):
        safe = telemetry_export._safe_telemetry({
            "trace_id": "t", "taxonomy_version": 1, "stage_durations": {},
            "frames": [], "ocr_attempts": [], "decision": {}, "actuation": {},
            "delivery": {},
            "direction": {
                "verdict": "exiting", "method": "box_width", "score": 0.8,
                "slope": -0.33, "frames": 7, "span_ms": 6_700,
                "operator_note": "should not survive",
            },
        })

        self.assertEqual(safe["direction"], {
            "verdict": "exiting", "method": "box_width", "score": 0.8,
            "slope": -0.33, "frames": 7, "span_ms": 6_700,
        })


class DocumentationTests(unittest.TestCase):
    """Every knob is documented, and the promotion rule is written down."""

    ROOT = Path(__file__).resolve().parents[1]

    def test_every_environment_key_appears_in_the_example(self):
        example = (self.ROOT / ".env.example").read_text(encoding="utf-8")

        for key, value in (
            ("GATE_DIRECTION_ENABLED", "true"),
            ("GATE_DIRECTION_EXIT_SLOPE", "-0.06"),
            ("GATE_DIRECTION_ENTER_SLOPE", "0.01"),
            ("GATE_DIRECTION_MIN_FRAMES", "3"),
            ("GATE_DIRECTION_MIN_SPAN_SECONDS", "2.0"),
            ("GATE_DIRECTION_MIN_BRIGHTNESS", "0.20"),
        ):
            with self.subTest(key=key):
                self.assertIn(f"{key}={value}", example)

    def test_the_example_defaults_are_the_shipped_defaults(self):
        example = (self.ROOT / ".env.example").read_text(encoding="utf-8")
        environment = dict(
            line.split("=", 1)
            for line in example.splitlines()
            if line.startswith("GATE_DIRECTION_")
        )

        self.assertEqual(load_direction_config(environment), DirectionConfig())

    def test_the_deployment_doc_states_the_promotion_rule(self):
        doc = (self.ROOT / "docs/deployment.md").read_text(encoding="utf-8")

        self.assertIn("### Vehicle Direction (Shadow)", doc)
        self.assertIn("shadow telemetry and nothing acts on it", doc)
        self.assertIn("gate-controller#95", doc)
        self.assertIn("**zero** false `exiting`", doc)
        self.assertIn("100", doc)
        self.assertIn("gate_direction stage=counters", doc)
        self.assertIn("PI_STATUS_CAPABILITY_KEYS", doc)

    def test_the_night_gate_is_documented_the_way_the_code_gates(self):
        """The code suppresses on the BRIGHTEST frame; the docs said any frame.

        Both files described the opposite rule, and the tracker's own test
        (`one lit frame of the passage is enough`) asserts the code's. A
        deployment note that inverts a suppression rule is how an operator
        sets `GATE_DIRECTION_MIN_BRIGHTNESS` to the wrong side of the data.
        """
        for name in ("docs/deployment.md", ".env.example"):
            with self.subTest(document=name):
                text = (self.ROOT / name).read_text(encoding="utf-8")
                section = text[text.index("GATE_DIRECTION_MIN_BRIGHTNESS") - 800:]
                self.assertRegex(section.lower(), r"brightest")
                self.assertRegex(
                    section.lower(),
                    r"(a )?(single|one) (dark|lit) frame does\s+\*?\*?not\*?\*?",
                )

    def test_the_deployment_doc_states_the_recall_on_the_whole_set(self):
        doc = (self.ROOT / "docs/deployment.md").read_text(encoding="utf-8")

        self.assertIn("7 of 12", doc)
        self.assertIn("five exits fail the gate", doc)

    def test_the_deployment_doc_says_what_the_replay_test_does_not_prove(self):
        doc = (self.ROOT / "docs/deployment.md").read_text(encoding="utf-8")

        self.assertIn("42 recorded slopes", doc)
        self.assertIn("does **not** validate the fitter against raw box widths", doc)


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    unittest.main()
