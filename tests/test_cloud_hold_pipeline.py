"""2026-10-05, arrival 16:30:27: the cloud budget was spent while the plate was small.

The camera's alarm fired with the car far down the lane. The sweep handed the
cloud plate reader its five frames at +1.8, +4.0, +5.1, +7.5 and +8.8 s, ran to
+17 s, and never showed it a frame from the closest part of the approach --
while the on-device reader went on reading about 1.5 frames a second. Over 15
Sep-5 Oct: 222 ``decision_timeout``, 50 ``ocr_busy``, 72 ``queue_coalesced``.

With ``sweep_cloud_hold="on"`` a hand-over waits for a plate worth a lookup.
Everything here goes the way the alarm goes, on the harness
``tests/test_sweep_pipeline.py`` built for it:

    on_camera_event -> run_forever -> local_sweep -> the worker's injector ->
    the burst queue -> GateProcessor.prepare -> GateProcessor.process ->
    ActuationCoordinator -> the relay

inside the real ``run_worker``, with the real sweep reader, on-device
recogniser, OCR client, processor, store and coordinator. The engine fake
boxes each plate as wide as the passage says (in the recogniser's own padded
frame-fraction box), and the cloud fake can only read the frames it is told
are close: a far frame reaching it costs a lookup and reads nothing.
"""
import unittest
from functools import partial
from unittest import mock

from gate_controller.local_recognizer import EngineRead, EngineResult
from gate_controller.local_sweep import crop_to_region
from gate_controller.trigger_capture import TriggerCaptureConfig
from tests import test_sweep_pipeline as harness
from tests.test_local_recognizer import FakeResponse, FakeSession
from tests.test_sweep_pipeline import (
    FRAME_SIZE, REGION, CapturedLogs, Gate, ScriptedEngine, digest, frame, wait_for,
)

AUTHORISED_PLATE = "10CE1990"


def box_for(plate_px: int):
    """The engine's pixel box, on the band it is shown, for a plate ``plate_px`` wide in 4K.

    The band is ``REGION`` of a ``FRAME_SIZE`` frame shown unscaled, so a box
    ``w`` band pixels wide is ``w / FRAME_SIZE[0]`` of the frame; the
    recogniser's box carries an 8% pad each side, taken off again by
    ``SweepRead.plate_px``.
    """
    width = plate_px * 1.16 / 3840 * FRAME_SIZE[0]
    return (100.0, 60.0, 100.0 + width, 60.0 + width / 4)


class BoxingEngine(ScriptedEngine):
    """The ONNX boundary, boxing each plate as wide as the passage says."""

    def __init__(self, answers, widths, **kwargs):
        super().__init__(answers, **kwargs)
        self.widths = widths  # band digest -> plate px (4K)

    def read(self, image):
        result = super().read(image)
        width = self.widths.get(digest(image))
        if not result.reads or width is None:
            return result
        read = result.reads[0]
        return EngineResult(
            reads=(EngineRead(
                plate=read.plate, confidence=read.confidence,
                detection_confidence=read.detection_confidence, box=box_for(width),
                mean_confidence=read.mean_confidence,
            ),),
            width=result.width, height=result.height,
            decode_ms=result.decode_ms, detect_ms=result.detect_ms, ocr_ms=result.ocr_ms,
        )


class CloseOnlyCloud(FakeSession):
    """The cloud reader: reads the plate only in an upload it was told is close."""

    def __init__(self):
        super().__init__([])
        self.readable: set[str] = set()
        self.posted: list[bool] = []  # whether each upload was a close frame

    def post(self, *args, **kwargs):
        self.calls.append(kwargs)
        upload = kwargs["files"]["upload"][1]
        upload.seek(0)
        close = digest(upload.read()) in self.readable
        self.posted.append(close)
        if not close:
            return FakeResponse({"results": []})
        return FakeResponse({"results": [{"plate": AUTHORISED_PLATE.lower(), "score": 0.97, "box": {
            "xmin": 10, "ymin": 20, "xmax": 110, "ymax": 60,
        }}]})


def held_gate(test, *, widths_by_frame, answers, hold="on", last_chance=1.0, **options):
    """The harness's gate, with the cloud hold configured and plates boxed."""
    widths = {
        digest(crop_to_region(data, REGION)): width for data, width in widths_by_frame.items()
    }
    with mock.patch.object(
        harness, "TriggerCaptureConfig",
        partial(
            TriggerCaptureConfig, sweep_cloud_hold=hold, sweep_cloud_min_plate_px=300,
            sweep_cloud_last_chance_seconds=last_chance,
        ),
    ), mock.patch.object(
        harness, "ScriptedEngine", partial(BoxingEngine, widths=widths),
    ):
        return Gate(test, answers=answers, **options)


class CloudHoldPipelineTests(unittest.TestCase):
    def setUp(self):
        self.gate = None

    def tearDown(self):
        if self.gate is not None:
            self.gate.close()

    def test_far_frames_are_held_and_the_close_frame_the_cloud_reads_opens_the_gate(self):
        far = [frame(seed) for seed in range(120, 128)]
        close = [frame(seed) for seed in range(128, 140)]
        widths = {data: 190 + 8 * index for index, data in enumerate(far)}  # 190-246 px
        widths.update({data: 370 for data in close})
        # The device sees the plate in every frame and never reads it well
        # enough to open: the case the cloud is being paid for.
        answers = {
            digest(crop_to_region(data, REGION)): (AUTHORISED_PLATE, 0.40)
            for data in far + close
        }
        cloud = CloseOnlyCloud()
        gate = self.gate = held_gate(
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

        self.assertTrue(cloud.posted, "nothing reached the cloud")
        self.assertTrue(all(cloud.posted), f"a far frame was paid for: {cloud.posted}")
        self.assertEqual(gate.relay_calls, ["relay"], "one passage, one pulse")
        self.assertEqual(
            gate.stored(gate.opened_result())["source"], "ocr", "the cloud read opened it",
        )
        self.assertNotIn((False, "decision_timeout"), gate.outcomes())
        text = logs.text()
        self.assertEqual(text.count("gate_local_sweep stage=cloud_held"), 1)
        self.assertIn("stage=cloud_held mode=on reason=small_plate", text)
        self.assertIn("plate_px=370 release=plate_width", text)
        self.assertIn("reason=opened", text)

    def test_an_authorised_local_read_of_a_far_plate_opens_exactly_as_before(self):
        far = [frame(seed) for seed in range(140, 150)]
        widths = {data: 200 for data in far}
        answers = {digest(crop_to_region(far[2], REGION)): (AUTHORISED_PLATE, 0.99)}
        answers.update({
            digest(crop_to_region(data, REGION)): (AUTHORISED_PLATE, 0.40)
            for data in far[:2]
        })
        cloud = CloseOnlyCloud()
        gate = self.gate = held_gate(
            self, widths_by_frame=widths, answers=answers, cloud=cloud,
            sweep_seconds=6.0, cloud_frames=5,
        )
        gate.frames.script([(0.05 + index * 0.15, data) for index, data in enumerate(far)])

        with CapturedLogs() as logs:
            gate.alarm()
            self.assertTrue(
                wait_for(gate.opened), f"never opened: {gate.outcomes()}\n{logs.text()}",
            )
            self.assertTrue(wait_for(lambda: "gate_local_sweep outcome=ended" in logs.text()))

        self.assertEqual(gate.relay_calls, ["relay"])
        result = gate.opened_result()
        self.assertEqual(gate.stored(result)["source"], "local")
        self.assertEqual(cloud.posted, [], "a far plate was paid for although the device had it")
        self.assertIn("gate_local_sweep outcome=ended reason=opened", logs.text())


if __name__ == "__main__":
    unittest.main()
