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
from threading import Event, Lock
from time import sleep

from PIL import Image

from gate_controller.local_recognizer import (
    DEFAULT_DETECTOR,
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_MODEL_DIR,
    DEFAULT_RECOGNISER,
    EngineRead,
    EngineResult,
    LocalRecognizer,
    LocalRecognizerConfig,
    LocalRecognizerUnavailable,
    build_local_recognizer,
    classify_agreement,
    load_local_recognizer_config,
)
from gate_controller.corpus import TrainingCorpus
from gate_controller.models import RelayResult
from gate_controller.ocr import PlateRecognizerClient
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


def read(plate, confidence, box=(10, 20, 110, 60), detection=0.9):
    return EngineResult(
        reads=(EngineRead(
            plate=plate, confidence=confidence, detection_confidence=detection,
            box=box,
        ),),
        width=1280, height=720, decode_ms=8.0, detect_ms=150.0, ocr_ms=14.0,
    )


def no_plate():
    return EngineResult(width=1280, height=720, decode_ms=8.0, detect_ms=150.0)


def recognizer(script, *, mode="shadow", cloud="fallback", logger=None, **kwargs):
    config = LocalRecognizerConfig(
        mode=mode, cloud=cloud, min_confidence=DEFAULT_MIN_CONFIDENCE,
        model_dir=Path("/var/lib/gate-controller/models"),
    )
    local = LocalRecognizer(
        config, engine_factory=engine_factory(script, **kwargs),
        logger=logger or RecordingLogger(),
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
        self.assertEqual(config.min_confidence, 0.95)
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
        local = recognizer([read("12D3456", 0.80)], logger=logger)
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
        local = recognizer([read("12D3456", 0.90)], mode="active")
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


if __name__ == "__main__":
    unittest.main()
