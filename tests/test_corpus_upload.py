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
    CloudflareCorpusSender, CorpusUploadAborted, CorpusUploadConfig,
    CorpusUploadError, CorpusUploadWorker, PacedBody,
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


if __name__ == "__main__":
    unittest.main()
