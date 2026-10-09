"""2026-10-06 10:06: the D-Max waited ~50 s and nothing said it was the D-Max.

The silver pickup ``172L66`` stopped at the gate and the device read it as
``172L61`` (0.61), ``172L68`` and ``172L88`` while the cloud was not asked --
the sweep was reading, or the link was down. A frame whose cloud request is
skipped answers "no plate" on purpose: offered as an observation, the device's
own read could corroborate itself, and a skip must never open the gate. The
cost was that the misread was never compared with the list, so the passage
page could not say "closest authorised plate 172L66".

The refused read now rides along on the skipped frame's observation as
``review_plate``, which matching consults only after every rule has refused,
and only to fill ``near_miss_*``. These tests drive it the way production
does -- the alarm, the sweep, the burst queue, the processor, the coordinator
and the relay, or the camera's still through the fast lane -- and check the
two halves together: the gate stays shut and nothing is posted, and the denial
names the plate the read nearly was.
"""
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gate_controller.local_recognizer import LocalRecognizer, LocalRecognizerConfig
from gate_controller.local_sweep import crop_to_region
from gate_controller.matching import decide_access
from gate_controller.models import MatchDecision, PlateObservation
from gate_controller.ocr import PlateRecognizerClient
from gate_controller.processor import GateProcessor
from gate_controller.store import LocalStore
from gate_controller.telemetry import MatchPolicyTelemetry
from tests import test_fast_lane as fast_lane
from tests.test_internet_down_pipeline import DeadLinkCloud, Link, probed_gate
from tests.test_local_pass_reserve import SlowEngine
from tests.test_local_recognizer import FakeSession
from tests.test_processor import RecordingRelay
from tests.test_sweep_pipeline import REGION, CapturedLogs, digest, frame, wait_for

DMAX = "172L66"
DMAX_MISREAD = "172L61"


def _policy(result):
    telemetry = result.telemetry
    if telemetry is None or telemetry.match_policy is None:
        return None
    return telemetry.match_policy.to_wire()


class SweepWithTheCloudDownTests(unittest.TestCase):
    """The waiting D-Max, the internet down: the sweep reads, the cloud is not asked."""

    def setUp(self):
        self.gate = None

    def tearDown(self):
        if self.gate is not None:
            self.gate.close()

    def _passage(self, answers_for, *, seeds):
        link = Link(up=False)
        self.assertEqual(link.measure(), "failed")
        frames = [frame(seed) for seed in seeds]
        answers = {
            digest(crop_to_region(data, REGION)): answers_for(index)
            for index, data in enumerate(frames)
        }
        cloud = DeadLinkCloud()
        self.gate = probed_gate(
            self, link, answers=answers, cloud=cloud, sweep_seconds=1.5,
            cloud_frames=5, fallback=1, authorised={DMAX},
        )
        gate = self.gate
        with CapturedLogs() as logs:
            gate.frames.script([(0.05 + index * 0.12, data) for index, data in enumerate(frames)])
            gate.alarm()
            self.assertTrue(
                wait_for(lambda: "gate_local_sweep outcome=ended" in logs.text(), 10.0),
                f"the sweep never ended:\n{logs.text()}",
            )
            self.assertTrue(
                wait_for(lambda: gate.outcomes(), 8.0),
                f"the passage was never decided:\n{logs.text()}",
            )
        return gate, cloud, logs

    def test_a_misread_one_character_off_still_denies_and_names_the_dmax(self):
        gate, cloud, logs = self._passage(
            lambda index: (DMAX_MISREAD, 0.61), seeds=range(500, 512),
        )

        self.assertEqual(gate.relay_calls, [], "a refused read opened the gate")
        self.assertEqual(cloud.posted_at, [], "a request left over a link the probe said was dead")
        self.assertEqual(gate.outcomes(), [(False, "no_match")])
        with gate._lock:
            result = gate.results[0][2]
        self.assertIsNone(gate.stored(result)["observed_plate"],
                          "the event row must not claim a read nothing stood on")
        policy = _policy(result)
        self.assertIsNotNone(policy, "the denial carried no match policy")
        self.assertEqual(policy["near_miss_plate"], DMAX)
        self.assertEqual(policy["near_miss_distance"], 1)
        self.assertEqual(policy["observed_plate"], DMAX_MISREAD)
        self.assertNotIn("authorised_plate", policy)
        self.assertNotIn("rule", policy)
        self.assertIn("gate_ocr stage=cloud_skipped reason=internet_down", logs.text())

    def test_a_confident_misread_is_still_only_a_near_miss(self):
        """0.99 on every frame: confidence buys nothing for a plate that is not on the list."""
        gate, cloud, _logs = self._passage(
            lambda index: (DMAX_MISREAD, 0.99), seeds=range(520, 532),
        )

        self.assertEqual(gate.relay_calls, [])
        self.assertEqual(cloud.posted_at, [])
        self.assertTrue(all(not opened for opened, _reason in gate.outcomes()), gate.outcomes())
        with gate._lock:
            policies = [_policy(result) for _at, _paths, result in gate.results]
        self.assertTrue(
            any(policy and policy.get("near_miss_plate") == DMAX for policy in policies),
            policies,
        )

    def test_a_strangers_plate_read_with_the_cloud_skipped_records_nothing_new(self):
        """No near miss, no read on the record: the session ends exactly as before."""
        gate, cloud, logs = self._passage(
            lambda index: ("99D12345", 0.99), seeds=range(540, 552),
        )

        self.assertEqual(gate.relay_calls, [])
        self.assertEqual(cloud.posted_at, [])
        with gate._lock:
            results = [result for _at, _paths, result in gate.results]
        for result in results:
            self.assertIsNone(result.decision.observed_plate if result.decision else None)
            policy = _policy(result) or {}
            self.assertNotIn("near_miss_plate", policy)
            self.assertNotIn("observed_plate", policy)
        self.assertNotIn("reason=plate_denied", logs.text(),
                         "a read the cloud never saw concluded a different car")


class CameraStillWhileTheSweepReadsTests(unittest.TestCase):
    """The camera's alarm still is held off the cloud while a sweep reads (#193)."""

    setUp = fast_lane.FastLaneTests.setUp
    tearDown = fast_lane.FastLaneTests.tearDown
    _jpeg = fast_lane.FastLaneTests._jpeg
    _stored = fast_lane.FastLaneTests._stored

    def _client(self, reads, session):
        config = LocalRecognizerConfig(
            mode="active", cloud="fallback", min_confidence=0.5,
            model_dir=Path("/var/lib/gate-controller/models"),
        )

        def factory(configuration):
            self.engine = SlowEngine(configuration, reads)
            return self.engine

        self.local = LocalRecognizer(config, engine_factory=factory)
        self.local.start()
        assert self.local.wait_ready(5)
        return PlateRecognizerClient(
            "token", session=session, local_recognizer=self.local,
            authorised=lambda: {DMAX}, max_upload_width=1920,
        )

    def _processor(self, client, **options):
        return GateProcessor(
            recognizer=client, store=LocalStore(self.root / "gate.db"),
            relay=RecordingRelay(self.relay_calls), authorised={DMAX},
            cooldown=timedelta(seconds=0), clock=lambda: datetime.now(timezone.utc),
            decision_timeout=7.0, min_cloud_request_seconds=1.0, **options,
        )

    def test_the_held_still_is_denied_and_names_the_dmax(self):
        still = self._jpeg("camera-still.jpg", 100, (3840, 2160))
        session = FakeSession([])
        self.relay_calls = []
        client = self._client([(DMAX_MISREAD, 0.61)], session)
        processor = self._processor(client, camera_still_hold=lambda: "on")
        self.lanes = fast_lane.Lanes(processor)

        with self.assertLogs("gate_controller.processor", level="INFO") as journal:
            self.lanes.inject_upload(still)
            self.assertTrue(wait_for(lambda: self.lanes.result_for(still) is not None, 3.0))
        _, result = self.lanes.result_for(still)

        self.assertFalse(result.opened)
        self.assertEqual(result.reason, "no_match")
        self.assertEqual(session.calls, [], "the still was sent to the cloud")
        self.assertEqual(self.relay_calls, [])
        self.assertTrue(any("cloud_skipped reason=sweep_reading" in line
                            for line in journal.output), journal.output)
        self.assertIsNone(self._stored(processor, result)["observed_plate"])
        policy = _policy(result)
        self.assertEqual(policy["near_miss_plate"], DMAX)
        self.assertEqual(policy["near_miss_distance"], 1)
        self.assertEqual(policy["observed_plate"], DMAX_MISREAD)


class ReviewOnlyReadMatchingTests(unittest.TestCase):
    """`decide_access` never grants on a reviewed read, whatever it says."""

    NOW = datetime(2026, 10, 6, 9, 6, tzinfo=timezone.utc)

    def _skipped(self, plate, confidence):
        return PlateObservation(
            plate=None, confidence=0.0, source="local", cloud_lookup=False,
            review_plate=plate, review_confidence=confidence,
        )

    def test_an_exact_authorised_plate_at_full_confidence_is_never_a_grant(self):
        decision = decide_access(
            [self._skipped(DMAX, 1.0), self._skipped(DMAX, 1.0)], {DMAX}, now=self.NOW,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "no_match")
        self.assertIsNone(decision.observed_plate)
        # Distance zero is not a near miss; it was refused where it was read.
        self.assertIsNone(decision.near_miss_plate)

    def test_one_ocr_confusion_on_two_frames_is_never_a_fuzzy_grant(self):
        # "11WH2O41" for 11WH2041 on two frames is the two-frame confusion
        # rule's case exactly, had these been observations.
        decision = decide_access(
            [self._skipped("11WH2O41", 0.99), self._skipped("11WH2O41", 0.99)],
            {"11WH2041"}, now=self.NOW,
            corroborations=[PlateObservation("11WH2O41", 0.99, source="local")],
        )
        self.assertFalse(decision.allowed)
        self.assertIsNone(decision.match_rule)
        self.assertEqual(decision.near_miss_plate, "11WH2041")
        self.assertEqual(decision.near_miss_read, "11WH2O41")

    def test_the_most_confident_reviewed_read_with_a_near_miss_is_named(self):
        decision = decide_access(
            [self._skipped("ZZZ", 0.99), self._skipped("172L88", 0.40),
             self._skipped(DMAX_MISREAD, 0.61)],
            {DMAX}, now=self.NOW,
        )
        self.assertEqual(decision.near_miss_read, DMAX_MISREAD)
        self.assertEqual(decision.near_miss_plate, DMAX)
        self.assertEqual(decision.near_miss_distance, 1)

    def test_a_read_a_reader_stood_on_keeps_its_own_near_miss(self):
        decision = decide_access(
            [PlateObservation("172L68", 0.95), self._skipped(DMAX_MISREAD, 0.99)],
            {DMAX}, now=self.NOW,
        )
        self.assertEqual(decision.observed_plate, "172L68")
        self.assertEqual(decision.near_miss_plate, DMAX)
        self.assertIsNone(decision.near_miss_read)

    def test_the_telemetry_names_the_reviewed_read_beside_its_near_miss(self):
        decision = MatchDecision(
            allowed=False, reason="no_match", policy_band="08:00-22:00",
            policy_level="standard", policy_timezone="Europe/Dublin",
            policy_local_time="10:06", near_miss_plate=DMAX, near_miss_distance=1,
            near_miss_read=DMAX_MISREAD,
        )
        wire = MatchPolicyTelemetry.from_decision(decision).to_wire()
        self.assertEqual(wire["observed_plate"], DMAX_MISREAD)
        self.assertEqual(wire["near_miss_plate"], DMAX)
        self.assertEqual(wire["near_miss_distance"], 1)


if __name__ == "__main__":
    unittest.main()
