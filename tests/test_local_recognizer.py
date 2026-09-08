"""On-device recognition: shadow journalling and the active decision path.

Nothing here imports onnxruntime, open-image-models or fast-plate-ocr: the
engine seam is faked, exactly as it is on a CI runner where those wheels are
not installed.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic, sleep
from unittest.mock import patch
from zoneinfo import ZoneInfo

from PIL import Image

import gate_controller.local_recognizer as local_recognizer_module
from gate_controller.local_recognizer import (
    DEFAULT_DETECTOR,
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_MODEL_DIR,
    DEFAULT_RECOGNISER,
    LOCAL_DECISION_CLOUD_RESERVE_SECONDS,
    LOCAL_DECISION_TIMEOUT_SECONDS,
    LOCAL_EVENT_WAIT_BUDGET_SECONDS,
    EngineRead,
    EngineResult,
    LocalRecognition,
    LocalRecognizer,
    LocalRecognizerConfig,
    LocalRecognizerUnavailable,
    build_local_recognizer,
    character_scores,
    classify_agreement,
    is_confident,
    load_local_recognizer_config,
)
from gate_controller.corpus import TrainingCorpus
from gate_controller.match_policy import (
    DEFAULT_POLICY, RECOMMENDED_POLICY, STRICT_POLICY, apply_confidence,
)
from gate_controller.matching import decide_access
from gate_controller.models import PlateObservation, RelayResult
from gate_controller.ocr import OcrResponseError, PlateRecognizerClient
from gate_controller.processor import GateProcessor
from gate_controller.store import LocalStore
from gate_controller.telemetry import LocalOcrTelemetry, ProcessingTrace


class RecordingLogger:
    def __init__(self):
        self.lines = []

    def info(self, message, *args):
        self.lines.append(message % args if args else message)

    warning = info

    def line(self, stage):
        for line in self.lines:
            if f"stage={stage} " in line or line.endswith(f"stage={stage}"):
                return line
        return None

    def lines_for(self, stage):
        return [line for line in self.lines if f"stage={stage} " in line]


class FakeEngine:
    """Stands in for the detector/recogniser pair, with no ONNX anywhere."""

    def __init__(self, config, script, *, load_error=None, warmup_error=None,
                 gate=None, tracker=None):
        self.config = config
        self._script = list(script)
        self._load_error = load_error
        self._warmup_error = warmup_error
        self._gate = gate
        self._tracker = tracker
        self.loaded = False
        self.warmed = False

    def load(self):
        if self._load_error is not None:
            raise self._load_error
        self.loaded = True

    def warmup(self):
        if self._warmup_error is not None:
            raise self._warmup_error
        self.warmed = True

    def read(self, image):
        if self._tracker is not None:
            self._tracker.enter()
        try:
            if self._gate is not None:
                self._gate.wait(5)
            outcome = self._script.pop(0) if self._script else EngineResult()
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        finally:
            if self._tracker is not None:
                self._tracker.leave()


class ConcurrencyTracker:
    def __init__(self):
        self._lock = Lock()
        self.current = 0
        self.peak = 0

    def enter(self):
        with self._lock:
            self.current += 1
            self.peak = max(self.peak, self.current)

    def leave(self):
        with self._lock:
            self.current -= 1


def engine_factory(script, **kwargs):
    engines = []

    def factory(config):
        engine = FakeEngine(config, script, **kwargs)
        engines.append(engine)
        return engine

    factory.engines = engines
    return factory


def read(plate, confidence, box=(10, 20, 110, 60), detection=0.9, mean=None):
    return EngineResult(
        reads=(EngineRead(
            plate=plate, confidence=confidence, detection_confidence=detection,
            box=box, mean_confidence=confidence if mean is None else mean,
        ),),
        width=1280, height=720, decode_ms=8.0, detect_ms=150.0, ocr_ms=14.0,
    )


def no_plate():
    return EngineResult(width=1280, height=720, decode_ms=8.0, detect_ms=150.0)


def recognizer(script, *, mode="shadow", cloud="fallback", logger=None,
               wall_clock=None, min_confidence=DEFAULT_MIN_CONFIDENCE, **kwargs):
    config = LocalRecognizerConfig(
        mode=mode, cloud=cloud, min_confidence=min_confidence,
        model_dir=Path("/var/lib/gate-controller/models"),
    )
    local = LocalRecognizer(
        config, engine_factory=engine_factory(script, **kwargs),
        logger=logger or RecordingLogger(), wall_clock=wall_clock,
    )
    local.start()
    local.wait_ready(5)
    return local


class ConfigurationTests(unittest.TestCase):
    def test_an_unset_environment_leaves_local_recognition_off(self):
        config = load_local_recognizer_config({})

        self.assertEqual(config.mode, "off")
        self.assertFalse(config.enabled)
        self.assertIsNone(build_local_recognizer({}))

    def test_defaults_match_what_was_measured_on_this_gate(self):
        config = load_local_recognizer_config({"GATE_LOCAL_OCR_MODE": "shadow"})

        self.assertEqual(config.detector, DEFAULT_DETECTOR)
        self.assertEqual(config.recogniser, DEFAULT_RECOGNISER)
        self.assertEqual(config.threads, 1, "the board is thermally limited")
        self.assertEqual(
            config.min_confidence, 0.5,
            "0.95 was the shadow-mode measurement threshold; as the admission "
            "gate it is the floor under agreement_min_local and would keep the "
            "agreement rule from ever running on shipped defaults",
        )
        self.assertEqual(str(config.model_dir), DEFAULT_MODEL_DIR)
        self.assertEqual(config.cloud, "fallback")

    def test_rejects_configuration_it_cannot_honour(self):
        for environment, message in (
            ({"GATE_LOCAL_OCR_MODE": "local_only"}, "GATE_LOCAL_OCR_MODE"),
            ({"GATE_LOCAL_OCR_CLOUD": "never"}, "GATE_LOCAL_OCR_CLOUD"),
            ({"GATE_LOCAL_OCR_THREADS": "0"}, "GATE_LOCAL_OCR_THREADS"),
            ({"GATE_LOCAL_OCR_THREADS": "many"}, "GATE_LOCAL_OCR_THREADS"),
            ({"GATE_LOCAL_OCR_MIN_CONFIDENCE": "2"}, "GATE_LOCAL_OCR_MIN_CONFIDENCE"),
            ({"GATE_LOCAL_OCR_MODEL_DIR": "models"}, "GATE_LOCAL_OCR_MODEL_DIR"),
        ):
            with self.subTest(environment=environment):
                with self.assertRaisesRegex(ValueError, message):
                    load_local_recognizer_config(environment)


class AgreementTests(unittest.TestCase):
    def test_every_agreement_case_is_named(self):
        self.assertEqual(classify_agreement("12D3456", "12D3456"), "match")
        self.assertEqual(classify_agreement("12-D-3456", "12d3456"), "match")
        self.assertEqual(classify_agreement("12D3456", "12D9999"), "mismatch")
        self.assertEqual(classify_agreement("12D3456", None), "local_only")
        self.assertEqual(classify_agreement(None, "12D3456"), "cloud_only")
        self.assertEqual(classify_agreement(None, None), "both_none")

    def test_the_journal_line_carries_every_field_the_weekly_read_needs(self):
        logger = RecordingLogger()
        local = recognizer([read("12D3456", 0.99)], logger=logger)
        frame = local.begin(b"frame", trace_id="trace-1", authorised={"12D3456"})
        frame.result(5)
        frame.complete_cloud("12D3456", 0.91)

        line = logger.line("shadow")
        self.assertIsNotNone(line, logger.lines)
        for fragment in (
            "gate_local_ocr stage=shadow", "trace_id=trace-1",
            "local_plate=12D3456", "local_score=0.990", "local_ms=",
            "cloud_plate=12D3456", "cloud_score=0.910", "agreement=match",
            "authorised=both", "decision_source=cloud",
        ):
            self.assertIn(fragment, line)
        local.close()

    def test_a_read_below_the_threshold_is_not_an_authorised_local_match(self):
        logger = RecordingLogger()
        local = recognizer([read("12D3456", 0.40)], logger=logger)
        frame = local.begin(b"frame", trace_id="trace-2", authorised={"12D3456"})
        frame.result(5)
        frame.complete_cloud("12D3456", 0.99)

        line = logger.line("shadow")
        self.assertIn("agreement=match", line)
        self.assertIn(
            "authorised=cloud_match", line,
            "the cloud read authorises, the low-confidence local read does not",
        )
        local.close()

    def test_counters_and_latency_are_reported_for_the_week(self):
        local = recognizer([
            read("12D3456", 0.99), read("12D9999", 0.99), no_plate(),
        ])
        for index, cloud in enumerate(("12D3456", "12D3456", "12D3456")):
            frame = local.begin(
                b"frame", trace_id=f"trace-{index}", authorised={"12D3456"},
            )
            frame.result(5)
            frame.complete_cloud(cloud, 0.99)

        status = local.status()
        self.assertEqual(status["mode"], "shadow")
        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["frames"], 3)
        self.assertEqual(status["agreements"], 1)
        self.assertEqual(status["mismatches"], 1)
        self.assertEqual(status["cloud_only"], 1)
        self.assertEqual(status["local_only"], 0)
        self.assertEqual(status["unavailable"], 0)
        self.assertEqual(status["latency_ms"]["samples"], 3)
        self.assertGreaterEqual(status["latency_ms"]["mean"], 0.0)
        self.assertGreaterEqual(
            status["latency_ms"]["p95"], status["latency_ms"]["mean"] - 1e-6
        )
        local.close()


class AvailabilityTests(unittest.TestCase):
    def test_a_missing_dependency_is_reported_once_and_changes_nothing(self):
        logger = RecordingLogger()
        config = LocalRecognizerConfig(mode="active")
        local = LocalRecognizer(
            config,
            engine_factory=engine_factory(
                [], load_error=LocalRecognizerUnavailable("import_failed"),
            ),
            logger=logger,
        )
        local.start()
        local.wait_ready(5)

        self.assertFalse(local.available)
        self.assertEqual(
            logger.lines_for("unavailable"),
            ["gate_local_ocr stage=unavailable reason=import_failed"],
        )

        frame = local.begin(b"frame", trace_id="t", authorised={"12D3456"})
        recognition = frame.result(5)
        self.assertEqual(recognition.status, "unavailable")
        self.assertFalse(local.decides(frame, recognition))
        self.assertEqual(local.status()["state"], "unavailable")
        self.assertEqual(len(logger.lines_for("unavailable")), 1, "reported once")
        local.close()

    def test_an_engine_that_throws_leaves_the_frame_to_the_cloud(self):
        local = recognizer([RuntimeError("inference exploded")], mode="active")
        frame = local.begin(b"frame", trace_id="t", authorised={"12D3456"})
        recognition = frame.result(5)

        self.assertEqual(recognition.status, "error")
        self.assertFalse(local.decides(frame, recognition))
        self.assertEqual(local.status()["errors"], 1)
        local.close()

    def test_warm_up_loads_the_models_and_says_so(self):
        logger = RecordingLogger()
        factory = engine_factory([read("12D3456", 0.99)])
        local = LocalRecognizer(
            LocalRecognizerConfig(mode="shadow"), engine_factory=factory,
            logger=logger,
        )
        local.start()
        self.assertTrue(local.wait_ready(5))

        engine = factory.engines[0]
        self.assertTrue(engine.loaded)
        self.assertTrue(engine.warmed, "a synthetic inference precedes the first frame")
        line = logger.line("ready")
        self.assertIn(f"detector={DEFAULT_DETECTOR}", line)
        self.assertIn(f"recogniser={DEFAULT_RECOGNISER}", line)
        self.assertIn("load_ms=", line)
        self.assertIn("warmup_ms=", line)
        local.close()


class SerialisationTests(unittest.TestCase):
    def test_one_worker_means_two_inferences_never_share_the_board(self):
        tracker = ConcurrencyTracker()
        local = recognizer(
            [read("12D3456", 0.99) for _ in range(4)], tracker=tracker,
        )
        for index in range(4):
            frame = local.begin(
                b"frame", trace_id=f"t{index}", authorised={"12D3456"},
            )
            frame.result(5)

        self.assertEqual(tracker.peak, 1, "the pool has exactly one worker")
        local.close()

    def test_a_frame_is_declined_rather_than_queued_behind_a_running_read(self):
        # A stalled inference must not make every later frame wait its turn
        # before falling back to the cloud, and a queue would pin each JPEG.
        gate = Event()
        tracker = ConcurrencyTracker()
        local = recognizer(
            [read("12D3456", 0.99) for _ in range(3)], gate=gate, tracker=tracker,
        )
        first = local.begin(b"frame", trace_id="t0", authorised={"12D3456"})
        second = local.begin(b"frame", trace_id="t1", authorised={"12D3456"})

        self.assertEqual(
            second.result(5).status, "unavailable",
            "the second frame is answered at once, not queued",
        )
        gate.set()
        self.assertEqual(first.result(5).plate, "12D3456")
        self.assertEqual(local.status()["busy"], 1)

        # Admission is released once the read finishes.
        third = local.begin(b"frame", trace_id="t2", authorised={"12D3456"})
        self.assertEqual(third.result(5).plate, "12D3456")
        self.assertEqual(tracker.peak, 1)
        local.close()


class EventIsolationTests(unittest.TestCase):
    def test_untraced_frames_never_pool_into_one_two_frame_match(self):
        # A disabled trace means no event identity. Two unrelated frames must
        # not satisfy the two-frame fuzzy rule between them.
        local = recognizer(
            [read("11WH2S71", 0.99), read("11WH2S71", 0.99)], mode="active",
        )
        first = local.begin(b"frame", trace_id=None, authorised={"11WH2571"})
        first.result(5)
        second = local.begin(b"frame", trace_id=None, authorised={"11WH2571"})
        recognition = second.result(5)

        self.assertEqual(len(second.local_observations()), 1)
        self.assertFalse(
            local.decides(second, recognition),
            "a lone fuzzy frame must not authorise on a previous event's read",
        )
        local.close()


class ConfidenceGateTests(unittest.TestCase):
    """The one local-specific gate: which statistic, and what it rejects."""

    def test_a_non_finite_score_never_clears_the_gate(self):
        # `nan < 0.95` is False, so a `<` gate lets NaN through. Every form of
        # unusable score has to fail closed instead.
        for score in (float("nan"), float("-nan"), None, "high", object()):
            with self.subTest(score=score):
                self.assertFalse(is_confident(score, 0.95))
        self.assertFalse(is_confident(float("inf"), 0.95), "not a probability")
        self.assertTrue(is_confident(0.95, 0.95))
        self.assertTrue(is_confident(1, 0.95), "a scalar that is not a Python float")

    def test_decides_rejects_a_non_finite_recogniser_confidence(self):
        # An empty probability slice yields NaN, and a NaN that got past here
        # would reach GateEvent.ocr_confidence and be written to the outbox as
        # a bare NaN literal no strict JSON reader accepts.
        local = recognizer([read("12D3456", float("nan"))], mode="active")
        frame = local.begin(b"frame", trace_id="t-nan", authorised={"12D3456"})
        recognition = frame.result(5)

        self.assertEqual(recognition.plate, "12D3456")
        self.assertFalse(
            local.decides(frame, recognition),
            "a NaN confidence must not answer for the frame",
        )
        self.assertEqual(
            frame.local_observations(), (),
            "and must not be accumulated towards the two-frame rule either",
        )
        local.close()

    def test_a_nan_read_cannot_ride_on_an_earlier_confident_read(self):
        # The event already authorises on the first frame's read of the same
        # plate, so nothing downstream would stop the NaN: the gate is the
        # only thing standing between it and GateEvent.ocr_confidence.
        local = recognizer(
            [read("12D3456", 0.99), read("12D3456", float("nan"))], mode="active",
        )
        first = local.begin(b"frame", trace_id="t-nan2", authorised={"12D3456"})
        self.assertTrue(local.decides(first, first.result(5)))

        second = local.begin(b"frame", trace_id="t-nan2", authorised={"12D3456"})
        recognition = second.result(5)

        self.assertFalse(
            local.decides(second, recognition),
            "a NaN confidence is not a confidence, whatever the event allows",
        )
        local.close()

    def test_the_threshold_gates_on_the_weakest_character_not_the_mean(self):
        # Six characters at 1.00 and one at 0.65 average 0.96 and would clear
        # a 0.95 mean gate; that weak character is the one deciding whether
        # the read is the authorised plate.
        scores = character_scores("12D3456", [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.65])

        self.assertEqual(len(scores), 7)
        self.assertEqual(min(scores), 0.65)
        self.assertGreater(sum(scores) / len(scores), DEFAULT_MIN_CONFIDENCE)

        local = recognizer(
            [read("12D3456", 0.65, mean=sum(scores) / len(scores))], mode="active",
        )
        frame = local.begin(b"frame", trace_id="t-weak", authorised={"12D3456"})
        recognition = frame.result(5)

        self.assertFalse(local.decides(frame, recognition))
        self.assertGreater(
            recognition.mean_score, DEFAULT_MIN_CONFIDENCE,
            "the mean is kept for telemetry, and it does not gate",
        )
        local.close()

    def test_an_interior_pad_character_cannot_shift_the_confidence_slice(self):
        # The library strips only *trailing* padding, so slicing char_probs by
        # the length of the normalised plate would drop a real character and
        # count a pad slot in its place.
        scores = character_scores(
            "12D_3456", [0.99, 0.99, 0.99, 0.10, 0.99, 0.99, 0.99, 0.99],
        )

        self.assertEqual(len(scores), 7, "seven characters, eight slots")
        self.assertEqual(min(scores), 0.99, "the pad slot's score is not a character's")

    def test_probabilities_that_are_missing_or_unusable_fail_closed(self):
        self.assertEqual(character_scores("12D3456", None), ())
        self.assertEqual(character_scores("", [0.9]), ())
        self.assertEqual(
            character_scores("AB", [float("nan"), "x"]), (0.0, 0.0),
            "an unscorable slot is 0.0, not absent",
        )


class FrameCreditTests(unittest.TestCase):
    """A frame answers on its own read, never on a sibling frame's."""

    def test_a_frame_never_answers_on_another_frames_credit(self):
        local = recognizer(
            [read("12D3456", 0.99), read("99ZZ9999", 0.99)], mode="active",
        )
        first = local.begin(b"frame", trace_id="t-credit", authorised={"12D3456"})
        self.assertTrue(local.decides(first, first.result(5)))

        second = local.begin(b"frame", trace_id="t-credit", authorised={"12D3456"})
        recognition = second.result(5)

        self.assertEqual(recognition.plate, "99ZZ9999")
        self.assertFalse(
            local.decides(second, recognition),
            "the event authorises on the first frame's plate, not this one's",
        )
        local.close()

    def test_the_journal_labels_the_frame_that_actually_authorised(self):
        logger = RecordingLogger()
        local = recognizer(
            [read("12D3456", 0.99), read("99ZZ9999", 0.99)], mode="active",
            logger=logger,
        )
        first = local.begin(b"frame", trace_id="t-label", authorised={"12D3456"})
        first.result(5)
        first.complete_cloud(None, 0.0, decided=False)
        second = local.begin(b"frame", trace_id="t-label", authorised={"12D3456"})
        second.result(5)
        second.complete_cloud(None, 0.0, decided=False)

        lines = logger.lines_for("active")
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("local_plate=12D3456", lines[0])
        self.assertIn("authorised=local_match", lines[0])
        self.assertIn("local_plate=99ZZ9999", lines[1])
        self.assertIn(
            "authorised=none", lines[1],
            "the unrelated read is not the local match, whatever its siblings did",
        )
        local.close()


class RecordingMatching:
    """The real :func:`decide_access`, with every call it was given recorded."""

    def __init__(self):
        self.calls = []

    def __call__(self, observations, authorised, policy=None, *, now=None):
        observations = tuple(observations)
        self.calls.append({"policy": policy, "now": now, "observations": observations})
        return decide_access(observations, authorised, policy, now=now)


class MatchPolicyTests(unittest.TestCase):
    """The local gate runs under the band the processor is about to apply.

    Nothing here is faked: these are the shipped ``strict`` and ``standard``
    levels and the shipped :func:`decide_access`, driven through the local
    admission gate exactly as ``PlateRecognizerClient`` drives it.
    """

    #: 5 and S are a known OCR confusion pair, so this read is one substitution
    #: from the authorised plate at equal length: admitted by `standard` on the
    #: second high-confidence frame, refused outright by `strict`.
    AUTHORISED = "11WH2571"
    MISREAD = "11WH2S71"

    def _fuzzy_frames(self, policy, *, wall_clock=None, matching=None):
        matching = matching or RecordingMatching()
        local = recognizer(
            [read(self.MISREAD, 0.99), read(self.MISREAD, 0.99)], mode="active",
            wall_clock=wall_clock,
        )
        self.addCleanup(local.close)
        decisions = []
        with patch.object(local_recognizer_module, "decide_access", matching):
            for _ in range(2):
                frame = local.begin(
                    b"frame", trace_id="t-policy", authorised={self.AUTHORISED},
                    policy=policy,
                )
                recognition = frame.result(5)
                decisions.append(local.decides(frame, recognition))
                frame.complete_cloud(None, 0.0, decided=False)
        return local, decisions, matching

    def test_a_standard_band_admits_the_fuzzy_read_the_two_frame_rule_allows(self):
        _local, decisions, matching = self._fuzzy_frames(lambda: DEFAULT_POLICY)

        self.assertEqual([False, True], decisions)
        self.assertTrue(all(call["policy"] is DEFAULT_POLICY for call in matching.calls))

    def test_a_strict_band_denies_a_fuzzy_local_read_standard_would_admit(self):
        # Otherwise the local path would burn the frame the cloud would have
        # read exactly, making the overnight band *less* likely to open for a
        # legitimate car.
        local, decisions, matching = self._fuzzy_frames(lambda: STRICT_POLICY)

        self.assertEqual([False, False], decisions)
        self.assertTrue(all(call["policy"] is STRICT_POLICY for call in matching.calls))
        self.assertEqual(
            local.summary("t-policy")["authorised"], "none",
            "the journal label is computed under the band in force too",
        )

    def test_the_overnight_band_of_a_schedule_denies_what_the_day_band_admits(self):
        # The same recommended schedule, the same read, two wall clocks: this
        # is the band lookup itself, not a hand-picked single-level policy.
        dublin = ZoneInfo("Europe/Dublin")
        night = datetime(2026, 9, 7, 2, 30, tzinfo=dublin)
        day = datetime(2026, 9, 7, 14, 30, tzinfo=dublin)

        _l, night_decisions, _m = self._fuzzy_frames(
            lambda: RECOMMENDED_POLICY, wall_clock=lambda: night,
        )
        _l, day_decisions, _m = self._fuzzy_frames(
            lambda: RECOMMENDED_POLICY, wall_clock=lambda: day,
        )

        self.assertEqual([False, False], night_decisions)
        self.assertEqual([False, True], day_decisions)

    def test_the_band_is_resolved_once_per_frame(self):
        # The decision and the journal line must speak about the same instant,
        # or a frame read at 01:59:59 could be labelled under the 02:00 band.
        policies = iter((DEFAULT_POLICY, STRICT_POLICY, DEFAULT_POLICY, STRICT_POLICY))

        _local, _decisions, matching = self._fuzzy_frames(lambda: next(policies))

        self.assertTrue(matching.calls)
        for frame_policy in (DEFAULT_POLICY, STRICT_POLICY):
            moments = {
                call["now"] for call in matching.calls
                if call["policy"] is frame_policy
            }
            self.assertEqual(len(moments), 1, matching.calls)
        self.assertEqual(
            2, len({id(call["policy"]) for call in matching.calls}),
            "one band per frame, not one per decide_access call",
        )

    def test_an_unreadable_policy_provider_never_widens_the_match(self):
        def broken():
            raise RuntimeError("no schedule")

        local, decisions, matching = self._fuzzy_frames(broken)

        self.assertTrue(all(call["policy"] is None for call in matching.calls))
        self.assertEqual(
            [False, True], decisions,
            "decide_access applies its own default, which is what the "
            "processor falls back to for the same frame",
        )
        local.close()

    def test_the_client_hands_the_recogniser_the_policy_provider_it_was_given(self):
        # The wiring `__main__` performs: one provider, both consumers. The
        # client resolves it per frame rather than at construction, so a
        # schedule refreshed in the background reaches the next frame.
        local = recognizer([read(self.AUTHORISED, 0.99)], mode="active")
        self.addCleanup(local.close)
        provider = lambda: STRICT_POLICY
        seen = []
        original = local.begin

        def watching(*args, **kwargs):
            seen.append(kwargs.get("policy"))
            return original(*args, **kwargs)

        client = PlateRecognizerClient(
            "token", session=FakeSession([FakeResponse(cloud_payload("12D3456"))]),
            local_recognizer=local, authorised=lambda: {self.AUTHORISED},
            match_policy=provider,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frame.jpg"
            Image.new("L", (64, 32), color=128).save(path, format="JPEG")
            with patch.object(local, "begin", watching):
                client.recognise(path, trace_id="t-wiring")

        self.assertEqual([provider], seen)


class DecisionBudgetTests(unittest.TestCase):
    """A stalled local read must not spend the burst's decision budget."""

    def test_the_wait_is_clamped_to_what_the_cloud_still_needs(self):
        local = recognizer([], mode="active")

        self.assertEqual(
            LOCAL_DECISION_TIMEOUT_SECONDS, local.wait_seconds("t", None),
            "with no budget declared, the stuck-engine ceiling stands",
        )
        self.assertAlmostEqual(
            0.5,
            local.wait_seconds("t", LOCAL_DECISION_CLOUD_RESERVE_SECONDS + 0.5),
        )
        self.assertEqual(
            0.0, local.wait_seconds("t", LOCAL_DECISION_CLOUD_RESERVE_SECONDS),
            "a budget that only covers the cloud request buys no local wait",
        )
        self.assertEqual(0.0, local.wait_seconds("t", -1.0))
        self.assertEqual(0.0, local.wait_seconds("t", float("nan")))
        local.close()

    def test_one_event_pays_the_stuck_engine_guard_once(self):
        local = recognizer([], mode="active")

        self.assertEqual(
            LOCAL_EVENT_WAIT_BUDGET_SECONDS, local.event_wait_remaining("burst")
        )
        local.record_event_wait("burst", LOCAL_EVENT_WAIT_BUDGET_SECONDS / 2)
        self.assertAlmostEqual(
            LOCAL_EVENT_WAIT_BUDGET_SECONDS / 2,
            local.wait_seconds("burst", 60.0),
        )
        local.record_event_wait("burst", LOCAL_EVENT_WAIT_BUDGET_SECONDS)
        self.assertEqual(
            0.0, local.wait_seconds("burst", 60.0),
            "later frames of the same burst do not pay the guard again",
        )
        self.assertEqual(
            LOCAL_DECISION_TIMEOUT_SECONDS, local.wait_seconds("other-burst", 60.0),
            "the next event starts with its own budget",
        )
        local.forget("burst")
        self.assertEqual(
            LOCAL_EVENT_WAIT_BUDGET_SECONDS, local.event_wait_remaining("burst")
        )
        local.close()

    def test_a_wait_that_has_run_out_still_takes_a_read_that_already_landed(self):
        local = recognizer([read("12D3456", 0.99)], mode="active")
        frame = local.begin(b"frame", trace_id="t-settled", authorised={"12D3456"})
        frame.result(5)

        self.assertEqual(frame.result(0.0).plate, "12D3456")
        local.close()


class LifecycleTests(unittest.TestCase):
    def test_a_closed_recogniser_is_no_longer_ready(self):
        local = recognizer([read("12D3456", 0.99)], mode="active")
        self.assertTrue(local.available)

        local.close()

        self.assertFalse(local.available, "the engine is gone")
        self.assertFalse(local.active)
        self.assertEqual(local.status()["state"], "closed")
        self.assertIs(
            local.begin(b"frame", trace_id="t", authorised={"12D3456"}),
            local_recognizer_module.NULL_FRAME,
        )

    def test_a_busy_frame_is_counted_once(self):
        gate = Event()
        local = recognizer(
            [read("12D3456", 0.99) for _ in range(2)], gate=gate,
        )
        first = local.begin(b"frame", trace_id="t0", authorised={"12D3456"})
        second = local.begin(b"frame", trace_id="t1", authorised={"12D3456"})
        self.assertEqual(second.result(5).status, "unavailable")
        gate.set()
        first.result(5)

        status = local.status()
        self.assertEqual(status["busy"], 1)
        self.assertEqual(
            status["unavailable"], 0,
            "a frame refused because a read is in flight is busy, not unavailable",
        )
        local.close()


class TelemetryBlockTests(unittest.TestCase):
    def test_the_event_block_is_bounded_and_closed_vocabulary(self):
        local = recognizer([read("12D3456", 0.99)])
        frame = local.begin(b"frame", trace_id="trace-9", authorised={"12D3456"})
        frame.result(5)
        frame.complete_cloud("12D3456", 0.99)

        block = local.summary("trace-9")
        self.assertEqual(block["mode"], "shadow")
        self.assertEqual(block["frames"], 1)
        self.assertEqual(block["plate"], "12D3456")
        self.assertEqual(block["agreement"], "match")
        self.assertEqual(block["authorised"], "both")
        self.assertEqual(block["decision_source"], "cloud")

        trace = ProcessingTrace()
        trace.set_local_ocr(block)
        wire = trace.finish().to_wire()["local_ocr"]
        self.assertEqual(wire["mode"], "shadow")
        self.assertEqual(wire["plate"], "12D3456")
        self.assertEqual(wire["agreement"], "match")
        self.assertIsInstance(wire["latency_ms"], int)

        local.forget("trace-9")
        self.assertIsNone(local.summary("trace-9"))
        local.close()

    def test_an_unknown_token_never_escapes_onto_the_wire(self):
        wire = LocalOcrTelemetry(
            mode="experimental", agreement="probably", authorised="maybe",
            decision_source="somewhere", status="weird", score=5.0,
            latency_ms=-3,
        ).to_wire()

        self.assertEqual(wire["mode"], "shadow")
        self.assertEqual(wire["agreement"], "both_none")
        self.assertEqual(wire["authorised"], "none")
        self.assertEqual(wire["decision_source"], "none")
        self.assertEqual(wire["status"], "no_plate")
        self.assertEqual(wire["score"], 1.0)
        self.assertEqual(wire["latency_ms"], 0)

    def test_the_block_keeps_the_nine_keys_the_worker_accepts(self):
        wire = LocalOcrTelemetry(mode="active").to_wire()

        self.assertEqual(
            sorted(wire),
            sorted((
                "mode", "frames", "plate", "score", "latency_ms", "agreement",
                "authorised", "decision_source", "status",
            )),
            "access-gate-ui PR #42 validates this block against a closed "
            "allowlist; a tenth key would be rejected, and `box` never travels",
        )

    def test_a_local_decision_can_only_be_reported_under_active_mode(self):
        # The Worker enforces this pairing and rejects the whole event when it
        # is broken, so a shadow block claiming a local decision must never
        # leave here. Nothing in the controller can produce one - only the
        # active path calls `decided_locally` - and this is the backstop.
        self.assertEqual(
            "local",
            LocalOcrTelemetry(mode="active", decision_source="local")
            .to_wire()["decision_source"],
        )
        for mode in ("shadow", "experimental"):
            with self.subTest(mode=mode):
                wire = LocalOcrTelemetry(
                    mode=mode, decision_source="local"
                ).to_wire()
                self.assertEqual(wire["decision_source"], "none")
                self.assertNotEqual(wire["mode"], "active")

    def test_the_recogniser_never_labels_a_shadow_event_as_deciding(self):
        local = recognizer([read("12D3456", 0.99)], mode="shadow")
        frame = local.begin(b"frame", trace_id="t-shadow", authorised={"12D3456"})
        frame.result(5)
        # Driven exactly as the active path drives a frame it answered for,
        # which shadow mode can never reach.
        frame.decided_locally()
        frame.cloud_skipped()

        block = local.summary("t-shadow")
        trace = ProcessingTrace()
        trace.set_local_ocr(block)
        wire = trace.finish().to_wire()["local_ocr"]

        self.assertEqual(wire["mode"], "shadow")
        self.assertEqual(wire["decision_source"], "none")
        local.close()


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0) if self._responses else FakeResponse({"results": []})

    def close(self):
        return None


class RecordingCorpus:
    def __init__(self):
        self.records = []

    def record(self, image, **kwargs):
        self.records.append(kwargs)
        return None


def cloud_payload(plate, score=0.97):
    return {"results": [{"plate": plate, "score": score, "box": {
        "xmin": 10, "ymin": 20, "xmax": 110, "ymax": 60,
    }}]}


class OcrClientIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "frame.jpg"
        Image.new("L", (64, 32), color=128).save(self.path, format="JPEG")

    def tearDown(self):
        self.directory.cleanup()

    def _client(self, local, session, *, corpus=None):
        return PlateRecognizerClient(
            "token", session=session, local_recognizer=local,
            authorised=lambda: {"12D3456"}, corpus=corpus,
        )

    def test_mode_off_leaves_the_cloud_path_exactly_as_it_was(self):
        session = FakeSession([FakeResponse(cloud_payload("12D3456"))])
        corpus = RecordingCorpus()
        client = PlateRecognizerClient(
            "token", session=session, corpus=corpus, authorised=lambda: {"12D3456"},
        )

        observation = client.recognise(self.path, trace_id="trace-off")

        self.assertEqual(observation.plate, "12D3456")
        self.assertEqual(observation.source, "cloud")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(corpus.records[0]["extra"]["local_ocr"], "off")
        self.assertIsNone(corpus.records[0]["local"])

    def test_shadow_mode_never_delays_the_decision(self):
        gate = Event()
        local = recognizer([read("12D3456", 0.99)], gate=gate)
        session = FakeSession([FakeResponse(cloud_payload("12D3456"))])
        client = self._client(local, session)

        observation = client.recognise(self.path, trace_id="trace-shadow")

        # The local read is still blocked inside the engine, and the cloud
        # answer has already come back and been returned to the processor.
        self.assertEqual(observation.plate, "12D3456")
        self.assertEqual(observation.source, "cloud")
        self.assertIsNone(local.summary("trace-shadow"))

        gate.set()
        for _ in range(200):
            if local.summary("trace-shadow"):
                break
            sleep(0.01)
        block = local.summary("trace-shadow")
        self.assertEqual(block["agreement"], "match")
        self.assertEqual(block["decision_source"], "cloud")
        local.close()

    def test_shadow_mode_writes_the_local_block_into_the_corpus_sidecar(self):
        local = recognizer([read("12D3456", 0.99)])
        session = FakeSession([FakeResponse(cloud_payload("12D3456"))])
        corpus = RecordingCorpus()
        client = self._client(local, session, corpus=corpus)

        client.recognise(self.path, trace_id="trace-corpus")

        record = corpus.records[0]
        self.assertEqual(record["extra"]["local_ocr"], "shadow")
        self.assertEqual(record["extra"]["cloud"], "requested")
        self.assertEqual(record["source"], "plate_recognizer")
        self.assertEqual(record["local"]["plate"], "12D3456")
        self.assertEqual(record["local"]["status"], "recognized")
        self.assertIn("total", record["local"]["latency_ms"])
        self.assertEqual(len(record["local"]["box"]), 4)
        local.close()

    def test_active_mode_answers_without_the_cloud_when_it_is_confident(self):
        logger = RecordingLogger()
        local = recognizer([read("12D3456", 0.99)], mode="active", logger=logger)
        session = FakeSession([])
        corpus = RecordingCorpus()
        client = self._client(local, session, corpus=corpus)

        observation = client.recognise(self.path, trace_id="trace-active")

        self.assertEqual(observation.plate, "12D3456")
        self.assertEqual(observation.source, "local")
        self.assertEqual(session.calls, [], "no Plate Recognizer lookup was spent")
        line = logger.line("active")
        self.assertIn("decision_source=local", line)
        self.assertIn("cloud_plate=-", line)
        self.assertIn("authorised=local_match", line)
        record = corpus.records[0]
        self.assertEqual(record["extra"]["cloud"], "skipped")
        self.assertEqual(record["source"], "local_recognizer")
        local.close()

    def test_a_read_below_the_threshold_falls_through_to_the_cloud(self):
        local = recognizer([read("12D3456", 0.40)], mode="active")
        session = FakeSession([FakeResponse(cloud_payload("12D3456"))])
        client = self._client(local, session)

        observation = client.recognise(self.path, trace_id="trace-low")

        self.assertEqual(observation.source, "cloud")
        self.assertEqual(len(session.calls), 1)
        local.close()

    def test_a_confident_but_unauthorised_read_falls_through_to_the_cloud(self):
        local = recognizer([read("99ZZ9999", 0.99)], mode="active")
        session = FakeSession([FakeResponse(cloud_payload("12D3456"))])
        client = self._client(local, session)

        observation = client.recognise(self.path, trace_id="trace-visitor")

        self.assertEqual(observation.plate, "12D3456")
        self.assertEqual(observation.source, "cloud")
        self.assertEqual(len(session.calls), 1)
        local.close()

    def test_a_local_failure_falls_through_to_the_cloud(self):
        local = recognizer([RuntimeError("inference exploded")], mode="active")
        session = FakeSession([FakeResponse(cloud_payload("12D3456"))])
        client = self._client(local, session)

        observation = client.recognise(self.path, trace_id="trace-broken")

        self.assertEqual(observation.plate, "12D3456")
        self.assertEqual(observation.source, "cloud")
        self.assertEqual(len(session.calls), 1)
        local.close()

    def test_cloud_always_labels_the_frame_and_still_lets_the_local_read_decide(self):
        logger = RecordingLogger()
        local = recognizer(
            [read("12D3456", 0.99)], mode="active", cloud="always", logger=logger,
        )
        session = FakeSession([FakeResponse(cloud_payload("12D3456", 0.93))])
        corpus = RecordingCorpus()
        client = self._client(local, session, corpus=corpus)

        observation = client.recognise(self.path, trace_id="trace-always")

        self.assertEqual(observation.source, "local", "the local read decided")
        self.assertEqual(len(session.calls), 1, "the frame was still labelled")
        line = logger.line("active")
        self.assertIn("decision_source=local", line)
        self.assertIn(
            "cloud_score=0.930", line,
            "the label answer reaches the journal line for comparison",
        )
        record = corpus.records[-1]
        self.assertEqual(record["extra"]["cloud"], "requested")
        self.assertEqual(record["local"]["plate"], "12D3456")
        local.close()

    def test_a_cloud_failure_cannot_undo_a_local_decision_in_always_mode(self):
        local = recognizer([read("12D3456", 0.99)], mode="active", cloud="always")
        session = FakeSession([FakeResponse({"results": []}, status_code=503)])
        client = self._client(local, session)

        observation = client.recognise(self.path, trace_id="trace-always-fail")

        self.assertEqual(observation.plate, "12D3456")
        self.assertEqual(
            observation.source, "local",
            "the label request failed; the decision was already made locally",
        )
        local.close()


    def test_a_stalled_local_read_leaves_the_cloud_the_budget_it_needs(self):
        # The guard runs on the OCR worker holding the OCR slot, so every
        # second it spends is a second the burst does not have. It waits for
        # what the budget can spare, not for its own ceiling.
        gate = Event()
        local = recognizer(
            [read("12D3456", 0.99)], mode="active", gate=gate,
        )
        session = FakeSession([FakeResponse(cloud_payload("12D3456"))])
        client = self._client(local, session)
        budget = LOCAL_DECISION_CLOUD_RESERVE_SECONDS + 0.4

        started = monotonic()
        observation = client.recognise(
            self.path, timeout=(1.5, 1.5), trace_id="t-stall", budget=budget,
        )
        elapsed = monotonic() - started
        gate.set()

        self.assertEqual(observation.source, "cloud")
        self.assertLess(
            elapsed, LOCAL_DECISION_TIMEOUT_SECONDS,
            "the guard stopped at the budget, not at its stuck-engine ceiling",
        )
        posted = session.calls[0]["timeout"]
        self.assertLessEqual(
            sum(posted), budget,
            "the request is sized for the budget left after the local wait",
        )
        self.assertTrue(all(value > 0 for value in posted))
        self.assertLess(
            local.event_wait_remaining("t-stall"), LOCAL_EVENT_WAIT_BUDGET_SECONDS,
            "the wait is charged to the event, so later frames pay less",
        )
        local.close()

    def test_a_frame_the_local_reader_cannot_answer_still_spends_its_lookup(self):
        # Frame two reads an unrelated plate. The event authorises on frame
        # one's read, so answering here would close and discard frame two's
        # cloud lookup and corrupt the source attribution.
        local = recognizer(
            [read("12D3456", 0.99), read("99ZZ9999", 0.99)], mode="active",
        )
        session = FakeSession([FakeResponse(cloud_payload("12D3456"))])
        client = self._client(local, session)

        first = client.recognise(self.path, trace_id="t-credit")
        second = client.recognise(self.path, trace_id="t-credit")

        self.assertEqual(first.source, "local")
        self.assertEqual(second.source, "cloud")
        self.assertEqual(
            len(session.calls), 1, "exactly the frame the local read declined"
        )
        local.close()

    def test_a_local_answer_never_reaches_an_abandoned_burst(self):
        # The cloud path has always refused to answer for an event the
        # processor gave up on. The local path, which can now wait, owes the
        # same invariant.
        gate = Event()
        tracker = ConcurrencyTracker()
        local = recognizer(
            [read("12D3456", 0.99)], mode="active", gate=gate, tracker=tracker,
        )
        client = self._client(local, FakeSession([]))
        outcome = []

        def call():
            try:
                outcome.append(client.recognise(
                    self.path, trace_id="t-abandoned", budget=60.0,
                ))
            except Exception as error:  # noqa: BLE001 - recorded and asserted
                outcome.append(error)

        worker = Thread(target=call, daemon=True)
        worker.start()
        for _ in range(500):
            if tracker.current:
                break
            sleep(0.01)
        client.abandon_in_flight()
        gate.set()
        worker.join(10)

        self.assertEqual(len(outcome), 1, outcome)
        self.assertIsInstance(outcome[0], OcrResponseError)
        self.assertEqual(outcome[0].failure_cause, "request_abandoned")
        local.close()


def _reject_constant(token):
    raise ValueError(f"non-standard JSON token: {token}")


class RealCorpusTests(unittest.TestCase):
    """Against the real TrainingCorpus, not a fake that records kwargs."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.path = self.root / "frame.jpg"
        Image.new("L", (64, 32), color=128).save(self.path, format="JPEG")

    def tearDown(self):
        self.directory.cleanup()

    def _sidecar(self, corpus):
        sidecars = sorted(Path(corpus.directory).glob("*.json"))
        self.assertTrue(sidecars, "a sidecar was written")
        return json.loads(sidecars[-1].read_text())

    def test_the_local_read_is_written_beside_the_cloud_read(self):
        local = recognizer([read("12D3456", 0.99)])
        corpus = TrainingCorpus(self.root / "corpus")
        client = PlateRecognizerClient(
            "token", session=FakeSession([FakeResponse(cloud_payload("12D3456"))]),
            local_recognizer=local, authorised=lambda: {"12D3456"}, corpus=corpus,
        )

        client.recognise(self.path, trace_id="trace-real")

        sidecar = self._sidecar(corpus)
        self.assertEqual(sidecar["ocr"]["plate"], "12D3456")
        self.assertEqual(sidecar["local"]["plate"], "12D3456")
        self.assertEqual(sidecar["local"]["status"], "recognized")
        self.assertEqual(sidecar["local"]["score"], 0.99)
        self.assertEqual(len(sidecar["local"]["box"]), 4)
        self.assertIn("total", sidecar["local"]["latency_ms"])
        self.assertEqual(sidecar["extra"]["local_ocr"], "shadow")
        local.close()

    def test_a_locally_decided_frame_is_kept_with_no_cloud_answer(self):
        local = recognizer([read("12D3456", 0.99)], mode="active")
        corpus = TrainingCorpus(self.root / "corpus")
        client = PlateRecognizerClient(
            "token", session=FakeSession([]), local_recognizer=local,
            authorised=lambda: {"12D3456"}, corpus=corpus,
        )

        client.recognise(self.path, trace_id="trace-real-local")

        sidecar = self._sidecar(corpus)
        self.assertEqual(sidecar["source"], "local_recognizer")
        self.assertEqual(sidecar["ocr"]["results"], [])
        self.assertEqual(sidecar["local"]["plate"], "12D3456")
        self.assertEqual(sidecar["extra"]["cloud"], "skipped")
        local.close()

    def test_a_non_finite_confidence_never_makes_a_sidecar_unreadable(self):
        # json.dumps would happily write NaN, which no strict reader parses.
        local = recognizer([read("12D3456", float("nan"))])
        corpus = TrainingCorpus(self.root / "corpus")
        client = PlateRecognizerClient(
            "token", session=FakeSession([FakeResponse(cloud_payload("12D3456"))]),
            local_recognizer=local, authorised=lambda: {"12D3456"}, corpus=corpus,
        )

        client.recognise(self.path, trace_id="trace-nan")

        raw = sorted(Path(corpus.directory).glob("*.json"))[-1].read_text()
        json.loads(raw, parse_constant=_reject_constant)
        self.assertNotIn("score", json.loads(raw)["local"])
        local.close()

    def test_a_frame_with_no_local_read_keeps_the_sidecar_it_always_had(self):
        corpus = TrainingCorpus(self.root / "corpus")
        client = PlateRecognizerClient(
            "token", session=FakeSession([FakeResponse(cloud_payload("12D3456"))]),
            corpus=corpus,
        )

        client.recognise(self.path)

        sidecar = self._sidecar(corpus)
        self.assertNotIn("local", sidecar)
        self.assertEqual(sidecar["ocr"]["plate"], "12D3456")


class RecordingRelay:
    def __init__(self):
        self.calls = []

    def trigger(self, source, idempotency_key=None, *, pre_activation_inhibit=None,
                on_activation=None):
        if pre_activation_inhibit is not None:
            inhibition = pre_activation_inhibit()
            if inhibition is not None:
                return RelayResult(False, inhibition[1], idempotency_key)
        self.calls.append(source)
        if on_activation is not None:
            on_activation()
        return RelayResult(True, "activated", idempotency_key)


class ProcessorDecisionTests(unittest.TestCase):
    """The local read goes through the controller's own decision function."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def _jpeg(self, name, colour):
        path = self.root / name
        Image.new("L", (16, 8), color=colour).save(path, format="JPEG")
        return path

    def _processor(self, client, authorised):
        return GateProcessor(
            recognizer=client,
            store=LocalStore(self.root / "gate.db"),
            relay=RecordingRelay(),
            authorised=authorised,
            cooldown=timedelta(seconds=20),
            clock=lambda: datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc),
        )

    def test_a_confident_authorised_local_read_opens_the_gate_as_source_local(self):
        local = recognizer([read("12D3456", 0.99)], mode="active")
        session = FakeSession([])
        client = PlateRecognizerClient(
            "token", session=session, local_recognizer=local,
            authorised=lambda: {"12D3456"},
        )
        processor = self._processor(client, {"12D3456"})

        result = processor.process((self._jpeg("event.jpg", 120),))

        self.assertTrue(result.opened)
        self.assertEqual(result.decision.reason, "exact_match")
        self.assertEqual(session.calls, [])
        stored = processor._store.event_payload(result.event_id)
        self.assertEqual(stored["source"], "local")
        telemetry = processor._store.event_telemetry(result.event_id)
        self.assertEqual(telemetry["local_ocr"]["decision_source"], "local")
        self.assertEqual(telemetry["local_ocr"]["authorised"], "local_match")
        local.close()

    def test_a_fuzzy_local_match_uses_the_same_two_frame_rule_as_the_cloud(self):
        # The recogniser reads S for 5 on both frames. Nothing about the local
        # path relaxes decide_access: it opens only under the same
        # two-frame confusion rule the cloud read has always used.
        local = recognizer(
            [read("11WH2S71", 0.99), read("11WH2S71", 0.99)], mode="active",
        )
        session = FakeSession([FakeResponse(cloud_payload("11WH2S71", 0.96))])
        client = PlateRecognizerClient(
            "token", session=session, local_recognizer=local,
            authorised=lambda: {"11WH2571"},
        )
        processor = self._processor(client, {"11WH2571"})

        result = processor.process((
            self._jpeg("first.jpg", 90), self._jpeg("second.jpg", 160),
        ))

        self.assertTrue(result.opened)
        self.assertEqual(result.decision.reason, "two_frame_ocr_confusion")
        self.assertEqual(result.decision.authorised_plate, "11WH2571")
        self.assertEqual(
            len(session.calls), 1,
            "the first frame had no authorised local answer yet, so it went to the cloud",
        )
        self.assertEqual(
            processor._store.event_payload(result.event_id)["source"], "local",
            "the observation that completed the decision was the local one",
        )
        local.close()

    def test_the_11_13_48_passage_now_opens_on_the_two_readers_agreeing(self):
        # The measured denial: local read 10CE1990 with a 0.566 per-character
        # minimum, cloud read 10CE1990 at 0.806, agreement=match, and the gate
        # stayed shut because each reader was judged alone against 0.90.
        local = recognizer(
            [read("10CE1990", 0.566, mean=0.945)], mode="active",
            min_confidence=0.5,
        )
        self.addCleanup(local.close)
        session = FakeSession([FakeResponse(cloud_payload("10CE1990", 0.806))])
        client = PlateRecognizerClient(
            "token", session=session, local_recognizer=local,
            authorised=lambda: {"10CE1990"},
        )
        processor = self._processor(client, {"10CE1990"})

        result = processor.process((self._jpeg("event.jpg", 120),))

        self.assertTrue(result.opened)
        self.assertEqual(result.decision.reason, "exact_match")
        self.assertEqual(result.decision.observed_plate, "10CE1990")

    def test_an_unauthorised_local_read_never_opens_the_gate(self):
        local = recognizer([read("99ZZ9999", 0.99)], mode="active")
        session = FakeSession([FakeResponse({"results": []})])
        client = PlateRecognizerClient(
            "token", session=session, local_recognizer=local,
            authorised=lambda: {"12D3456"},
        )
        processor = self._processor(client, {"12D3456"})

        result = processor.process((self._jpeg("visitor.jpg", 200),))

        self.assertFalse(result.opened)
        self.assertEqual(
            processor._store.event_payload(result.event_id)["source"], "ocr",
        )
        local.close()

    def test_the_processor_hands_the_recogniser_the_budget_it_has_left(self):
        # Without the budget the local guard cannot know what it may spend,
        # and the clamp in wait_seconds falls back to its own ceiling.
        class BudgetRecordingRecognizer:
            def __init__(self):
                self.budgets = []

            def recognise(self, path, timeout=None, trace_id=None, budget=None):
                self.budgets.append(budget)
                return PlateObservation(plate="12D3456", confidence=0.99)

        recogniser = BudgetRecordingRecognizer()
        processor = GateProcessor(
            recognizer=recogniser, store=LocalStore(self.root / "gate.db"),
            relay=RecordingRelay(), authorised={"12D3456"},
            decision_timeout=4.0,
            clock=lambda: datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc),
        )

        result = processor.process((self._jpeg("budget.jpg", 110),))

        self.assertTrue(result.opened)
        self.assertEqual(len(recogniser.budgets), 1)
        budget = recogniser.budgets[0]
        self.assertIsNotNone(budget, "the budget reached the recogniser")
        self.assertGreater(budget, 0.0)
        self.assertLessEqual(
            budget, 4.0, "and it is what is left, never more than the timeout"
        )

    def test_shadow_mode_can_never_reach_the_relay(self):
        local = recognizer([read("12D3456", 0.99)], mode="shadow")
        session = FakeSession([FakeResponse({"results": []})])
        client = PlateRecognizerClient(
            "token", session=session, local_recognizer=local,
            authorised=lambda: {"12D3456"},
        )
        relay = RecordingRelay()
        processor = GateProcessor(
            recognizer=client, store=LocalStore(self.root / "gate.db"),
            relay=relay, authorised={"12D3456"},
            clock=lambda: datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc),
        )

        result = processor.process((self._jpeg("shadow.jpg", 140),))

        self.assertFalse(result.opened)
        self.assertEqual(relay.calls, [], "shadow mode journals and nothing else")
        self.assertEqual(len(session.calls), 1)
        local.close()



class LocalPassTests(unittest.TestCase):
    """Requirement (a) at the client seam, and (b)'s corroboration source."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def _jpeg(self, name, colour=120):
        path = self.root / name
        Image.new("L", (16, 8), color=colour).save(path, format="JPEG")
        return path

    def test_an_authorised_local_read_answers_without_any_cloud_request(self):
        local = recognizer([read("12D3456", 0.99)], mode="active")
        self.addCleanup(local.close)
        session = FakeSession([])
        client = PlateRecognizerClient(
            "token", session=session, local_recognizer=local,
            authorised=lambda: {"12D3456"},
        )

        attempt = client.local_pass(self._jpeg("event.jpg"), trace_id="trace-1")

        self.assertTrue(attempt.decided)
        self.assertEqual(attempt.observation.plate, "12D3456")
        self.assertEqual(attempt.observation.source, "local")
        self.assertEqual(session.calls, [])

    def test_a_read_that_cannot_decide_carries_its_work_into_the_cloud_call(self):
        local = recognizer([read("99ZZ9999", 0.99)], mode="active")
        self.addCleanup(local.close)
        session = FakeSession([FakeResponse(cloud_payload("12D3456", 0.97))])
        client = PlateRecognizerClient(
            "token", session=session, local_recognizer=local,
            authorised=lambda: {"12D3456"},
        )
        path = self._jpeg("event.jpg")

        attempt = client.local_pass(path, trace_id="trace-1")

        self.assertFalse(attempt.decided)
        self.assertIsNotNone(attempt.state.get("upload_bytes"))
        self.assertIsNotNone(attempt.state.get("frame"))

        observation = client.recognise(path, trace_id="trace-1", attempt=attempt)

        self.assertEqual(observation.plate, "12D3456")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(
            local.status()["frames"], 1,
            "the frame was inferred once, not once per stage",
        )

    def test_the_always_mode_keeps_the_cloud_request_on_the_decision_path(self):
        # `always` exists to label every frame for the training corpus, and
        # the cloud request is deliberately on the decision path there. The
        # split must not silently change what that mode collects.
        local = recognizer([read("12D3456", 0.99)], mode="active", cloud="always")
        self.addCleanup(local.close)
        client = PlateRecognizerClient(
            "token", session=FakeSession([]), local_recognizer=local,
            authorised=lambda: {"12D3456"},
        )

        attempt = client.local_pass(self._jpeg("event.jpg"), trace_id="trace-1")

        self.assertFalse(attempt.decided)
        self.assertIsNone(attempt.state)

    def test_a_shadow_recogniser_never_answers_for_a_frame(self):
        local = recognizer([read("12D3456", 0.99)], mode="shadow")
        self.addCleanup(local.close)
        client = PlateRecognizerClient(
            "token", session=FakeSession([]), local_recognizer=local,
            authorised=lambda: {"12D3456"},
        )

        attempt = client.local_pass(self._jpeg("event.jpg"), trace_id="trace-1")

        self.assertFalse(attempt.decided)
        self.assertIsNone(attempt.state)

    def test_a_client_with_no_local_reader_has_nothing_to_pass(self):
        client = PlateRecognizerClient("token", session=FakeSession([]))

        attempt = client.local_pass(self._jpeg("event.jpg"), trace_id="trace-1")

        self.assertFalse(attempt.decided)
        self.assertIsNone(attempt.state)

    def test_an_abandoned_pass_still_journals_the_frame(self):
        local = recognizer([read("99ZZ9999", 0.99)], mode="active")
        self.addCleanup(local.close)
        logger = RecordingLogger()
        local._logger = logger
        client = PlateRecognizerClient(
            "token", session=FakeSession([]), local_recognizer=local,
            authorised=lambda: {"12D3456"},
        )

        attempt = client.local_pass(self._jpeg("event.jpg"), trace_id="trace-1")
        attempt.abandon()

        self.assertTrue(any(
            "gate_local_ocr stage=active" in line for line in logger.lines
        ), "a frame that never reached the cloud is still owed its line")


class LocalObservationTests(unittest.TestCase):
    """What the processor may use as corroboration, and what it may not."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def _jpeg(self, name, colour=120):
        path = self.root / name
        Image.new("L", (16, 8), color=colour).save(path, format="JPEG")
        return path

    def _client(self, local):
        return PlateRecognizerClient(
            "token", session=FakeSession([FakeResponse({"results": []})]),
            local_recognizer=local, authorised=lambda: {"10CE1990"},
        )

    def test_a_confident_read_is_offered_as_corroboration_for_its_own_event(self):
        local = recognizer(
            [read("10CE1990", 0.566)], mode="active", min_confidence=0.5,
        )
        self.addCleanup(local.close)
        client = self._client(local)

        client.local_pass(self._jpeg("event.jpg"), trace_id="trace-1")

        observations = client.local_observations("trace-1")
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].plate, "10CE1990")
        self.assertEqual(observations[0].source, "local")
        self.assertAlmostEqual(observations[0].confidence, 0.566)

    def test_another_events_reads_are_never_offered(self):
        local = recognizer(
            [read("10CE1990", 0.99)], mode="active", min_confidence=0.5,
        )
        self.addCleanup(local.close)
        client = self._client(local)

        client.local_pass(self._jpeg("event.jpg"), trace_id="trace-1")

        self.assertEqual(client.local_observations("trace-2"), ())
        self.assertEqual(client.local_observations(None), ())

    def test_a_read_below_the_local_gate_corroborates_nothing(self):
        # 2026-09-08 11:07:08: the on-device reader produced `131D26956` at a
        # 0.389 per-character minimum on the very frame the cloud read
        # `131D2696` correctly. It never entered the pool, and must not.
        local = recognizer(
            [read("131D26956", 0.389)], mode="active", min_confidence=0.5,
        )
        self.addCleanup(local.close)
        client = PlateRecognizerClient(
            "token", session=FakeSession([FakeResponse(cloud_payload("131D2696", 0.961))]),
            local_recognizer=local, authorised=lambda: {"131D2696"},
        )
        path = self._jpeg("event.jpg")

        attempt = client.local_pass(path, trace_id="trace-1")
        client.recognise(path, trace_id="trace-1", attempt=attempt)

        self.assertEqual(client.local_observations("trace-1"), ())

    def test_forgetting_an_event_drops_its_observations(self):
        local = recognizer(
            [read("10CE1990", 0.99)], mode="active", min_confidence=0.5,
        )
        self.addCleanup(local.close)
        client = self._client(local)

        client.local_pass(self._jpeg("event.jpg"), trace_id="trace-1")
        client.forget_local_ocr("trace-1")

        self.assertEqual(client.local_observations("trace-1"), ())


class ShippedDefaultAgreementTests(unittest.TestCase):
    """The agreement rule has to be reachable on what the controller ships.

    Every other test of the rule sets ``min_confidence=0.5`` by hand. With the
    admission gate at its old 0.95 default nothing under 0.95 ever reached the
    corroboration pool, so the shipped 0.50 agreement bar was unreachable and
    the rule could not run at all on a stock configuration.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(apply_confidence, {})
        apply_confidence({})

    def _jpeg(self, name="event.jpg", colour=120):
        path = self.root / name
        Image.new("L", (16, 8), color=colour).save(path, format="JPEG")
        return path

    def _recognizer(self, script, *, environment=None):
        config = load_local_recognizer_config(
            environment or {"GATE_LOCAL_OCR_MODE": "active"}
        )
        local = LocalRecognizer(
            config, engine_factory=engine_factory(script),
            logger=RecordingLogger(),
        )
        local.start()
        local.wait_ready(5)
        self.addCleanup(local.close)
        return local

    def _open(self, local, cloud_score=0.806):
        """Run the 11:13:48 event end to end and return the result."""
        session = FakeSession([FakeResponse(cloud_payload("10CE1990", cloud_score))])
        client = PlateRecognizerClient(
            "token", session=session, local_recognizer=local,
            authorised=lambda: {"10CE1990"},
        )
        processor = GateProcessor(
            recognizer=client,
            store=LocalStore(self.root / "gate.db"),
            relay=RecordingRelay(),
            authorised={"10CE1990"},
            cooldown=timedelta(seconds=20),
            clock=lambda: datetime(2026, 9, 8, 10, 13, 48, tzinfo=timezone.utc),
        )
        return processor.process((self._jpeg(),))

    def test_the_shipped_default_admits_the_11_13_48_event(self):
        # local 10CE1990 at 0.566, cloud the same plate at 0.806.
        result = self._open(self._recognizer([read("10CE1990", 0.566)]))

        self.assertTrue(result.opened)
        self.assertEqual(result.decision.reason, "exact_match")

    def test_agreement_opens_the_gate_when_neither_reader_could_alone(self):
        """The same event with the cloud read below the 0.75 exact bar.

        Nothing but the agreement rule can open this, so it fails outright
        with the admission gate above the agreement bar.
        """
        result = self._open(
            self._recognizer([read("10CE1990", 0.566)]), cloud_score=0.71,
        )

        self.assertTrue(result.opened)
        self.assertEqual(result.decision.reason, "exact_match")

    def test_the_shipped_default_lets_the_agreement_rule_run(self):
        """The local read reaches the pool, which is what 0.95 prevented."""
        local = self._recognizer([read("10CE1990", 0.566)])
        client = PlateRecognizerClient(
            "token", session=FakeSession([FakeResponse({"results": []})]),
            local_recognizer=local, authorised=lambda: {"10CE1990"},
        )

        client.local_pass(self._jpeg(), trace_id="trace-1")

        observations = client.local_observations("trace-1")
        self.assertEqual(
            [(item.plate, item.source) for item in observations],
            [("10CE1990", "local")],
        )
        # And the pair opens under the shipped bars even when the cloud read
        # alone is below the 0.75 exact bar: only agreement can carry this.
        self.assertTrue(decide_access(
            [PlateObservation("10CE1990", 0.71, source="cloud")],
            {"10CE1990"}, DEFAULT_POLICY, corroborations=observations,
        ).allowed)

    def test_the_old_default_would_have_kept_the_rule_unreachable(self):
        local = self._recognizer(
            [read("10CE1990", 0.566)],
            environment={
                "GATE_LOCAL_OCR_MODE": "active",
                "GATE_LOCAL_OCR_MIN_CONFIDENCE": "0.95",
            },
        )
        client = PlateRecognizerClient(
            "token", session=FakeSession([FakeResponse({"results": []})]),
            local_recognizer=local, authorised=lambda: {"10CE1990"},
        )

        client.local_pass(self._jpeg(), trace_id="trace-1")

        self.assertEqual(
            client.local_observations("trace-1"), (),
            "a 0.95 admission gate is a 0.95 floor under every agreement bar",
        )


if __name__ == "__main__":
    unittest.main()
