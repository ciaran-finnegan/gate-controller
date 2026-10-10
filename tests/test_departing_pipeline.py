"""Departures are not sent to the cloud plate reader.

Since 2026-10-05 the cloud reader decided no opening at all, and 42 of the 107
lookups that were slow (250 ms or more) or failed were spent on passages the
direction scan later marked ``exiting``. A departing car comes from behind the
camera and recedes up the approach, so the plate the on-device detector boxes
shrinks read by read -- 321 to 100 px and 333 to 103 px on 2026-10-09 -- while
an arriving car's plate only grows until it stops (138 to 319 px that
afternoon, its largest dip 3.8 %). The sweep's own reads are the signal
(``DepartingPlate``), ``TriggerFrameCapture.departing_skip`` is what the
processor asks, and ``GateProcessor.prepare`` records the answer once per
burst as ``cloud_skip="departing"`` before the burst is routed.

Everything here goes the way the alarm goes, on the harness
``tests/test_sweep_pipeline.py`` built:

    on_camera_event -> run_forever -> local_sweep -> the worker's injector ->
    the burst queue -> GateProcessor.prepare -> GateProcessor.process ->
    ActuationCoordinator -> the relay

inside the real ``run_worker``, with the processor handed the capture's own
``departing_skip`` exactly as ``__main__`` hands it. The engine fake boxes
each plate as wide as the passage says, and the cloud fake reads only frames
it is told are close.
"""
import unittest
from functools import partial
from time import monotonic
from unittest import mock

from gate_controller.local_sweep import crop_to_region
from gate_controller.processor import GateProcessor
from gate_controller.trigger_capture import TriggerCaptureConfig
from tests import test_sweep_pipeline as harness
from tests.test_cloud_hold_pipeline import AUTHORISED_PLATE, BoxingEngine, CloseOnlyCloud
from tests.test_sweep_pipeline import REGION, CapturedLogs, Gate, digest, frame, wait_for

STRANGER = "191D12345"


def departing_gate(test, *, widths_by_frame, answers, mode="on", hold="on",
                   last_chance=1.0, **options):
    """The harness's gate with the departure rule in ``mode`` and plates boxed.

    ``mode=None`` leaves the rule at its shipped default. The processor is
    given the capture's ``departing_skip`` the way ``main`` gives it: the
    capture does not exist until the gate is built, so the predicate reaches
    through to it once it does.
    """
    widths = {
        digest(crop_to_region(data, REGION)): width for data, width in widths_by_frame.items()
    }
    built = {}

    def departing():
        capture = built.get("capture")
        return None if capture is None else capture.departing_skip()

    with mock.patch.object(
        harness, "TriggerCaptureConfig",
        partial(
            TriggerCaptureConfig, sweep_cloud_hold=hold, sweep_cloud_min_plate_px=300,
            sweep_cloud_last_chance_seconds=last_chance,
            **({} if mode is None else {"sweep_departing_skip": mode}),
        ),
    ), mock.patch.object(
        harness, "ScriptedEngine", partial(BoxingEngine, widths=widths),
    ), mock.patch.object(
        harness, "GateProcessor", partial(GateProcessor, departing=departing),
    ):
        gate = Gate(test, answers=answers, **options)
    built["capture"] = gate.capture
    return gate


def receding_passage(seeds, *, big=280, small=(180, 170, 160, 150, 140, 130)):
    """A car leaving: two frames at ``big`` px, then its plate receding."""
    frames = [frame(seed) for seed in seeds]
    widths = {}
    for index, data in enumerate(frames):
        widths[data] = big if index < 2 else small[min(index - 2, len(small) - 1)]
    return frames, widths


class DepartingPipelineTests(unittest.TestCase):
    def setUp(self):
        self.gate = None

    def tearDown(self):
        if self.gate is not None:
            self.gate.close()

    def _run_passage(self, gate, frames, logs, *, still=None):
        gate.frames.script([(0.05 + index * 0.15, data) for index, data in enumerate(frames)])
        gate.alarm()
        if still is not None:
            # The camera's own alarm still lands while the sweep is reading.
            self.assertTrue(wait_for(
                lambda: "gate_local_sweep stage=departing" in logs.text(), 4.0,
            ), f"the plate never read as receding:\n{logs.text()}")
            gate.ftp_still(still)
        self.assertTrue(
            wait_for(lambda: "gate_local_sweep outcome=ended" in logs.text(), 12.0),
            logs.text(),
        )
        # Every hand-over the sweep made has been decided, still included.
        wait_for(lambda: gate.sweep_status()["cloud_handovers"] + (1 if still else 0)
                 <= len(gate.outcomes()), 4.0)

    def test_a_departing_cars_frames_and_still_post_nothing(self):
        frames, widths = receding_passage(range(200, 208))
        # The device boxes a plate in every frame and reads it badly: the
        # frame the cloud would otherwise be paid for.
        answers = {
            digest(crop_to_region(data, REGION)): (STRANGER, 0.40) for data in frames
        }
        cloud = CloseOnlyCloud()
        gate = self.gate = departing_gate(
            self, widths_by_frame=widths, answers=answers, cloud=cloud,
            sweep_seconds=4.0, cloud_frames=5, fallback=1,
        )
        still = frame(299)
        gate.engine.answers[digest(gate.pipeline_bytes(still))] = (STRANGER, 0.40)
        cloud.readable = {digest(gate.pipeline_bytes(data)) for data in frames}

        with CapturedLogs() as logs:
            self._run_passage(gate, frames, logs, still=still)

        text = logs.text()
        self.assertEqual(cloud.posted, [], f"a departure was paid for:\n{text}")
        self.assertEqual(gate.relay_calls, [])
        self.assertEqual(gate.opened(), [])
        self.assertIn("gate_local_sweep stage=departing mode=on peak_px=280 plate_px=170", text)
        skipped = text.count("gate_ocr stage=cloud_skipped reason=departing")
        self.assertGreaterEqual(skipped, 2, f"hand-over and still were not both skipped:\n{text}")
        # Each skipped frame is on record, answered "no plate", as every skip is.
        self.assertTrue(gate.outcomes(), "nothing was decided")
        self.assertEqual(set(gate.outcomes()), {(False, "no_match")})
        self.assertNotIn("decision_timeout", text)
        self.assertEqual(gate.sweep_status()["departing"], 1)

    def test_an_arriving_car_still_gets_its_cloud_fallback(self):
        # Far frames growing with the detector's jitter (a 3 % dip at the
        # third read), then the close frames the cloud can read.
        far = [frame(seed) for seed in range(210, 217)]
        close = [frame(seed) for seed in range(217, 227)]
        widths = dict(zip(far, (200, 210, 204, 198, 215, 228, 246)))
        widths.update({data: 370 for data in close})
        answers = {
            digest(crop_to_region(data, REGION)): (AUTHORISED_PLATE, 0.40)
            for data in far + close
        }
        cloud = CloseOnlyCloud()
        gate = self.gate = departing_gate(
            self, widths_by_frame=widths, answers=answers, cloud=cloud,
            sweep_seconds=6.0, cloud_frames=5,
        )
        cloud.readable = {digest(gate.pipeline_bytes(data)) for data in close}
        gate.frames.script([(0.05 + index * 0.15, data) for index, data in enumerate(far + close)])

        with CapturedLogs() as logs:
            gate.alarm()
            self.assertTrue(
                wait_for(gate.opened), f"never opened: {gate.outcomes()}\n{logs.text()}",
            )
            self.assertTrue(wait_for(lambda: "gate_local_sweep outcome=ended" in logs.text()))

        text = logs.text()
        self.assertTrue(cloud.posted, "the arrival's cloud fallback was never made")
        self.assertEqual(gate.relay_calls, ["relay"])
        self.assertEqual(gate.stored(gate.opened_result())["source"], "ocr")
        self.assertNotIn("stage=departing", text)
        self.assertNotIn("reason=departing", text)
        self.assertEqual(gate.sweep_status()["departing"], 0)

    def test_a_departing_cars_authorised_rear_plate_still_opens_on_the_device(self):
        # #171: the relay fires for an authorised rear plate the device reads.
        frames, widths = receding_passage(range(230, 237), big=330)
        answers = {
            digest(crop_to_region(data, REGION)): (AUTHORISED_PLATE, 0.40) for data in frames
        }
        # The fifth frame, read once the plate is already receding, is sharp.
        answers[digest(crop_to_region(frames[4], REGION))] = (AUTHORISED_PLATE, 0.99)
        cloud = CloseOnlyCloud()
        gate = self.gate = departing_gate(
            self, widths_by_frame=widths, answers=answers, cloud=cloud,
            sweep_seconds=4.0, cloud_frames=5, fallback=1,
        )
        gate.frames.script([(0.05 + index * 0.15, data) for index, data in enumerate(frames)])

        with CapturedLogs() as logs:
            gate.alarm()
            self.assertTrue(
                wait_for(gate.opened), f"never opened: {gate.outcomes()}\n{logs.text()}",
            )
            self.assertTrue(wait_for(lambda: "gate_local_sweep outcome=ended" in logs.text()))

        text = logs.text()
        self.assertEqual(gate.relay_calls, ["relay"], "one passage, one pulse")
        result = gate.opened_result()
        self.assertEqual(result.reason, "exact_match")
        self.assertEqual(gate.stored(result)["source"], "local")
        # The first frame, 330 px, was released on plate width before the
        # plate had shrunk: that one lookup is spent on a departure, as it is
        # on the Pi. Nothing after the verdict is.
        self.assertLessEqual(len(cloud.posted), 1, f"paid for after the verdict:\n{text}")
        self.assertIn("gate_local_sweep stage=departing mode=on", text)
        self.assertIn("gate_local_sweep outcome=ended reason=opened", text)
        self.assertEqual(gate.sweep_status()["departing"], 1)

    def test_a_new_alarm_withdraws_the_verdict_from_the_next_cars_still_but_not_from_the_last_cars_frames(self):
        # The next alarm is queued by the webhook thread and dequeued by the
        # sweep's loop later; its camera still can reach the processor in
        # between, and must not be kept off the cloud by the car before it.
        # The departing car's own sweep, halted by that alarm, still hands
        # its fallback frame over -- and that frame keeps its verdict.
        frames, widths = receding_passage(range(260, 268))
        answers = {
            digest(crop_to_region(data, REGION)): (STRANGER, 0.40) for data in frames
        }
        cloud = CloseOnlyCloud()
        gate = self.gate = departing_gate(
            self, widths_by_frame=widths, answers=answers, cloud=cloud,
            sweep_seconds=4.0, cloud_frames=5, fallback=1,
        )
        cloud.readable = {digest(gate.pipeline_bytes(data)) for data in frames}
        gate.frames.script([(0.05 + index * 0.15, data) for index, data in enumerate(frames)])

        with CapturedLogs() as logs:
            started = monotonic()
            gate.alarm()
            self.assertTrue(wait_for(
                lambda: "gate_local_sweep stage=departing" in logs.text(), 4.0,
            ), logs.text())
            self.assertEqual(gate.capture.departing_skip(), "on")
            # Past the capture's minimum interval between alarms.
            wait_for(lambda: monotonic() - started >= 0.6, 1.0)
            gate.alarm()
            self.assertIsNone(gate.capture.departing_skip(), "the next car inherited the verdict")
            self.assertTrue(wait_for(
                lambda: "outcome=ended reason=new_event" in logs.text(), 6.0,
            ), logs.text())
            self.assertTrue(wait_for(
                lambda: logs.text().count("gate_local_sweep outcome=ended") >= 2, 12.0,
            ), logs.text())
            wait_for(lambda: gate.outcomes(), 4.0)

        text = logs.text()
        self.assertIn("source=sweep_fallback", text, "the halted sweep handed nothing over")
        self.assertIn("gate_ocr stage=cloud_skipped reason=departing", text)
        self.assertEqual(cloud.posted, [], f"the departing car's frame was paid for:\n{text}")
        self.assertEqual(gate.relay_calls, [])

    def test_the_shipped_default_judges_and_journals_and_sends_exactly_as_before(self):
        # Shipped in shadow: a departure's frames still go to the cloud, and
        # the journal says what `on` would have kept back.
        frames, widths = receding_passage(range(270, 278))
        answers = {
            digest(crop_to_region(data, REGION)): (STRANGER, 0.40) for data in frames
        }
        cloud = CloseOnlyCloud()
        gate = self.gate = departing_gate(
            self, widths_by_frame=widths, answers=answers, cloud=cloud, mode=None,
            sweep_seconds=4.0, cloud_frames=5, fallback=1,
        )
        self.assertEqual(gate.sweep_status()["departing_skip"], "shadow")

        with CapturedLogs() as logs:
            self._run_passage(gate, frames, logs)
            self.assertTrue(wait_for(lambda: cloud.posted, 4.0), logs.text())

        text = logs.text()
        self.assertIn("gate_local_sweep stage=departing mode=shadow", text)
        self.assertIn("gate_ocr stage=cloud_skip_shadow would=departing", text)
        self.assertNotIn("stage=cloud_skipped reason=departing", text)
        self.assertEqual(gate.relay_calls, [])
        self.assertEqual(gate.sweep_status()["departing"], 1)

    def test_shadow_journals_the_skip_and_sends_the_frame_as_before(self):
        frames, widths = receding_passage(range(240, 248))
        answers = {
            digest(crop_to_region(data, REGION)): (STRANGER, 0.40) for data in frames
        }
        cloud = CloseOnlyCloud()
        gate = self.gate = departing_gate(
            self, widths_by_frame=widths, answers=answers, cloud=cloud, mode="shadow",
            sweep_seconds=4.0, cloud_frames=5, fallback=1,
        )

        with CapturedLogs() as logs:
            self._run_passage(gate, frames, logs)
            self.assertTrue(wait_for(lambda: cloud.posted, 4.0), logs.text())

        text = logs.text()
        self.assertIn("gate_local_sweep stage=departing mode=shadow", text)
        self.assertIn("gate_ocr stage=cloud_skip_shadow would=departing", text)
        self.assertNotIn("stage=cloud_skipped reason=departing", text)
        self.assertEqual(gate.relay_calls, [])

    def test_off_journals_nothing_and_sends_the_frame_as_before(self):
        frames, widths = receding_passage(range(250, 258))
        answers = {
            digest(crop_to_region(data, REGION)): (STRANGER, 0.40) for data in frames
        }
        cloud = CloseOnlyCloud()
        gate = self.gate = departing_gate(
            self, widths_by_frame=widths, answers=answers, cloud=cloud, mode="off",
            sweep_seconds=4.0, cloud_frames=5, fallback=1,
        )

        with CapturedLogs() as logs:
            self._run_passage(gate, frames, logs)
            self.assertTrue(wait_for(lambda: cloud.posted, 4.0), logs.text())

        text = logs.text()
        self.assertNotIn("stage=departing", text)
        self.assertNotIn("reason=departing", text)
        self.assertNotIn("would=departing", text)
        self.assertEqual(gate.sweep_status()["departing_skip"], "off")


if __name__ == "__main__":
    unittest.main()
