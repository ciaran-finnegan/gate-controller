import base64
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image

from gate_controller.backpressure import (
    BLOCKED_EVENT_DELIVERY, BLOCKED_GATE_ACTIVITY, BLOCKED_QUIET_WINDOW,
    ActivityGate,
)
from gate_controller.corpus import TrainingCorpus
from gate_controller.corpus_upload import (
    ABORT_OUTBOX_SENDING, ATTEMPT_NETWORK_DOWN_PROBE, BLOCKED_NETWORK_DOWN,
    CloudflareCorpusSender, CorpusUploadAborted, CorpusUploadConfig,
    CorpusUploadError, CorpusUploadUnshippable, CorpusUploadWorker, PacedBody,
    load_corpus_upload_config,
)


def jpeg(color="blue", size=(64, 32)):
    output = BytesIO()
    Image.new("RGB", size, color=color).save(output, format="JPEG")
    return output.getvalue()


def noisy_jpeg(size=(320, 320)):
    """A frame that does not compress away, so the body is many chunks long."""
    pixels = bytes(
        (index * 37 + (index >> 3) * 11) % 251
        for index in range(size[0] * size[1] * 3)
    )
    output = BytesIO()
    Image.frombytes("RGB", size, pixels).save(output, format="JPEG", quality=95)
    return output.getvalue()


class Clock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class RecordingSender:
    """Drains the paced body exactly as `requests` would."""

    def __init__(self, error=None):
        self.error = error
        self.sent = []

    def __call__(self, artefact_id, chunks):
        body = b"".join(chunks)
        self.sent.append((artefact_id, json.loads(body.decode("utf-8"))))
        if self.error is not None:
            raise self.error


class CorpusUploadTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "corpus"
        self.wall = [datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)]
        self.clock = Clock()
        self.corpus = TrainingCorpus(self.root, clock=lambda: self.wall[0])

    def record(self, image=None, payload=None, **kwargs):
        image = jpeg() if image is None else image
        path = self.corpus.record(
            image,
            payload=payload if payload is not None else {
                "results": [{"plate": "12d3456", "score": 0.91, "box": {"xmin": 1}}],
            },
            source=kwargs.pop("source", "plate_recognizer"),
            extra=kwargs.pop("extra", {"authorised": True, "cloud": "requested"}),
            **kwargs,
        )
        self.wall[0] = self.wall[0].replace(microsecond=self.wall[0].microsecond + 1)
        return path

    def gate(self, *, quiet_seconds=60.0, pending_events=lambda: 0):
        gate = ActivityGate(
            quiet_seconds=quiet_seconds, pending_events=pending_events,
            clock=self.clock,
        )
        self.clock.advance(quiet_seconds + 1)
        return gate

    def worker(self, send, gate=None, **config):
        return CorpusUploadWorker(
            self.corpus, send, gate if gate is not None else self.gate(),
            config=CorpusUploadConfig(enabled=True, **config),
            clock=lambda: self.wall[0], monotonic_clock=self.clock,
            sleep=lambda seconds: self.clock.advance(seconds),
            jitter=lambda: 1.0,
        )


class ArtefactDiscoveryTests(CorpusUploadTestCase):
    def test_pending_lists_pairs_oldest_first_and_ignores_half_artefacts(self):
        first = self.record()
        second = self.record(jpeg("red"))
        (self.root / "orphan.jpg").write_bytes(jpeg("green"))
        (self.root / "orphan-sidecar.json").write_text("{}")

        pending = self.corpus.pending()

        self.assertEqual(
            [artefact.payload_path for artefact in pending], [first, second],
            "sorting the stems sorts by capture time",
        )

    def test_discard_removes_the_pair_and_keeps_the_size_accounting_honest(self):
        path = self.record()
        self.record(jpeg("red"))
        before = self.corpus.status()["bytes"]

        self.assertTrue(self.corpus.discard(path.stem))

        self.assertFalse(path.exists())
        self.assertFalse(path.with_suffix(".json").exists())
        self.assertEqual(len(self.corpus.pending()), 1)
        self.assertLess(self.corpus.status()["bytes"], before)
        self.assertEqual(self.corpus.status()["discarded"], 1)
        self.assertFalse(self.corpus.discard("never-written"))

    def test_an_audio_artefact_travels_the_same_path_as_a_frame(self):
        """The model is artefacts, not frames.

        Audio capture is a separate piece of work; when it lands it writes a
        clip and a sidecar under the same stem convention, and everything from
        discovery to upload to discard already handles it.
        """
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        stem = "20260907T120500000000Z-abcdef123456"
        clip = b"RIFF....WAVEfmt "
        (self.root / (stem + ".wav")).write_bytes(clip)
        (self.root / (stem + ".json")).write_text(json.dumps({
            "schema_version": 2,
            "captured_at": "2026-09-07T12:05:00+00:00",
            "source": "gate_microphone",
            "artefact": {
                "kind": "audio", "media_type": "audio/wav",
                "sha256": hashlib.sha256(clip).hexdigest(), "bytes": len(clip),
            },
        }))
        sender = RecordingSender()

        self.assertEqual(self.worker(sender).run_once(), 1)

        _, document = sender.sent[0]
        self.assertEqual(document["kind"], "audio")
        self.assertEqual(document["media_type"], "audio/wav")
        self.assertEqual(base64.b64decode(document["data_base64"]), clip)
        self.assertFalse((self.root / (stem + ".wav")).exists())


class BackpressureTests(CorpusUploadTestCase):
    def test_nothing_is_sent_while_a_gate_event_is_running(self):
        self.record()
        sender = RecordingSender()
        gate = self.gate()
        worker = self.worker(sender, gate)

        with gate.activity("camera_event"):
            self.assertEqual(worker.run_once(), 0)

        self.assertEqual(sender.sent, [])
        self.assertEqual(worker.status()["last_blocked_by"], BLOCKED_GATE_ACTIVITY)
        self.assertEqual(worker.status()["pending"], 1,
                         "the frame is still on the card")

    def test_nothing_is_sent_until_the_link_has_been_quiet(self):
        self.record()
        sender = RecordingSender()
        gate = ActivityGate(quiet_seconds=60.0, clock=self.clock)
        worker = self.worker(sender, gate)
        with gate.activity("burst"):
            pass

        self.clock.advance(30)
        self.assertEqual(worker.run_once(), 0)
        self.assertEqual(worker.status()["last_blocked_by"], BLOCKED_QUIET_WINDOW)

        self.clock.advance(31)
        self.assertEqual(worker.run_once(), 1)

    def test_real_events_waiting_in_the_outbox_block_the_corpus_entirely(self):
        self.record()
        sender = RecordingSender()
        pending = [2]
        worker = self.worker(sender, self.gate(pending_events=lambda: pending[0]))

        self.assertEqual(worker.run_once(), 0)
        self.assertEqual(worker.status()["last_blocked_by"], BLOCKED_EVENT_DELIVERY)

        pending[0] = 0
        self.assertEqual(worker.run_once(), 1)

    def test_a_transfer_already_running_is_abandoned_when_an_event_starts(self):
        """Abandon, do not finish.

        The body is drained a chunk at a time; a camera event landing part way
        through tears the request down there rather than holding the uplink
        for the rest of a frame nobody is waiting for.
        """
        path = self.record(noisy_jpeg())
        gate = self.gate()

        def interrupting(artefact_id, chunks):
            collected = []
            for index, chunk in enumerate(chunks):
                collected.append(chunk)
                if index == 1:
                    gate.begin("camera_event")
            return collected

        worker = self.worker(interrupting, gate, bytes_per_second=4096)

        self.assertEqual(worker.run_once(), 0)

        self.assertTrue(path.exists(), "an abandoned upload leaves the card alone")
        self.assertEqual(worker.status()["aborted"], 1)
        self.assertEqual(worker.status()["consecutive_failures"], 0,
                         "standing down is not a failure and is not backed off")

    def test_an_abort_wrapped_by_the_http_client_is_still_read_as_an_abort(self):
        """`requests` re-raises a body generator's exception as a transport error."""
        self.record()
        gate = self.gate()

        def wrapping(artefact_id, chunks):
            try:
                for _ in chunks:
                    gate.begin("burst")
            except CorpusUploadAborted as error:
                raise ConnectionError("connection aborted") from error

        worker = self.worker(wrapping, gate)

        self.assertEqual(worker.run_once(), 0)
        self.assertEqual(worker.status()["aborted"], 1)
        self.assertEqual(worker.status()["consecutive_failures"], 0)

    def test_the_body_is_paced_to_a_fraction_of_the_uplink(self):
        gate = self.gate()
        body = PacedBody(
            b"x" * 32768, gate=gate, epoch=gate.epoch(), bytes_per_second=8192,
            chunk_bytes=8192, clock=self.clock,
            sleep=lambda seconds: self.clock.advance(seconds),
        )
        started = self.clock.now

        self.assertEqual(len(b"".join(body)), 32768)

        self.assertGreaterEqual(
            self.clock.now - started, 3.0,
            "32 KB at 8 KB/s cannot be delivered in under three seconds",
        )

    def test_the_gate_is_re_checked_between_artefacts_in_one_pass(self):
        for _ in range(4):
            self.record()
        gate = self.gate()
        sender = RecordingSender()
        sent = []

        def stopping(artefact_id, chunks):
            sender(artefact_id, chunks)
            sent.append(artefact_id)
            if len(sent) == 2:
                gate.begin("camera_event")

        worker = self.worker(stopping, gate)

        self.assertEqual(worker.run_once(), 2)
        self.assertEqual(len(self.corpus.pending()), 2,
                         "the pass stopped rather than draining the queue")


class UploadTests(CorpusUploadTestCase):
    def test_a_confirmed_frame_is_dropped_locally_and_indexed_for_search(self):
        image = jpeg("navy")
        path = self.record(image)
        sender = RecordingSender()
        worker = self.worker(sender)

        self.assertEqual(worker.run_once(), 1)

        artefact_id, document = sender.sent[0]
        self.assertEqual(artefact_id, hashlib.sha256(image).hexdigest())
        self.assertEqual(document["kind"], "frame")
        self.assertEqual(document["media_type"], "image/jpeg")
        self.assertEqual(document["source"], "plate_recognizer")
        self.assertEqual(document["plate"], "12D3456")
        self.assertEqual(document["score"], 0.91)
        self.assertEqual(document["decision"], "authorised")
        self.assertEqual(document["captured_at"], "2026-09-07T12:00:00+00:00")
        self.assertEqual(base64.b64decode(document["data_base64"]), image)
        self.assertEqual(document["sidecar"]["ocr"]["results"][0]["box"], {"xmin": 1})
        self.assertFalse(path.exists(), "the card is a buffer, not the archive")
        self.assertEqual(worker.status()["pending"], 0)
        self.assertEqual(
            worker.status()["last_success_at"], self.wall[0].isoformat()
        )

    def test_a_failed_upload_keeps_the_local_copy_and_backs_off(self):
        path = self.record()
        worker = self.worker(RecordingSender(error=CorpusUploadError("HTTP 503")))

        self.assertEqual(worker.run_once(), 0)

        self.assertTrue(path.exists(), "a failed upload never loses the frame")
        status = worker.status()
        self.assertEqual(status["consecutive_failures"], 1)
        self.assertEqual(status["last_error"], "HTTP 503")
        self.assertEqual(status["pending"], 1)

        # Inside the backoff nothing is attempted at all.
        sender = RecordingSender()
        worker._send = sender
        self.assertEqual(worker.run_once(), 0)
        self.assertEqual(sender.sent, [])
        self.clock.advance(61)
        self.assertEqual(worker.run_once(), 1)

    def test_an_artefact_that_can_never_be_sent_does_not_block_the_queue(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.root / "20260907T115900000000Z-000000000000.jpg").write_bytes(b"")
        (self.root / "20260907T115900000000Z-000000000000.json").write_text("{}")
        good = self.record()
        sender = RecordingSender()
        worker = self.worker(sender)

        self.assertEqual(worker.run_once(), 1)

        self.assertEqual(len(sender.sent), 1)
        self.assertFalse(good.exists())
        self.assertEqual(worker.status()["unshippable"], 1)
        self.assertTrue(
            (self.root / "20260907T115900000000Z-000000000000.jpg").exists(),
            "an artefact that cannot be shipped is kept, not deleted",
        )

    def test_a_pass_never_raises_however_broken_the_corpus_is(self):
        class Broken:
            def pending(self, limit=None):
                raise OSError("the card is gone")

        worker = CorpusUploadWorker(Broken(), RecordingSender(), self.gate())
        with self.assertLogs("gate_controller.corpus_upload", level="WARNING"):
            self.assertEqual(worker.run_once(), 0)

    def test_a_version_one_sidecar_still_on_the_card_is_read_as_a_frame(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        stem = "20260905T090000000000Z-0123456789ab"
        image = jpeg("olive")
        (self.root / (stem + ".jpg")).write_bytes(image)
        (self.root / (stem + ".json")).write_text(json.dumps({
            "schema_version": 1,
            "captured_at": "2026-09-05T09:00:00+00:00",
            "source": "plate_recognizer",
            "image": {"sha256": hashlib.sha256(image).hexdigest(), "bytes": len(image)},
            "ocr": {"results": [], "plate": None},
        }))
        sender = RecordingSender()

        self.assertEqual(self.worker(sender).run_once(), 1)

        _, document = sender.sent[0]
        self.assertEqual(document["kind"], "frame")
        self.assertEqual(document["decision"], "unknown")
        self.assertIsNone(document["plate"])

    def test_status_shows_a_buffer_that_is_growing_because_uploads_fail(self):
        self.record()
        self.record(jpeg("red"))
        worker = self.worker(RecordingSender(error=CorpusUploadError("HTTP 500")))

        worker.run_once()

        status = worker.status()
        self.assertEqual(status["pending"], 2)
        self.assertIsNone(status["last_success_at"])
        self.assertEqual(status["oldest_pending_at"], "2026-09-07T12:00:00+00:00")
        self.assertIsNotNone(status["oldest_pending_age_s"])
        self.assertEqual(status["quiet_window_seconds"], 60.0)


class SenderTests(unittest.TestCase):
    def test_the_endpoint_must_confirm_this_exact_artefact(self):
        class Client:
            def __init__(self, acknowledgement):
                self.acknowledgement = acknowledgement
                self.calls = []

            def post_stream(self, path, chunks, **kwargs):
                self.calls.append((path, b"".join(chunks), kwargs))
                return self.acknowledgement

        artefact_id = "a" * 64
        client = Client({"artefactId": artefact_id, "stored": True})
        CloudflareCorpusSender(client, "primary")(artefact_id, [b"{}"])
        path, body, kwargs = client.calls[0]
        self.assertEqual(path, "/api/controller/corpus")
        self.assertEqual(body, b"{}")
        self.assertEqual(kwargs["content_type"], "application/json")
        self.assertIn("Idempotency-Key", kwargs["headers"])

        for acknowledgement in (
            None, {}, {"stored": True}, {"artefactId": "b" * 64, "stored": True},
            {"artefactId": artefact_id, "stored": "yes"},
        ):
            with self.assertRaises(CorpusUploadError):
                CloudflareCorpusSender(Client(acknowledgement), "primary")(
                    artefact_id, [b"{}"]
                )


class ConfigurationTests(unittest.TestCase):
    def test_upload_is_on_unless_it_is_explicitly_turned_off(self):
        self.assertTrue(load_corpus_upload_config({}).enabled)
        self.assertFalse(load_corpus_upload_config({"GATE_CORPUS_UPLOAD": "off"}).enabled)
        with self.assertRaises(ValueError):
            load_corpus_upload_config({"GATE_CORPUS_UPLOAD": "sometimes"})

    def test_every_setting_is_bounded(self):
        config = load_corpus_upload_config({
            "GATE_CORPUS_QUIET_SECONDS": "120",
            "GATE_CORPUS_POLL_SECONDS": "600",
            "GATE_CORPUS_BATCH": "4",
            "GATE_CORPUS_UPLOAD_BYTES_PER_SECOND": "32768",
        })
        self.assertEqual(config.quiet_seconds, 120.0)
        self.assertEqual(config.poll_interval, 600.0)
        self.assertEqual(config.batch, 4)
        self.assertEqual(config.bytes_per_second, 32768)
        for variable, value in (
            ("GATE_CORPUS_QUIET_SECONDS", "1"),
            ("GATE_CORPUS_POLL_SECONDS", "0"),
            ("GATE_CORPUS_BATCH", "0"),
            ("GATE_CORPUS_BATCH", "not a number"),
            ("GATE_CORPUS_UPLOAD_BYTES_PER_SECOND", "10000000"),
        ):
            with self.assertRaises(ValueError, msg=f"{variable}={value}"):
                load_corpus_upload_config({variable: value})


class GateAudioTests(CorpusUploadTestCase):
    """The clips ``audio_capture`` actually writes, in the place it writes them.

    ADTS AAC under ``<corpus>/audio``, with a sidecar that names its own
    ``kind`` -- ``gate_audio``, what the capture is -- and carries no
    ``artefact`` block at all. The suffix table is what says which artefact
    family the corpus ships it as, and it had no ``.aac`` row, so from the
    first clip on 2026-09-08 every one of them was refused as an unknown
    suffix while the frames beside them shipped normally.
    """

    def clip(self, stem="20260908T120500000000Z-abcdef123456", *, payload=None,
             sidecar=None):
        payload = b"\xff\xf1" + b"gate audio" * 8 if payload is None else payload
        directory = self.root / "audio"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        clip_path = directory / (stem + ".aac")
        clip_path.write_bytes(payload)
        (directory / (stem + ".json")).write_text(json.dumps(sidecar or {
            "schema_version": 1,
            "kind": "gate_audio",
            "clip_id": stem.split("-", 1)[0],
            "captured_at": "2026-09-08T12:05:00+00:00",
            "label": "actuated",
            "actuation": {"source": "plate", "offset_seconds": 3.2},
            "audio": {
                "codec": "aac_lc", "sample_rate_hz": 16000, "channels": 1,
                "raw_copy": True, "decoded": False, "captured_seconds": 40.0,
            },
        }))
        return clip_path

    def test_a_gate_audio_clip_uploads_as_audio_with_its_sidecar(self):
        clip = self.clip()
        sender = RecordingSender()

        self.assertEqual(self.worker(sender).run_once(), 1)

        artefact_id, document = sender.sent[0]
        payload = clip.read_bytes() if clip.exists() else None
        self.assertIsNone(payload, "the clip left the card once the cloud had it")
        self.assertEqual(document["kind"], "audio")
        self.assertEqual(document["media_type"], "audio/aac")
        self.assertEqual(document["captured_at"], "2026-09-08T12:05:00+00:00")
        self.assertEqual(
            base64.b64decode(document["data_base64"]), b"\xff\xf1" + b"gate audio" * 8,
        )
        self.assertEqual(
            artefact_id,
            hashlib.sha256(b"\xff\xf1" + b"gate audio" * 8).hexdigest(),
            "idempotency is the digest of the clip, as it is for a frame",
        )
        self.assertEqual(document["sha256"], artefact_id)
        self.assertFalse((self.root / "audio" / "20260908T120500000000Z-abcdef123456.json").exists())

    def test_the_sidecar_travels_with_the_clip_it_labels(self):
        """A clip without its sidecar is not a training example.

        ``offset_seconds`` is what locates the relay pulse inside 40 s of
        audio, so it is the field that makes the clip worth keeping at all.
        """
        self.clip()

        sender = RecordingSender()
        self.assertEqual(self.worker(sender).run_once(), 1)

        _, document = sender.sent[0]
        self.assertEqual(document["sidecar"]["label"], "actuated")
        self.assertEqual(document["sidecar"]["actuation"]["offset_seconds"], 3.2)
        self.assertEqual(document["sidecar"]["kind"], "gate_audio")
        self.assertIs(document["sidecar"]["audio"]["decoded"], False)

    def test_frames_and_clips_share_one_queue_oldest_first(self):
        frame = self.record()
        clip = self.clip("20260908T120500000000Z-abcdef123456")
        sender = RecordingSender()

        self.assertEqual(self.worker(sender).run_once(), 2)

        self.assertEqual(
            [document["kind"] for _, document in sender.sent], ["frame", "audio"],
            "the frame was captured on the 7th and the clip on the 8th",
        )
        self.assertFalse(frame.exists())
        self.assertFalse(clip.exists())
        self.assertEqual(self.corpus.pending(), [])

    def test_a_mixed_corpus_still_stands_down_for_the_gate(self):
        self.record()
        self.clip()
        sender = RecordingSender()
        gate = self.gate()
        worker = self.worker(sender, gate)

        with gate.activity("camera_event"):
            self.assertEqual(worker.run_once(), 0)

        self.assertEqual(sender.sent, [])
        self.assertEqual(worker.status()["last_blocked_by"], BLOCKED_GATE_ACTIVITY)
        self.assertEqual(worker.status()["pending"], 2,
                         "the frame and the clip are both still on the card")

    def test_a_mixed_corpus_waits_for_the_outbox_and_the_quiet_window(self):
        self.record()
        self.clip()
        sender = RecordingSender()
        pending = [1]
        gate = ActivityGate(
            quiet_seconds=60.0, pending_events=lambda: pending[0], clock=self.clock,
        )
        worker = self.worker(sender, gate)
        with gate.activity("burst"):
            pass

        self.clock.advance(30)
        self.assertEqual(worker.run_once(), 0)
        self.assertEqual(worker.status()["last_blocked_by"], BLOCKED_QUIET_WINDOW)

        self.clock.advance(31)
        self.assertEqual(worker.run_once(), 0)
        self.assertEqual(worker.status()["last_blocked_by"], BLOCKED_EVENT_DELIVERY)

        pending[0] = 0
        self.assertEqual(worker.run_once(), 2)

    def test_a_clip_the_cloud_refuses_never_blocks_the_frames_behind_it(self):
        """The Worker's media types are an allow-list, and it is deployed apart.

        A controller that ships ``audio/aac`` to a Worker that has not learnt
        it yet gets HTTP 400 on the oldest artefact in the queue, every pass,
        for ever. Counting that as a failure would stop the frames behind it
        too, so a refusal on the merits stands the artefact aside and leaves it
        on the card for the deploy that accepts it.
        """
        clip = self.clip("20260907T110000000000Z-abcdef123456")
        frame = self.record()
        refused = []

        class Refusing(RecordingSender):
            def __call__(self, artefact_id, chunks):
                body = b"".join(chunks)
                document = json.loads(body.decode("utf-8"))
                if document["kind"] == "audio":
                    refused.append(artefact_id)
                    raise CorpusUploadUnshippable("HTTP 400")
                self.sent.append((artefact_id, document))

        sender = Refusing()
        worker = self.worker(sender)

        self.assertEqual(worker.run_once(), 1)

        self.assertEqual(len(refused), 1)
        self.assertEqual([document["kind"] for _, document in sender.sent], ["frame"])
        self.assertFalse(frame.exists(), "the frame behind it still shipped")
        self.assertTrue(clip.exists(), "and the clip is still on the card")
        self.assertEqual(worker.status()["unshippable"], 1)
        self.assertEqual(worker.status()["consecutive_failures"], 0)


class RefusalTests(unittest.TestCase):
    """What the sender makes of the cloud saying no."""

    class Response:
        def __init__(self, status_code):
            self.status_code = status_code

    class Client:
        def __init__(self, error):
            self.error = error

        def post_stream(self, path, chunks, **kwargs):
            b"".join(chunks)
            raise self.error

    def send(self, error):
        CloudflareCorpusSender(self.Client(error), "primary")("a" * 64, [b"{}"])

    def test_a_refusal_on_the_merits_is_unshippable_not_a_failure(self):
        for status in (400, 413, 415, 422):
            error = RuntimeError(f"HTTP {status}")
            error.response = self.Response(status)
            with self.subTest(status=status):
                with self.assertRaises(CorpusUploadUnshippable):
                    self.send(error)

    def test_a_bad_minute_is_still_retried(self):
        for status in (401, 403, 404, 429, 500, 503):
            error = RuntimeError(f"HTTP {status}")
            error.response = self.Response(status)
            with self.subTest(status=status):
                with self.assertRaises(RuntimeError) as raised:
                    self.send(error)
                self.assertNotIsInstance(raised.exception, CorpusUploadUnshippable)
        with self.assertRaises(OSError):
            self.send(OSError("connection reset"))

    def test_a_stand_down_is_never_mistaken_for_a_refusal(self):
        """`requests` wraps whatever the body generator raised.

        An abandoned transfer can arrive carrying a response object; it is a
        stand-down for the gate and must not be recorded against the artefact.
        """
        aborted = CorpusUploadAborted("a gate event started")
        error = RuntimeError("HTTP 400")
        error.response = self.Response(400)
        error.__cause__ = aborted

        with self.assertRaises(RuntimeError) as raised:
            self.send(error)
        self.assertNotIsInstance(raised.exception, CorpusUploadUnshippable)


class FakeOutbox:
    """What the corpus reads off the outbox worker, under test control."""

    def __init__(self, *, pending=0, failing=0, unreachable=False,
                 stuck_for=None, sending=False, last_error_type=None):
        self.pending = pending
        self.failing = failing
        self.unreachable = unreachable
        self.stuck_for = stuck_for
        self.is_sending = sending
        self.last_error_type = last_error_type

    def delivery_health(self):
        return {
            "pending": self.pending,
            "sending": self.is_sending,
            "failing": self.failing,
            "unreachable": self.unreachable,
            "stuck_for_s": self.stuck_for,
            "oldest_pending_age_s": self.stuck_for,
            "last_error_type": self.last_error_type,
        }

    def sending(self):
        return self.is_sending


class FakeProbe:
    def __init__(self, state="failed", age=10.0, hops=True):
        self.state = state
        self.age = age
        self.hops = hops

    def status(self):
        measured = {"enabled": True, "probed": True, "age_seconds": self.age}
        if self.hops:
            measured["hops"] = {"internet": {"state": self.state}}
        return measured


class TickingEvent:
    """A stop event whose wait advances the fake clock and ends after N ticks."""

    def __init__(self, clock, ticks):
        self.clock = clock
        self.ticks = ticks
        self.waits = []

    def is_set(self):
        return self.ticks <= 0

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.clock.advance(timeout or 0)
        self.ticks -= 1
        return self.is_set()


class NetworkDownTests(CorpusUploadTestCase):
    """The outbox outranks the corpus while it is being delivered, not for ever.

    On 2026-09-22 the farm router dropped most packets for a day. Seven
    telemetry items sat in the outbox on attempt 18, every failure a
    ``ConnectionError``, and the corpus -- 51 audio segments behind them --
    deferred for ``event_delivery`` every five minutes towards the 48-hour
    horizon. Yielding to a queue nothing can deliver achieves nothing.
    """

    T = 900.0

    def stuck(self, **overrides):
        settings = dict(pending=7, failing=7, unreachable=True,
                        stuck_for=self.T, last_error_type="ConnectionError")
        settings.update(overrides)
        return FakeOutbox(**settings)

    def outage_worker(self, send, outbox, *, net_probe=None, **config):
        gate = self.gate(pending_events=lambda: outbox.pending)
        return CorpusUploadWorker(
            self.corpus, send, gate,
            config=CorpusUploadConfig(
                enabled=True, outage_seconds=self.T, outage_probe_seconds=600.0,
                **config,
            ),
            clock=lambda: self.wall[0], monotonic_clock=self.clock,
            sleep=lambda seconds: self.clock.advance(seconds),
            jitter=lambda: 1.0, outbox=outbox, net_probe=net_probe,
        )

    def test_an_outbox_that_is_being_delivered_still_blocks_the_corpus(self):
        self.record()
        sender = RecordingSender()
        for description, outbox in (
            ("an item is on the wire",
             self.stuck(sending=True)),
            ("an item has not been tried yet",
             self.stuck(pending=8, failing=7)),
            ("the cloud is answering, if only with a 500",
             self.stuck(unreachable=False, last_error_type="OutboxSyncError")),
            ("nothing has failed at all",
             self.stuck(failing=0, unreachable=False)),
            ("the queue has not been stuck for T yet",
             self.stuck(stuck_for=self.T - 1)),
            ("the outbox cannot say how long it has been stuck",
             self.stuck(stuck_for=None)),
        ):
            with self.subTest(description):
                worker = self.outage_worker(sender, outbox)
                self.assertEqual(worker.run_once(), 0)
                self.assertEqual(worker.status()["last_blocked_by"], BLOCKED_EVENT_DELIVERY)
                self.assertEqual(worker.status()["outage_probes"], 0)
        self.assertEqual(sender.sent, [])

    def test_without_an_outbox_to_read_the_block_is_absolute_as_before(self):
        self.record()
        sender = RecordingSender()
        worker = self.worker(sender, self.gate(pending_events=lambda: 7))
        self.clock.advance(self.T * 10)

        self.assertEqual(worker.run_once(), 0)

        self.assertEqual(worker.status()["last_blocked_by"], BLOCKED_EVENT_DELIVERY)
        self.assertEqual(sender.sent, [])

    def test_an_outbox_stuck_on_the_link_for_t_lets_the_corpus_probe_oldest_first(self):
        first = self.record()
        second = self.record(jpeg("red"))
        third = self.record(jpeg("green"))
        sender = RecordingSender()
        worker = self.outage_worker(sender, self.stuck())

        with self.assertLogs("gate_controller.corpus_upload", level="INFO") as logs:
            self.assertEqual(worker.run_once(), 3,
                             "a link that answers is used while it answers")

        self.assertIn(
            f"gate_corpus stage=attempting reason={ATTEMPT_NETWORK_DOWN_PROBE} "
            "pending=3", logs.output[0],
        )
        self.assertEqual(
            [artefact_id for artefact_id, _ in sender.sent],
            [hashlib.sha256(path.read_bytes()).hexdigest()
             for path in ()] + [
                hashlib.sha256(image).hexdigest() for image in (
                    jpeg(), jpeg("red"), jpeg("green"))],
            "oldest first",
        )
        for path in (first, second, third):
            self.assertFalse(path.exists())
        status = worker.status()
        self.assertEqual(status["outage_probes"], 1)
        self.assertEqual(status["last_attempt_reason"], ATTEMPT_NETWORK_DOWN_PROBE)
        self.assertIsNone(status["retention_hold"], "the corpus is draining")

    def test_a_failed_probe_waits_the_probe_interval_not_the_backoff(self):
        self.record()
        self.record(jpeg("red"))
        sender = RecordingSender(error=ConnectionError("connection refused"))
        worker = self.outage_worker(sender, self.stuck())

        with self.assertLogs("gate_controller.corpus_upload", level="WARNING") as logs:
            self.assertEqual(worker.run_once(), 0)
        self.assertEqual(len(sender.sent), 1, "one artefact per probe, then nothing")
        self.assertIn("stage=upload_failed", logs.output[0])
        self.assertIn("retry_in_s=600", logs.output[0])
        self.assertIn(f"reason={ATTEMPT_NETWORK_DOWN_PROBE}", logs.output[0])
        self.assertEqual(worker.status()["retention_hold"], "upload_failed")

        for _ in range(3):
            self.clock.advance(150)
            self.assertEqual(worker.run_once(), 0)
        self.assertEqual(len(sender.sent), 1, "inside the probe interval nothing is tried")

        self.clock.advance(150)
        with self.assertLogs("gate_controller.corpus_upload", level="INFO") as logs:
            worker.run_once()
        self.assertEqual(len(sender.sent), 2)
        self.assertIn(f"reason={ATTEMPT_NETWORK_DOWN_PROBE}", logs.output[0])
        self.assertEqual(worker.status()["outage_probes"], 2)
        self.assertEqual(worker.status()["consecutive_failures"], 2,
                         "the heartbeat still counts every failure")

    def test_between_probes_the_deferral_says_network_down(self):
        path = self.record(noisy_jpeg())
        outbox = self.stuck()
        gate = self.gate(pending_events=lambda: outbox.pending)

        def interrupted(artefact_id, chunks):
            for index, _ in enumerate(chunks):
                if index == 1:
                    gate.begin("camera_event")

        worker = CorpusUploadWorker(
            self.corpus, interrupted, gate,
            config=CorpusUploadConfig(enabled=True, outage_seconds=self.T,
                                      outage_probe_seconds=600.0, bytes_per_second=4096),
            clock=lambda: self.wall[0], monotonic_clock=self.clock,
            sleep=lambda seconds: self.clock.advance(seconds),
            jitter=lambda: 1.0, outbox=outbox,
        )
        self.assertEqual(worker.run_once(), 0)
        self.assertEqual(worker.status()["aborted"], 1)
        gate.end("camera_event")
        self.clock.advance(61)

        with self.assertLogs("gate_controller.corpus_upload", level="INFO") as logs:
            self.assertEqual(worker.run_once(), 0)

        self.assertIn(f"stage=deferred reason={BLOCKED_NETWORK_DOWN}", logs.output[0])
        self.assertIn("next_probe_in_s=", logs.output[0])
        self.assertEqual(worker.status()["last_blocked_by"], BLOCKED_NETWORK_DOWN)
        self.assertEqual(worker.status()["retention_hold"], BLOCKED_NETWORK_DOWN)
        self.assertTrue(path.exists())

    def test_the_net_probe_can_shorten_t_but_never_override_the_outbox(self):
        self.record()
        young = dict(stuck_for=60.0)
        for description, outbox, probe, expected in (
            ("the probe saw the internet fail",
             self.stuck(**young), FakeProbe("failed"), 1),
            ("the probe's failure is stale",
             self.stuck(**young), FakeProbe("failed", age=1801.0), 0),
            ("the probe saw the internet answer",
             self.stuck(**young), FakeProbe("ok"), 0),
            ("the probe ran in ping mode and has no internet hop",
             self.stuck(**young), FakeProbe("failed", hops=False), 0),
            ("the probe is not there",
             self.stuck(**young), None, 0),
            ("the probe says failed but the cloud is answering the outbox",
             self.stuck(unreachable=False, **young), FakeProbe("failed"), 0),
            ("the probe says failed but an item is on the wire",
             self.stuck(sending=True, **young), FakeProbe("failed"), 0),
        ):
            with self.subTest(description):
                sender = RecordingSender()
                worker = self.outage_worker(sender, outbox, net_probe=probe)
                self.assertEqual(worker.run_once(), expected)
                if expected:
                    self.record()

    def test_a_probe_stands_down_the_instant_the_outbox_starts_sending(self):
        path = self.record(noisy_jpeg())
        outbox = self.stuck()

        def interrupted(artefact_id, chunks):
            for index, _ in enumerate(chunks):
                if index == 1:
                    outbox.is_sending = True

        worker = self.outage_worker(interrupted, outbox, bytes_per_second=4096)

        with self.assertLogs("gate_controller.corpus_upload", level="INFO") as logs:
            self.assertEqual(worker.run_once(), 0)

        self.assertTrue(any(
            f"stage=aborted" in line and f"reason={ABORT_OUTBOX_SENDING}" in line
            for line in logs.output
        ), logs.output)
        self.assertTrue(path.exists(), "the card is untouched")
        status = worker.status()
        self.assertEqual(status["aborted"], 1)
        self.assertEqual(status["consecutive_failures"], 0,
                         "standing down for the outbox is not a failure")
        # While the outbox is on the wire the ordinary rule applies again.
        self.clock.advance(601)
        self.assertEqual(worker.run_once(), 0)
        self.assertEqual(worker.status()["last_blocked_by"], BLOCKED_EVENT_DELIVERY)

    def test_a_send_that_starts_before_the_first_byte_stands_the_probe_down(self):
        self.record()
        outbox = self.stuck()
        sender = RecordingSender()
        gate = self.gate(pending_events=lambda: outbox.pending)
        original = outbox.delivery_health

        def health_then_send():
            # The verdict is read, then the outbox picks its moment.
            health = original()
            outbox.is_sending = True
            return health

        outbox.delivery_health = health_then_send
        worker = CorpusUploadWorker(
            self.corpus, sender, gate,
            config=CorpusUploadConfig(enabled=True, outage_seconds=self.T),
            clock=lambda: self.wall[0], monotonic_clock=self.clock,
            sleep=lambda seconds: self.clock.advance(seconds),
            jitter=lambda: 1.0, outbox=outbox,
        )

        self.assertEqual(worker.run_once(), 0)

        self.assertEqual(sender.sent, [], "not a byte went out")
        self.assertEqual(worker.status()["aborted"], 1)

    def test_recovery_returns_the_corpus_to_the_ordinary_rules(self):
        self.record()
        self.record(jpeg("red"))
        outbox = self.stuck()
        sender = RecordingSender(error=ConnectionError("no route to host"))
        worker = self.outage_worker(sender, outbox)
        self.assertEqual(worker.run_once(), 0)

        # The link comes back: the outbox drains first, as it should.
        outbox.pending = outbox.failing = 0
        outbox.unreachable = False
        outbox.oldest_age = None
        sender.error = None
        self.clock.advance(601)
        with self.assertLogs("gate_controller.corpus_upload", level="INFO") as logs:
            self.assertEqual(worker.run_once(), 2)

        self.assertFalse(any("stage=attempting" in line for line in logs.output),
                         "no probe: the outbox is empty and the ordinary rule lets it run")
        self.assertEqual(worker.status()["outage_probes"], 1)
        self.assertEqual(worker.status()["consecutive_failures"], 0)
        self.assertIsNone(worker.status()["retention_hold"])

    def test_the_retention_hold_is_the_reason_the_corpus_is_not_draining(self):
        outbox = FakeOutbox()
        sender = RecordingSender()
        worker = self.outage_worker(sender, outbox)
        self.assertIsNone(worker.retention_hold(), "nothing to hold")

        self.record()
        outbox.pending = 1
        self.assertEqual(worker.run_once(), 0)
        self.assertEqual(worker.retention_hold(), BLOCKED_EVENT_DELIVERY)

        # A gate event is momentary and does not change the answer.
        gate = worker._gate
        with gate.activity("camera_event"):
            self.assertEqual(worker.run_once(), 0)
        self.assertEqual(worker.retention_hold(), BLOCKED_EVENT_DELIVERY)

        outbox.pending = 0
        self.assertEqual(worker.run_once(), 0, "the quiet window after the event")
        self.clock.advance(61)
        self.assertEqual(worker.run_once(), 1)
        self.assertIsNone(worker.retention_hold())

    def test_the_loop_probes_at_the_bounded_rate_and_no_faster(self):
        """Through ``run_forever``: three probes in half an hour, not six."""
        for _ in range(3):
            self.record()
        sender = RecordingSender(error=ConnectionError("no route to host"))
        worker = self.outage_worker(sender, self.stuck(), poll_interval=300.0)
        stop = TickingEvent(self.clock, ticks=6)

        with self.assertLogs("gate_controller.corpus_upload", level="INFO") as logs:
            worker.run_forever(stop)

        self.assertEqual(stop.waits, [300.0] * 6)
        self.assertEqual(len(sender.sent), 3,
                         "t=0, t=600 and t=1200: one artefact per probe interval")
        attempts = [line for line in logs.output if "stage=attempting" in line]
        failures = [line for line in logs.output if "stage=upload_failed" in line]
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(failures), 3)
        self.assertEqual(len(logs.output), 6,
                         "the ticks inside a probe's retry say nothing at all")
        self.assertEqual(len(self.corpus.pending()), 3, "nothing was lost or dropped")

    def test_the_real_outbox_tells_the_corpus_when_it_is_stuck(self):
        """The production chain: LocalStore, OutboxWorker, ActivityGate, uploader."""
        from gate_controller.models import GateEvent
        from gate_controller.outbox import OutboxWorker
        from gate_controller.store import LocalStore
        import requests

        store = LocalStore(Path(self.temporary.name) / "gate.db")
        event_id = store.record_event(GateEvent(
            source="ocr", reason="exact_match", opened=True, idempotency_key="event-1",
            received_at=self.wall[0],
        ))
        link = {"up": False}

        def send(payload, evidence=None):
            if not link["up"]:
                raise requests.ConnectionError("connection refused")

        outbox = OutboxWorker(
            store, send=send, controller_id="primary",
            clock=lambda: self.wall[0], jitter=lambda: 1.0,
        )
        outbox.enqueue(event_id)
        self.record()
        self.record(jpeg("red"))
        sender = RecordingSender(error=ConnectionError("connection refused"))
        gate = ActivityGate(
            quiet_seconds=60.0, pending_events=store.pending_outbox_count,
            clock=self.clock,
        )
        self.clock.advance(61)
        worker = CorpusUploadWorker(
            self.corpus, sender, gate,
            config=CorpusUploadConfig(enabled=True, outage_seconds=self.T,
                                      outage_probe_seconds=600.0),
            clock=lambda: self.wall[0], monotonic_clock=self.clock,
            sleep=lambda seconds: self.clock.advance(seconds),
            jitter=lambda: 1.0, outbox=outbox,
        )

        def tick(seconds):
            from datetime import timedelta
            self.wall[0] += timedelta(seconds=seconds)
            self.clock.advance(seconds)

        # The outbox fails on the link; the corpus yields, for now.
        self.assertEqual(outbox.run_once(), 0)
        self.assertEqual(worker.run_once(), 0)
        self.assertEqual(worker.status()["last_blocked_by"], BLOCKED_EVENT_DELIVERY)
        self.assertEqual(worker.retention_hold(), BLOCKED_EVENT_DELIVERY)

        # T passes with the outbox still failing before anything answers.
        for _ in range(4):
            tick(301)
            self.assertEqual(outbox.run_once(), 0)
        with self.assertLogs("gate_controller.corpus_upload", level="INFO") as logs:
            self.assertEqual(worker.run_once(), 0)
        self.assertIn(f"stage=attempting reason={ATTEMPT_NETWORK_DOWN_PROBE}", logs.output[0])
        self.assertEqual(len(sender.sent), 1, "one probe, oldest first")

        # An intermittent link: the corpus probe gets through before the
        # outbox's next retry does.
        sender.error = None
        tick(601)
        self.assertEqual(worker.run_once(), 2)
        self.assertEqual(len(self.corpus.pending()), 0)

        # Then the outbox drains and the ordinary rule is back.
        link["up"] = True
        tick(301)
        self.assertEqual(outbox.run_once(), 1)
        self.assertEqual(store.pending_outbox_count(), 0)
        self.record(jpeg("navy"))
        tick(61)
        with self.assertLogs("gate_controller.corpus_upload", level="INFO") as logs:
            self.assertEqual(worker.run_once(), 1)
        self.assertFalse(any("stage=attempting" in line for line in logs.output))
        self.assertIsNone(worker.retention_hold())

    def test_the_outage_settings_are_bounded(self):
        config = load_corpus_upload_config({})
        self.assertEqual(config.outage_seconds, 900.0)
        self.assertEqual(config.outage_probe_seconds, 600.0)
        for name, value in (
            ("GATE_CORPUS_OUTAGE_SECONDS", "59"),
            ("GATE_CORPUS_OUTAGE_SECONDS", "86401"),
            ("GATE_CORPUS_OUTAGE_SECONDS", "soon"),
            ("GATE_CORPUS_OUTAGE_PROBE_SECONDS", "59"),
            ("GATE_CORPUS_OUTAGE_PROBE_SECONDS", "86401"),
        ):
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                load_corpus_upload_config({name: value})
        config = load_corpus_upload_config({
            "GATE_CORPUS_OUTAGE_SECONDS": "1800",
            "GATE_CORPUS_OUTAGE_PROBE_SECONDS": "300",
        })
        self.assertEqual((config.outage_seconds, config.outage_probe_seconds), (1800.0, 300.0))


if __name__ == "__main__":
    unittest.main()
