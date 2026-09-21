"""Farm machinery is let in by what it is -- through the path production takes.

Two recent bugs survived because a helper was unit-tested while nothing in the
live path called it. So the tests that matter here do not call the policy: they
call ``GateProcessor.process`` -- the function ``gate_controller.__main__``
hands to the worker for every captured burst -- with a real store and a real
actuation coordinator, and look at two things only: whether the relay was
asked, and what the access log was told.

The one thing stubbed is the model boundary: ``embed``, JPEG bytes in, image
embedding out. Everything after it is the shipped prompts, the shipped rule and
the shipped processor. The embeddings handed back are CLIP's own text vectors
for a prompt ("a photo of a tractor", "a photo of a lorry"), which is what a
perfectly unambiguous photo of that thing would embed next to; mixtures of two
of them make the unsure and the leaving cases. No numpy, no onnxruntime: this
file runs on the CI host that has neither.

Production reaches ``process`` two ways, and both are driven here. A burst with
no fast lane ahead of it is assessed after the plate path has declined
(``ThroughTheProcessor``). On the Pi the worker runs the fast lane --
``prepare`` on the burst thread, then ``process`` there or on the cloud lane --
and ``ThroughTheFastLane`` runs those real threads.

A machine is admitted only once it has been seen standing still: two clear
frames at least a second apart that look the same. So every admission below
takes two bursts, and ``waiting_machine`` is the pair.
"""
import json
import logging
import math
import os
import re
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from time import monotonic, sleep
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import gate_controller.__main__ as gate_main
from gate_controller import agricultural
from gate_controller.actuation import HUMAN_COMMAND_SOURCES
from gate_controller.agricultural import (
    Assessment, FarmMachineryPolicy, FrameScores, Hours, PromptScorer,
    build_policy, judge, load_mode,
)
from gate_controller.models import PlateObservation, RelayResult
from gate_controller.processor import GateProcessor
from gate_controller.store import LocalStore
from tests.test_event_contract import (
    assert_ingest_accepts_event, assert_ingest_accepts_telemetry,
)

MODELS = Path(agricultural.__file__).with_name("models")
_SPEC = json.loads((MODELS / "agri-clip-v1.json").read_text(encoding="utf-8"))
_DIRECTION = json.loads((MODELS / "direction-clip-v1.json").read_text(encoding="utf-8"))
_TEXT = dict(zip(_SPEC["prompts"], _SPEC["text_embeddings"]))
_DIRECTION_TEXT = dict(zip(_DIRECTION["prompts"], _DIRECTION["text_embeddings"]))


def _blend(*parts):
    vector = [sum(weight * row[i] for weight, row in parts) for i in range(len(parts[0][1]))]
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector]


TRACTOR = _TEXT["a photo of a tractor"]
TELEHANDLER = _TEXT["a telehandler with forks, a yellow farm loader"]
LORRY = _TEXT["a photo of a lorry"]
VAN = _TEXT["a white delivery van"]
JEEP_AND_TRAILER = _TEXT["a jeep pulling a horse box or livestock trailer"]
PICKUP = _TEXT["a pickup truck or 4x4 jeep"]
EMPTY = _TEXT["an empty gravel driveway beside a wooden fence"]
#: Machinery and a lorry in nearly equal measure: machinery is still the
#: argmax (0.70), which is exactly the case the margin exists to refuse.
TRACTOR_OR_LORRY = _blend((1.0, TRACTOR), (0.9, LORRY))
#: Unmistakably a tractor *and* unmistakably the back of something driving
#: away, so the only rule that can refuse it is the one about leaving.
TRACTOR_LEAVING = _blend(
    (1.0, TRACTOR),
    (0.7, _DIRECTION_TEXT["a car driving away from the camera, tail lights visible"]),
)

NOON = datetime(2026, 9, 21, 11, 0, tzinfo=timezone.utc)      # 12:00 in Dublin
MIDNIGHT = datetime(2026, 9, 21, 23, 30, tzinfo=timezone.utc)  # 00:30 in Dublin


class StubTower:
    """The model boundary. Hands back the next embedding; counts the asks."""

    def __init__(self, *answers):
        self._answers = list(answers)
        self.calls = 0

    def __call__(self, jpeg):
        self.calls += 1
        answer = self._answers[min(self.calls, len(self._answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer


class TowerByFrame:
    """The model boundary for threaded tests: each frame's bytes name its embedding."""

    def __init__(self):
        self._by_content = {}
        self.seen = []

    def shows(self, path, embedding):
        self._by_content[Path(path).read_bytes()] = embedding
        return path

    def __call__(self, jpeg):
        self.seen.append(jpeg)
        return self._by_content[jpeg]


class RecordingRelay:
    """The relay the coordinator drives. ``pulses`` is what the gate felt."""

    def __init__(self):
        self.pulses = []

    def trigger(self, source, idempotency_key=None, *, pre_activation_inhibit=None,
                on_activation=None):
        if pre_activation_inhibit is not None:
            inhibition = pre_activation_inhibit()
            if inhibition is not None:
                return RelayResult(False, inhibition[1], idempotency_key)
        self.pulses.append(source)
        if on_activation is not None:
            on_activation()
        return RelayResult(True, "activated", idempotency_key, NOON)


class PlateReader:
    """The plate path's reader: whatever plate it is told to see, or none."""

    def __init__(self, plate=None, confidence=0.0, error=None):
        self._observation = PlateObservation(plate, confidence)
        self._error = error
        self.calls = 0

    def recognise(self, path):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._observation


class ThroughTheProcessor(unittest.TestCase):
    """``GateProcessor.process`` is what a captured burst goes through."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.store = LocalStore(self.directory / "gate.db")
        self.relay = RecordingRelay()
        self._frames = 0

    def frame(self):
        self._frames += 1
        path = self.directory / f"frame-{self._frames}.jpg"
        Image.new("L", (16, 8), color=20 + 9 * self._frames).save(path, format="JPEG")
        return path

    def policy(self, mode, tower, **kwargs):
        return FarmMachineryPolicy(mode, tower, PromptScorer(), **kwargs)

    def processor(self, policy, reader=None, *, now=NOON, **kwargs):
        return GateProcessor(
            recognizer=reader or PlateReader(), store=self.store, relay=self.relay,
            authorised={"12D3456"}, cooldown=timedelta(seconds=20),
            clock=lambda: now, farm_machinery=policy, **kwargs,
        )

    def waiting_machine(self, processor, *, frames=1, now=NOON):
        """A machine seen two seconds ago, and again now: the second burst's result."""
        first = processor.process(
            tuple(self.frame() for _ in range(frames)), received_at=now - timedelta(seconds=2),
        )
        self.assertFalse(first.opened, "one sighting is not yet a machine that is waiting")
        return processor.process(tuple(self.frame() for _ in range(frames)), received_at=now)

    def logged(self, result):
        payload = self.store.event_payload(result.event_id)
        payload["controller_id"] = "primary"
        return payload

    def appearance(self, result):
        with closing(sqlite3.connect(self.directory / "gate.db")) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM event_appearance WHERE event_id = ?", (result.event_id,)
            ).fetchone()
        return None if row is None else dict(row)

    # -- on ---------------------------------------------------------------
    def test_a_waiting_tractor_with_no_plate_is_let_in_and_logged_as_farm_machinery(self):
        tower = StubTower(TRACTOR)
        result = self.waiting_machine(self.processor(self.policy("on", tower)))

        self.assertTrue(result.opened)
        self.assertEqual(self.relay.pulses, ["appearance"], "one pulse, through the coordinator")
        logged = self.logged(result)
        self.assertTrue(logged["opened"])
        self.assertEqual(logged["source"], "appearance")
        self.assertEqual(logged["reason"], "farm_machinery")
        self.assertIsNone(logged["authorised_plate"], "no plate was matched, so none is claimed")
        self.assertIsNone(logged["observed_plate"])
        self.assertIsNone(logged["ocr_confidence"])
        self.assertIsNotNone(logged["relay_activated_at"])
        kept = self.appearance(result)
        self.assertEqual((kept["mode"], kept["would_admit"], kept["opened"]), ("on", 1, 1))
        self.assertEqual(kept["verdict"], "machine_clear")

    def test_what_the_access_log_is_sent_passes_the_app_s_ingest_contract(self):
        result = self.waiting_machine(self.processor(self.policy("on", StubTower(TELEHANDLER))))
        telemetry = self.store.event_telemetry(result.event_id)

        assert_ingest_accepts_event(self.logged(result))
        assert_ingest_accepts_telemetry(telemetry)
        self.assertEqual(telemetry["decision"], {"outcome": "allowed", "reason": "farm_machinery"})
        self.assertEqual(telemetry["actuation"]["relay_outcome"], "activated")
        self.assertNotIn("appearance", telemetry, "the contract has no such block; it stays local")

    def test_a_tractor_whose_unlisted_plate_was_read_is_still_let_in_as_machinery(self):
        reader = PlateReader("152D9911", 0.874)
        result = self.waiting_machine(self.processor(self.policy("on", StubTower(TRACTOR)), reader))

        self.assertTrue(result.opened)
        logged = self.logged(result)
        self.assertEqual((logged["source"], logged["reason"]), ("appearance", "farm_machinery"))
        self.assertEqual(logged["observed_plate"], "152D9911", "what was read stays on the record")
        self.assertIsNone(logged["authorised_plate"], "and is not dressed up as a match")

    def test_an_agricultural_admit_respects_the_relay_cooldown(self):
        processor = self.processor(self.policy("on", StubTower(TRACTOR)))
        first = self.waiting_machine(processor)
        second = processor.process((self.frame(),), received_at=NOON)

        self.assertTrue(first.opened)
        self.assertEqual(second.reason, "cooldown")
        self.assertEqual(self.relay.pulses, ["appearance"], "the next frame did not pulse again")
        self.assertNotIn(
            "appearance", HUMAN_COMMAND_SOURCES,
            "it waits out the whole gate cycle, like a plate read, not the short window a person gets",
        )

    def test_an_admit_that_arrives_after_the_relay_deadline_is_inhibited_like_any_other(self):
        class LateAtTheRelay(RecordingRelay):
            """The decision clock passes the deadline between the grant and the GPIO."""

            def trigger(relay, source, idempotency_key=None, *, pre_activation_inhibit=None,
                        on_activation=None):
                clock["now"] = 100.0
                return RecordingRelay.trigger(
                    relay, source, idempotency_key,
                    pre_activation_inhibit=pre_activation_inhibit, on_activation=on_activation,
                )

        clock = {"now": 0.0}
        self.relay = LateAtTheRelay()
        processor = self.processor(
            self.policy("on", StubTower(TRACTOR)), decision_clock=lambda: clock["now"],
        )
        processor.process((self.frame(),), received_at=NOON - timedelta(seconds=2))
        clock["now"] = 0.0
        result = processor.process((self.frame(),), received_at=NOON)

        self.assertFalse(result.opened)
        self.assertEqual(result.reason, "decision_timeout")
        self.assertEqual(self.relay.pulses, [])

    # -- the plate path is untouched ----------------------------------------
    def test_a_known_plate_opens_exactly_as_before_and_the_model_is_never_asked(self):
        tower = StubTower(LORRY)
        with_policy = self.processor(self.policy("on", tower), PlateReader("12D3456", 0.95))
        result = with_policy.process((self.frame(),))

        self.assertTrue(result.opened)
        self.assertEqual(tower.calls, 0, "a plate grant never waits on the image model")
        self.assertEqual(self.relay.pulses, ["ocr"])
        logged = self.logged(result)
        self.assertEqual((logged["source"], logged["reason"]), ("ocr", "exact_match"))
        self.assertEqual(logged["authorised_plate"], "12D3456")
        self.assertIsNone(self.appearance(result), "nothing was assessed, so nothing is kept")

    def test_with_the_flag_off_the_event_is_what_it_always_was(self):
        baseline = self.processor(None).process((self.frame(),))
        off = self.processor(build_policy({"GATE_AGRI_ADMIT": "off"})).process((self.frame(),))

        self.assertIsNone(build_policy({}), "off is the default, and builds nothing")
        for result in (baseline, off):
            logged = self.logged(result)
            self.assertEqual((logged["opened"], logged["source"], logged["reason"]),
                             (False, "ocr", "no_match"))
            self.assertIsNone(self.appearance(result))
        self.assertEqual(self.relay.pulses, [])

    # -- every way of being unsure is the ordinary answer -------------------
    def assert_ordinary_refusal(self, result, *, verdict, reason="no_match"):
        self.assertFalse(result.opened)
        self.assertEqual(self.relay.pulses, [])
        logged = self.logged(result)
        self.assertEqual((logged["opened"], logged["source"], logged["reason"]),
                         (False, "ocr", reason))
        kept = self.appearance(result)
        self.assertEqual((kept["would_admit"], kept["opened"], kept["verdict"]), (0, 0, verdict))

    def test_a_lorry_is_not_let_in_however_long_it_waits(self):
        result = self.waiting_machine(self.processor(self.policy("on", StubTower(LORRY))))
        self.assert_ordinary_refusal(result, verdict="road_vehicle")

    def test_nor_is_a_van_a_pickup_or_a_jeep_with_a_trailer(self):
        for confuser in (VAN, PICKUP, JEEP_AND_TRAILER):
            result = self.waiting_machine(self.processor(self.policy("on", StubTower(confuser))))
            self.assert_ordinary_refusal(result, verdict="road_vehicle")

    def test_winning_the_argmax_is_not_enough(self):
        scores = PromptScorer().score(TRACTOR_OR_LORRY)
        self.assertGreater(scores.machine, scores.rival_score, "machinery is the likeliest answer")
        result = self.waiting_machine(self.processor(self.policy("on", StubTower(TRACTOR_OR_LORRY))))
        self.assert_ordinary_refusal(result, verdict="unsure")

    def test_an_empty_driveway_is_not_let_in(self):
        result = self.waiting_machine(self.processor(self.policy("on", StubTower(EMPTY))))
        self.assert_ordinary_refusal(result, verdict="unsure")

    def test_a_machine_the_direction_prompts_read_as_leaving_is_not_opened_for(self):
        scores = PromptScorer().score(TRACTOR_LEAVING)
        self.assertGreater(scores.margin, 0.99, "nothing but the direction rule can refuse this")
        result = self.waiting_machine(self.processor(self.policy("on", StubTower(TRACTOR_LEAVING))))
        self.assert_ordinary_refusal(result, verdict="leaving")

    def test_a_machine_seen_once_is_not_opened_for(self):
        """Direction unknown, and nothing yet to say it is waiting rather than leaving."""
        result = self.processor(self.policy("on", StubTower(TRACTOR))).process((self.frame(),))
        self.assert_ordinary_refusal(result, verdict="not_standing_still")

    def test_a_machine_that_keeps_moving_is_not_opened_for(self):
        """The departing tractor of 2026-09-08: clear in every frame, never the same twice."""
        self.assertLess(agricultural._cosine(TRACTOR, TELEHANDLER), 0.95)
        tower = StubTower(TRACTOR, TELEHANDLER)
        result = self.waiting_machine(self.processor(self.policy("on", tower)))
        self.assert_ordinary_refusal(result, verdict="not_standing_still")

    def test_two_looks_less_than_a_second_apart_do_not_count_as_waiting(self):
        processor = self.processor(self.policy("on", StubTower(TRACTOR)))
        processor.process((self.frame(),), received_at=NOON - timedelta(seconds=0.7))
        result = processor.process((self.frame(),), received_at=NOON)
        self.assert_ordinary_refusal(result, verdict="not_standing_still")

    def test_a_machine_from_another_passage_does_not_count_as_waiting(self):
        policy = self.policy("on", StubTower(TRACTOR))
        earlier = NOON - timedelta(seconds=40)
        self.processor(policy, now=earlier).process((self.frame(),), received_at=earlier)
        result = self.processor(policy).process((self.frame(),), received_at=NOON)
        self.assert_ordinary_refusal(result, verdict="not_standing_still")

    def test_a_missing_model_is_the_normal_path(self):
        policy = build_policy(
            {"GATE_AGRI_ADMIT": "on"}, model_dir=self.directory / "no-models-here",
        )
        self.assertIn("not installed", policy.describe())
        result = self.waiting_machine(self.processor(policy))
        self.assert_ordinary_refusal(result, verdict="model_unavailable")

    def test_a_model_that_raises_is_the_normal_path(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        tower = StubTower(RuntimeError("onnxruntime fell over"))
        result = self.waiting_machine(self.processor(self.policy("on", tower)))
        self.assert_ordinary_refusal(result, verdict="error")

    def test_a_frame_the_model_cannot_read_is_the_normal_path(self):
        result = self.waiting_machine(self.processor(self.policy("on", StubTower(None))))
        self.assert_ordinary_refusal(result, verdict="unreadable")

    def test_a_model_that_overruns_is_not_waited_for(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def wedged(jpeg):
            release.wait(10)
            return TRACTOR

        with patch.object(agricultural, "MAX_BUDGET_SECONDS", 0.05):
            result = self.processor(self.policy("on", wedged)).process((self.frame(),))
        self.assert_ordinary_refusal(result, verdict="timeout")

    def test_a_plate_error_still_lets_a_waiting_tractor_in_but_never_a_lorry(self):
        failing = PlateReader(error=RuntimeError("cloud down"))
        lorry = self.waiting_machine(self.processor(self.policy("on", StubTower(LORRY)), failing))
        self.assert_ordinary_refusal(lorry, verdict="road_vehicle", reason="ocr_error")
        tractor = self.waiting_machine(self.processor(self.policy("on", StubTower(TRACTOR)), failing))
        self.assertTrue(tractor.opened)

    def test_outside_the_hours_a_machine_is_recorded_and_not_let_in(self):
        processor = self.processor(self.policy("on", StubTower(TRACTOR)), now=MIDNIGHT)
        result = self.waiting_machine(processor, now=MIDNIGHT)
        self.assert_ordinary_refusal(result, verdict="outside_hours")

    # -- more than one frame -------------------------------------------------
    def test_two_frames_that_agree_let_it_in(self):
        tower = StubTower(TRACTOR)
        result = self.waiting_machine(self.processor(self.policy("on", tower)), frames=2)
        self.assertTrue(result.opened)
        self.assertEqual(tower.calls, 4, "both frames of both bursts were read")
        self.assertEqual(self.appearance(result)["frames"], 2)

    def test_two_frames_that_disagree_do_not(self):
        for second, verdict in ((LORRY, "road_vehicle"), (EMPTY, "frames_disagree")):
            tower = StubTower(TRACTOR, second, TRACTOR, second)
            result = self.waiting_machine(self.processor(self.policy("on", tower)), frames=2)
            self.assert_ordinary_refusal(result, verdict=verdict)

    # -- shadow ----------------------------------------------------------------
    def test_shadow_records_would_admit_with_scores_and_never_pulses(self):
        tower = StubTower(TRACTOR)
        with self.assertLogs("gate_controller.processor", level="INFO") as journal:
            result = self.waiting_machine(self.processor(self.policy("shadow", tower)))

        self.assertEqual(tower.calls, 2, "shadow really does run the model")
        self.assertFalse(result.opened)
        self.assertEqual(self.relay.pulses, [], "shadow never reaches the relay")
        logged = self.logged(result)
        self.assertEqual((logged["opened"], logged["source"], logged["reason"]),
                         (False, "ocr", "no_match"), "the app is told what it was always told")
        kept = self.appearance(result)
        self.assertEqual((kept["mode"], kept["would_admit"], kept["opened"]), ("shadow", 1, 0))
        self.assertGreater(kept["margin"], 0.99)
        detail = json.loads(kept["detail"])
        self.assertEqual(detail["frames"][0]["rival"], "lorry")
        self.assertEqual((detail["stillness"], detail["still_seconds"]), (1.0, 2.0))
        line = [line for line in journal.output if "gate_agri" in line][-1]
        self.assertIn("mode=shadow would_admit=true verdict=machine_clear", line)
        self.assertRegex(line, r"machine=1\.000 margin=\+1\.000 .* stillness=1\.000 still_seconds=2\.0")

    def test_shadow_records_the_lorries_it_would_have_refused_too(self):
        result = self.processor(self.policy("shadow", StubTower(LORRY))).process((self.frame(),))
        self.assert_ordinary_refusal(result, verdict="road_vehicle")


class ThroughTheFastLane(unittest.TestCase):
    """The worker's real burst thread and cloud lane, as ``run_worker`` wires them.

    This is the path the Pi runs: ``prepare`` takes the on-device plate read
    and begins the machinery reading the moment a frame lands; a frame the
    device decided never reaches the model, and one the policy admits never
    reaches the cloud.
    """

    def setUp(self):
        from tests.test_fast_lane import Lanes, wait_for
        from tests.test_processor import TwoPhaseRecognizer

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.store = LocalStore(self.directory / "gate.db")
        self.relay = RecordingRelay()
        self.tower = TowerByFrame()
        self.Lanes, self.wait_for, self.TwoPhaseRecognizer = Lanes, wait_for, TwoPhaseRecognizer
        self._frames = 0

    def frame(self, embedding):
        self._frames += 1
        path = self.directory / f"frame-{self._frames}.jpg"
        Image.new("L", (16, 8), color=20 + 9 * self._frames).save(path, format="JPEG")
        return self.tower.shows(path, embedding)

    def lanes(self, mode, recognizer):
        processor = GateProcessor(
            recognizer=recognizer, store=self.store, relay=self.relay,
            authorised={"131D2696"}, cooldown=timedelta(seconds=20),
            clock=lambda: datetime.now(timezone.utc), decision_timeout=7.0,
            farm_machinery=FarmMachineryPolicy(mode, self.tower, PromptScorer(),
                                               hours=Hours.parse("00:00-24:00")),
        )
        lanes = self.Lanes(processor)
        self.addCleanup(lanes.close)
        return lanes

    def passage(self, lanes, first, second):
        """Two frames of one vehicle, captured two seconds apart, the older first."""
        from gate_controller.worker import BurstIdentity
        from time import monotonic

        now = datetime.now(timezone.utc)
        results = []
        for path, captured in ((first, now - timedelta(seconds=2)), (second, now)):
            lanes.bursts.put(((path,), captured, monotonic(), captured, BurstIdentity(path.name)))
            self.assertTrue(self.wait_for(lambda: lanes.result_for(path) is not None))
            results.append(lanes.result_for(path)[1])
        return results

    def logged(self, result):
        return self.store.event_payload(result.event_id)

    def kept(self, result):
        with closing(sqlite3.connect(self.directory / "gate.db")) as connection:
            return connection.execute(
                "SELECT mode, would_admit, opened, verdict FROM event_appearance WHERE event_id = ?",
                (result.event_id,),
            ).fetchone()

    def test_on_a_waiting_telehandler_is_let_in_without_a_cloud_lookup(self):
        reader = self.TwoPhaseRecognizer()
        first, second = self.frame(TELEHANDLER), self.frame(TELEHANDLER)
        seen_once, waiting = self.passage(self.lanes("on", reader), first, second)

        self.assertFalse(seen_once.opened)
        self.assertTrue(waiting.opened)
        self.assertEqual(self.relay.pulses, ["appearance"])
        self.assertEqual(reader.cloud_calls, [first], "no lookup is bought for a machine with no plate")
        logged = self.logged(waiting)
        self.assertEqual((logged["opened"], logged["source"], logged["reason"]),
                         (True, "appearance", "farm_machinery"))
        self.assertEqual(self.kept(waiting), ("on", 1, 1, "machine_clear"))
        self.assertEqual(self.kept(seen_once), ("on", 0, 0, "not_standing_still"))

    def test_shadow_changes_nothing_the_gate_or_the_app_can_see(self):
        reader = self.TwoPhaseRecognizer()
        first, second = self.frame(TELEHANDLER), self.frame(TELEHANDLER)
        _seen_once, waiting = self.passage(self.lanes("shadow", reader), first, second)

        self.assertFalse(waiting.opened)
        self.assertEqual(self.relay.pulses, [])
        self.assertEqual(reader.cloud_calls, [first, second], "both still go to the plate path")
        logged = self.logged(waiting)
        self.assertEqual((logged["opened"], logged["source"], logged["reason"]),
                         (False, "ocr", "no_match"))
        self.assertEqual(self.kept(waiting), ("shadow", 1, 0, "machine_clear"))

    def test_a_plate_the_device_reads_opens_as_before_and_never_reaches_the_model(self):
        reader = self.TwoPhaseRecognizer(local_plate="131D2696", local_confidence=0.99)
        first, second = self.frame(LORRY), self.frame(LORRY)
        opened, _cooldown = self.passage(self.lanes("on", reader), first, second)

        self.assertTrue(opened.opened)
        self.assertEqual(self.relay.pulses, ["local"])
        self.assertEqual(self.tower.seen, [], "a frame the device decided is never shown to the model")
        self.assertEqual(self.logged(opened)["reason"], "exact_match")
        self.assertIsNone(self.kept(opened))

    def test_a_plate_the_cloud_reads_still_opens_with_the_policy_on(self):
        reader = self.TwoPhaseRecognizer(cloud_observation=PlateObservation("131D2696", 0.97))
        first, second = self.frame(PICKUP), self.frame(PICKUP)
        opened, _cooldown = self.passage(self.lanes("on", reader), first, second)

        self.assertTrue(opened.opened)
        self.assertEqual(self.relay.pulses, ["ocr"])
        logged = self.logged(opened)
        self.assertEqual((logged["source"], logged["reason"], logged["authorised_plate"]),
                         ("ocr", "exact_match", "131D2696"))

    def test_a_departing_machine_and_a_waiting_lorry_go_to_the_plate_path_and_stay_shut(self):
        for first, second in ((TRACTOR, TELEHANDLER), (LORRY, LORRY)):
            reader = self.TwoPhaseRecognizer()
            one, two = self.frame(first), self.frame(second)
            results = self.passage(self.lanes("on", reader), one, two)
            self.assertEqual([result.opened for result in results], [False, False])
            self.assertEqual(reader.cloud_calls, [one, two])
        self.assertEqual(self.relay.pulses, [])


class TheLiveCallSite(unittest.TestCase):
    """Proof by reading the source that production reaches the policy."""

    ROOT = Path(agricultural.__file__).parent

    def test_process_consults_the_policy_and_main_builds_it(self):
        processor = (self.ROOT / "processor.py").read_text(encoding="utf-8")

        def body(name, until):
            return processor[processor.index(f"    def {name}("):processor.index(f"    def {until}(")]

        prepare = body("prepare", "_prepare_local_pass")
        process = body("process", "_begin_farm_machinery")
        self.assertIn("self._begin_farm_machinery(prepared, deadline)", prepare)
        self.assertIn("self._assess_farm_machinery(", process)
        self.assertEqual(
            process.count("self._coordinator.actuate("), 1,
            "one actuation call: plate grants and appearance grants share it",
        )
        self.assertNotIn("relay", body("_begin_farm_machinery", "_keep_appearance").lower().replace(
            "relay's own deadline", ""), "the assessment itself never touches the relay")
        self.assertIn("policy.begin(", processor)
        self.assertIn("pending.result(budget)", processor)
        main = (self.ROOT / "__main__.py").read_text(encoding="utf-8")
        self.assertRegex(main, r"farm_machinery=build_farm_machinery_policy\(os\.environ\)")
        self.assertRegex(main, r"processor\.prepare\(")
        self.assertRegex(main, r"processor\.process\(")

    def test_main_hands_the_processor_the_policy_the_environment_asks_for(self):
        from tests.test_main import no_live_state_access

        for setting, expected in (("shadow", "shadow"), ("on", "on"), (None, None), ("yes", None)):
            state_directory = tempfile.TemporaryDirectory()
            self.addCleanup(state_directory.cleanup)
            state = Path(state_directory.name)
            environment = {
                "PLATE_RECOGNIZER_API_TOKEN": "token",
                "GATE_DATABASE": str(state / "gate-controller.db"),
                "GATE_AUTHORISED_PLATES": str(state / "authorised_licence_plates.csv"),
                "GATE_WATCH_DIRECTORY": str(state / "uploads"),
                "GATE_MODEL_DIR": str(state / "no-models-here"),
            }
            if setting is not None:
                environment["GATE_AGRI_ADMIT"] = setting
            with patch.dict(os.environ, environment, clear=True), patch("sys.argv", [
                "gate-controller",
            ]), patch.object(gate_main, "require_python_version"), patch.object(
                gate_main, "PiRelayAdapter", return_value=object(),
            ), patch.object(gate_main, "RelayController"), patch.object(
                gate_main, "LocalStore",
            ), patch.object(gate_main, "AuthorisedPlateCache"), patch.object(
                gate_main, "build_background_workers",
                return_value=((), object(), object()),
            ), patch.object(
                gate_main, "PlateRecognizerClient", return_value=object(),
            ), patch.object(
                gate_main, "GateProcessor", return_value=object(),
            ) as built, patch.object(gate_main, "run_worker"), no_live_state_access():
                logging.disable(logging.CRITICAL)
                try:
                    gate_main.main()
                finally:
                    logging.disable(logging.NOTSET)
            policy = built.call_args.kwargs["farm_machinery"]
            self.assertEqual(None if policy is None else policy.mode, expected, setting)


class TheRule(unittest.TestCase):
    ROADS = ("car", "van", "lorry", "pickup", "trailer")

    def frame(self, machine, rival_score, *, rival="lorry", direction="unknown"):
        on_the_road = rival in self.ROADS
        return FrameScores(machine=machine, rival=rival, rival_score=rival_score,
                           road=rival if on_the_road else "lorry",
                           road_score=rival_score if on_the_road else 0.0,
                           direction=direction)

    def test_the_machines_in_the_stored_photos_clear_the_shipped_threshold(self):
        # Pi scores. Events 3060/3061, the telehandler at 14:55 on 2026-09-19;
        # 2507, the tractor with a loader on 09-07; 2635, the furthest-off
        # frame of the tractor on 09-08 that was still let in.
        for machine, rival_score, rival in ((0.9999, 0.0, "plant"), (0.9177, 0.0696, "other"),
                                            (0.7811, 0.1305, "trailer")):
            self.assertEqual(judge([self.frame(machine, rival_score, rival=rival)]),
                             (True, "machine_clear"))

    def test_nothing_that_was_not_a_machine_came_near_it(self):
        # The highest "machinery" share on any of 890 other frames was 0.164
        # (a corrupted frame); the highest on a real road vehicle, a pickup, 0.072.
        self.assertEqual(judge([self.frame(0.1637, 0.6878, rival="car")]), (False, "road_vehicle"))
        self.assertEqual(judge([self.frame(0.0721, 0.7568, rival="pickup")]), (False, "road_vehicle"))

    def test_a_margin_short_of_the_threshold_is_unsure(self):
        self.assertEqual(judge([self.frame(0.74, 0.05)]), (False, "unsure"))
        self.assertEqual(judge([self.frame(0.80, 0.19, rival="plant")]), (True, "machine_clear"))
        self.assertEqual(judge([self.frame(0.79, 0.21, rival="plant")]), (False, "unsure"))

    def test_leaving_is_refused_before_anything_else_is_asked(self):
        self.assertEqual(judge([self.frame(1.0, 0.0, direction="exiting")]), (False, "leaving"))
        self.assertEqual(judge([self.frame(1.0, 0.0, direction="entering")]), (True, "machine_clear"))
        self.assertEqual(judge([self.frame(1.0, 0.0), self.frame(1.0, 0.0, direction="exiting")]),
                         (False, "leaving"))

    def test_no_frames_is_no(self):
        self.assertEqual(judge([]), (False, "unreadable"))

    def test_the_prompts_name_every_road_vehicle_that_must_not_be_let_in(self):
        kinds = _SPEC["kinds"]
        self.assertEqual({label for label, kind in kinds.items() if kind == "machine"}, {"machine"})
        self.assertLessEqual({"car", "van", "lorry", "pickup", "trailer"},
                             {label for label, kind in kinds.items() if kind == "road"})

    def test_garbage_from_the_model_is_refused_not_scored(self):
        scorer = PromptScorer()
        for bad in ([0.0] * 3, [float("nan")] * 512):
            with self.assertRaises(ValueError):
                scorer.score(bad)


class TheFlag(unittest.TestCase):
    def test_only_the_three_words_mean_anything(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.assertEqual(load_mode({}), "off")
        for raw, mode in (("off", "off"), ("shadow", "shadow"), (" ON ", "on"),
                          ("true", "off"), ("1", "off"), ("enabled", "off")):
            self.assertEqual(load_mode({"GATE_AGRI_ADMIT": raw}), mode, raw)

    def test_a_threshold_cannot_be_configured_below_a_half(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        policy = build_policy({
            "GATE_AGRI_ADMIT": "shadow", "GATE_AGRI_ADMIT_MIN_MARGIN": "0.1",
            "GATE_AGRI_ADMIT_MIN_MACHINE": "banana", "GATE_AGRI_ADMIT_STILL_COSINE": "2",
            "GATE_MODEL_DIR": "/nonexistent",
        })
        self.assertIn("min_margin=0.60 min_machine=0.75 still_cosine=0.95", policy.describe())

    def test_shadow_is_consulted_and_never_admits(self):
        shadow = FarmMachineryPolicy("shadow", StubTower(TRACTOR), PromptScorer())
        self.assertTrue(shadow.consulted)
        self.assertFalse(shadow.admits)
        self.assertFalse(FarmMachineryPolicy("sideways", None, PromptScorer()).consulted)


class TheHours(unittest.TestCase):
    def test_the_default_window_is_dublin_time_not_utc(self):
        hours = Hours.parse("06:00-22:00")
        # 21:30 UTC in September is 22:30 in Dublin: shut.
        self.assertFalse(hours.open_at(datetime(2026, 9, 21, 21, 30, tzinfo=timezone.utc)))
        self.assertTrue(hours.open_at(datetime(2026, 9, 21, 20, 30, tzinfo=timezone.utc)))
        # In January Dublin is on UTC.
        self.assertTrue(hours.open_at(datetime(2026, 1, 15, 21, 30, tzinfo=timezone.utc)))

    def test_always_and_never_and_over_midnight(self):
        moment = datetime(2026, 9, 21, 2, 0, tzinfo=timezone.utc)
        self.assertTrue(Hours.parse("00:00-24:00").open_at(moment))
        self.assertFalse(Hours.parse("09:00-09:00").open_at(moment))
        self.assertTrue(Hours.parse("20:00-05:00").open_at(moment))

    def test_nonsense_keeps_the_default(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        policy = build_policy({
            "GATE_AGRI_ADMIT": "shadow", "GATE_AGRI_ADMIT_HOURS": "whenever",
            "GATE_MODEL_DIR": "/nonexistent",
        })
        self.assertIn("hours=06:00-22:00", policy.describe())


class TheAssessment(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.frame = Path(directory.name) / "frame.jpg"
        Image.new("L", (16, 8), color=90).save(self.frame, format="JPEG")
        self.release, self.started = threading.Event(), threading.Event()
        self.addCleanup(self.release.set)

    def wedged(self, jpeg):
        self.started.set()
        self.release.wait(10)
        return TRACTOR

    def test_a_second_burst_does_not_queue_behind_a_wedged_model(self):
        policy = FarmMachineryPolicy("on", self.wedged, PromptScorer())
        with patch.object(agricultural, "MAX_BUDGET_SECONDS", 0.05):
            first = policy.assess((self.frame,), budget_seconds=1.0, at=NOON)
        self.assertTrue(self.started.is_set())
        second = policy.assess((self.frame,), budget_seconds=1.0, at=NOON)
        self.assertEqual((first.verdict, second.verdict), ("timeout", "busy"))
        self.assertFalse(first.would_admit or second.would_admit)

    def test_beginning_a_reading_never_waits_and_collecting_it_is_bounded(self):
        policy = FarmMachineryPolicy("on", self.wedged, PromptScorer())
        began = monotonic()
        pending = policy.begin((self.frame,), at=NOON)
        self.assertLess(monotonic() - began, 0.5, "begin returned while the model was still busy")
        assessment = pending.result(0.01)
        self.assertEqual((assessment.verdict, assessment.would_admit), ("no_budget", False))
        self.release.set()
        self.assertIs(pending.result(5.0), assessment, "an answer, once given, does not change")

    def test_a_reading_that_has_finished_is_collected_without_any_budget(self):
        policy = FarmMachineryPolicy("on", StubTower(LORRY), PromptScorer())
        pending = policy.begin((self.frame,), at=NOON)
        deadline = monotonic() + 5
        while policy._running.locked() and monotonic() < deadline:
            sleep(0.005)
        self.assertEqual(pending.result(0.0).verdict, "road_vehicle")

    def test_the_journal_line_carries_no_path_and_no_plate(self):
        line = Assessment(True, "machine_clear", (FrameScores(0.98, "plant", 0.02, "lorry", 0.0),)).journal()
        self.assertTrue(re.fullmatch(r"[A-Za-z0-9_=+.:\- ]+", line), line)


if __name__ == "__main__":
    unittest.main()
