"""2026-09-09 10:10:59: two confident local reads of the authorised plate, no gate.

An authorised car was refused with its plate read at 0.999, twice, because the
off-slot local pass held a second cloud reserve out of a budget the processor
had already reserved from.

Real client, real on-device recogniser (scripted engine, ~170 ms a read), real
processor. Each test is one link of the causal chain; all four fail on 4a02dc8.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, Thread
from time import monotonic, sleep

from PIL import Image

from gate_controller.local_recognizer import (
    EngineRead, EngineResult, LocalRecognizer, LocalRecognizerConfig,
)
from gate_controller.ocr import PlateRecognizerClient
from gate_controller.processor import GateProcessor
from gate_controller.store import LocalStore
from tests.test_local_recognizer import FakeResponse, FakeSession, cloud_payload
from tests.test_processor import RecordingRelay

AUTHORISED = {"131D2696"}
INFERENCE_SECONDS = 0.17


class SlowEngine:
    """The Pi's measured cost: ~170 ms of inference per read, one at a time."""

    def __init__(self, config, reads):
        self._reads = list(reads)
        self._lock = Lock()

    def load(self):
        return None

    def warmup(self):
        return None

    def read(self, image):
        with self._lock:
            plate, score = self._reads.pop(0) if self._reads else (None, 0.0)
        sleep(INFERENCE_SECONDS)
        if plate is None:
            return EngineResult(width=1920, height=648, decode_ms=8, detect_ms=150)
        return EngineResult(
            reads=(EngineRead(plate=plate, confidence=score, detection_confidence=0.9,
                              box=(10, 20, 110, 60), mean_confidence=score),),
            width=1920, height=648, decode_ms=8, detect_ms=150, ocr_ms=14,
        )


class StallingSession(FakeSession):
    """One cloud request that holds the slot far past every deadline."""

    def __init__(self, stall_seconds):
        super().__init__([])
        self._stall = stall_seconds

    def post(self, *args, **kwargs):
        self.calls.append(kwargs)
        sleep(self._stall)
        return FakeResponse({"results": []})


def local_recognizer(reads, min_confidence=0.5):
    config = LocalRecognizerConfig(
        mode="active", cloud="fallback", min_confidence=min_confidence,
        model_dir=Path("/var/lib/gate-controller/models"),
    )
    local = LocalRecognizer(config, engine_factory=lambda cfg: SlowEngine(cfg, reads))
    local.start()
    assert local.wait_ready(5)
    return local


class ConfidentLocalReadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.local = None

    def tearDown(self):
        if self.local is not None:
            self.local.close()
        self.directory.cleanup()

    def _jpeg(self, name, shade=120, size=(1920, 1080)):
        path = self.root / name
        Image.new("RGB", size, color=(shade, shade, shade)).save(path, format="JPEG")
        return path

    def _client(self, reads, session=None):
        self.local = local_recognizer(reads)
        return PlateRecognizerClient(
            "token", session=session or FakeSession([]), local_recognizer=self.local,
            authorised=lambda: AUTHORISED, max_upload_width=1920,
        )

    def _processor(self, client, database="gate.db"):
        return GateProcessor(
            recognizer=client, store=LocalStore(self.root / database),
            relay=RecordingRelay([]), authorised=AUTHORISED,
            cooldown=timedelta(seconds=0), clock=lambda: datetime.now(timezone.utc),
            decision_timeout=7.0, min_cloud_request_seconds=1.0,
        )

    # -- link 1: the pass does not wait for a read that fits its budget ----
    def test_a_local_read_that_fits_the_pass_budget_is_waited_for(self):
        # 71030739 at 10:10:57.686: the pass was handed 0.46 s, inference
        # takes 0.17 s, and the pass returned undecided after 42 ms because
        # `wait_seconds` kept a second 2.0 s cloud reserve out of a budget
        # the processor had already reserved from.
        client = self._client([("131D2696", 0.999)])
        started = monotonic()

        attempt = client.local_pass(self._jpeg("71030739.jpg"), trace_id="t1", budget=0.46)

        self.assertTrue(
            attempt.decided,
            f"the pass gave up after {monotonic() - started:.3f}s with 0.46 s of budget",
        )
        self.assertEqual(attempt.observation.plate, "131D2696")

    # -- link 2: the frame then queues for a slot it never needed -----------
    def test_a_confident_local_read_opens_the_gate_while_the_slot_is_held(self):
        # The slot was held from 10:10:52.7 to 10:11:01.0 by fccbe991's cloud
        # request. 71030739 was dequeued with 1.54 s left, read 131D2696 at
        # 0.999 on the device 170 ms later, and was denied `ocr_busy`.
        older = self._jpeg("fccbe991.jpg", 110)
        newer = self._jpeg("71030739.jpg", 120)
        session = StallingSession(stall_seconds=8.0)
        client = self._client([("131D28866", 0.305), ("131D2696", 0.999)], session)
        processor = self._processor(client)

        older_started = monotonic() - (7.0 - 5.14)
        blocker = Thread(
            target=processor.process, args=((older,),),
            kwargs={"decision_started_at": older_started}, daemon=True,
        )
        blocker.start()
        deadline = monotonic() + 3.0
        while not session.calls and monotonic() < deadline:
            sleep(0.01)
        self.assertTrue(session.calls, "the older frame never reached its cloud request")

        result = processor.process(
            (newer,), decision_started_at=monotonic() - (7.0 - 1.54),
        )

        self.assertTrue(result.opened, f"denied: {result.reason}")
        self.assertEqual(result.reason, "exact_match")
        self.assertEqual(
            len(session.calls), 1, "the locally decided frame must not post",
        )
        stored = processor._store.event_payload(result.event_id)
        self.assertEqual(stored["source"], "local")

    # -- link 3: with the cloud unaffordable, the pass is cut to 50 ms ------
    def test_a_confident_local_read_opens_the_gate_when_the_cloud_is_unaffordable(self):
        # 2047ae83 at 10:10:59.41: 0.62 s left, so `_local_pass_deadline`
        # could not hold the 1.05 s reserve and bounded the pass to its
        # 50 ms floor; the pass "overran", the cloud was skipped for want of
        # budget, and the 0.995 read landed 80 ms after the denial.
        client = self._client([("131D2696", 0.995)])
        processor = self._processor(client)

        result = processor.process(
            (self._jpeg("2047ae83.jpg", 130, (3840, 2160)),),
            decision_started_at=monotonic() - (7.0 - 0.62),
        )

        self.assertTrue(result.opened, f"denied: {result.reason}")
        self.assertEqual(result.reason, "exact_match")

    # -- link 4 (#124 side effect): the cloud request inherits the pass's bound
    def test_the_cloud_request_after_a_local_pass_keeps_the_decision_deadline(self):
        # The pass carries `state["deadline"]`, the bound the processor held
        # 1.05 s short of the decision's; `recognise` then sized the sockets
        # to it. A pass that used its budget left the request (0.1, 0.1).
        session = FakeSession([FakeResponse(cloud_payload("131D2696"))])
        client = self._client([("BTF2555", 0.091)], session)
        path = self._jpeg("d8253b44.jpg", 100)
        attempt = client.local_pass(path, trace_id="t4", budget=0.25)
        self.assertFalse(attempt.decided)

        client.recognise(
            path, timeout=(2.5, 2.8), trace_id="t4", budget=6.0, attempt=attempt,
        )

        self.assertEqual(session.calls[0]["timeout"], (2.5, 2.8))


if __name__ == "__main__":
    unittest.main()
