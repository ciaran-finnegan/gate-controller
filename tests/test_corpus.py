import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image

from gate_controller.corpus import MIN_MAX_BYTES, TrainingCorpus


def jpeg(color="blue", size=(64, 32)):
    output = BytesIO()
    Image.new("RGB", size, color=color).save(output, format="JPEG")
    return output.getvalue()


class Geometry:
    frame_width, frame_height = 3840, 2160
    crop_left, crop_top, crop_width, crop_height = 192, 864, 3456, 1296
    upload_width, upload_height = 1920, 720
    precropped, cropped = False, True
    secret = object()  # non-JSON attribute must be ignored


class TrainingCorpusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "corpus"
        self.now = [datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)]

    def corpus(self, **kwargs):
        return TrainingCorpus(self.root, clock=lambda: self.now[0], **kwargs)

    def test_records_the_frame_and_a_sidecar_with_the_answer_privately(self):
        corpus = self.corpus()
        payload = {
            "processing_time": 87.1,
            "results": [{
                "plate": "11wh2571", "score": 0.93, "dscore": 0.88,
                "box": {"xmin": 900, "ymin": 400, "xmax": 1100, "ymax": 460},
                "candidates": [{"plate": "11wh2571", "score": 0.93}, {"plate": "11wh257i", "score": 0.4}],
                "region": {"code": "ie", "score": 0.9},
                "vehicle": {"type": "Sedan", "score": 0.8},
                "internal": object(),
            }],
        }
        image = jpeg()

        path = corpus.record(image, payload=payload, source="plate_recognizer", geometry=Geometry(), extra={"precropped": False, "junk": object()})

        self.assertIsNotNone(path)
        self.assertEqual(path.read_bytes(), image)
        self.assertEqual(oct(path.stat().st_mode & 0o777), oct(0o600))
        self.assertEqual(oct(self.root.stat().st_mode & 0o777), oct(0o700))
        sidecar = json.loads(path.with_suffix(".json").read_text())
        self.assertEqual(sidecar["ocr"]["plate"], "11wh2571")
        self.assertEqual(sidecar["ocr"]["results"][0]["candidates"][1]["plate"], "11wh257i")
        self.assertNotIn("internal", sidecar["ocr"]["results"][0])
        self.assertEqual(sidecar["geometry"]["crop_left"], 192)
        self.assertNotIn("secret", sidecar["geometry"])
        self.assertEqual(sidecar["extra"], {"precropped": False})
        self.assertEqual(sidecar["image"]["bytes"], len(image))
        self.assertEqual(sidecar["captured_at"], "2026-09-06T12:00:00+00:00")
        self.assertEqual(corpus.status()["records"], 1)

    def test_an_empty_answer_is_still_recorded(self):
        corpus = self.corpus()
        path = corpus.record(jpeg(), payload={"results": []}, source="plate_recognizer")
        sidecar = json.loads(path.with_suffix(".json").read_text())
        self.assertEqual(sidecar["ocr"], {"results": []})

    def test_oldest_pairs_are_pruned_once_the_size_bound_is_exceeded(self):
        image = jpeg()
        corpus = self.corpus(max_bytes=MIN_MAX_BYTES)
        paths = [corpus.record(image, payload={"results": []}, source="test")]
        pair_bytes = paths[0].stat().st_size + paths[0].with_suffix(".json").stat().st_size
        corpus._max_bytes = pair_bytes * 2 + 10  # room for exactly two pairs
        for index in range(3):
            self.now[0] += timedelta(seconds=1)
            paths.append(corpus.record(image, payload={"results": []}, source="test"))

        remaining = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(len(remaining), 4, "two image/sidecar pairs remain")
        self.assertFalse(paths[0].exists())
        self.assertFalse(paths[0].with_suffix(".json").exists())
        self.assertTrue(paths[3].exists())
        self.assertEqual(corpus.status()["pruned"], 2)
        self.assertLessEqual(corpus.status()["bytes"], corpus._max_bytes)

    def test_bad_input_never_raises_and_is_counted(self):
        corpus = self.corpus()
        with self.assertLogs("gate_controller.corpus", level="WARNING"):
            self.assertIsNone(corpus.record(b"not a jpeg", payload={}, source="test"))
        self.assertEqual(corpus.status()["failures"], 1)
        with self.assertRaises(ValueError):
            TrainingCorpus(self.root, max_bytes=1024)


class SubdirectoryArtefactTests(unittest.TestCase):
    """The corpus root is not the only place artefacts land.

    ``audio_capture`` writes the gate's own clips to an ``audio`` directory
    beside the frames, deliberately sharing the stem-and-sidecar convention so
    that one uploader carries both. Until this was fixed ``pending`` read only
    the root, so every clip recorded from 2026-09-08 stayed on the card while
    the uploader reported an empty queue.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "corpus"
        self.audio = self.root / "audio"
        self.audio.mkdir(mode=0o700, parents=True)
        self.now = [datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)]

    def corpus(self, **kwargs):
        return TrainingCorpus(self.root, clock=lambda: self.now[0], **kwargs)

    def frame(self, corpus, color="blue"):
        path = corpus.record(jpeg(color), payload={"results": []}, source="plate_recognizer")
        self.now[0] += timedelta(seconds=1)
        return path

    def clip(self, stem, *, directory=None, payload=b"\xff\xf1clip"):
        directory = self.audio if directory is None else directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        clip_path = directory / (stem + ".aac")
        clip_path.write_bytes(payload)
        (directory / (stem + ".json")).write_text(json.dumps({
            "schema_version": 1, "kind": "gate_audio", "label": "actuated",
            "captured_at": "2026-09-08T12:00:30+00:00",
        }))
        return clip_path

    def test_pending_offers_the_audio_clips_beside_the_frames(self):
        corpus = self.corpus()
        first = self.frame(corpus)
        # Between the two frames by the clock in its stem, which is the only
        # thing the ordering is allowed to depend on.
        clip = self.clip("20260908T120000500000Z-abcdef123456")
        last = self.frame(corpus, "red")

        pending = corpus.pending()

        self.assertEqual(
            [artefact.payload_path for artefact in pending], [first, clip, last],
            "one queue in capture order, whichever directory each sits in",
        )
        self.assertEqual(
            [artefact.sidecar_path for artefact in pending],
            [first.with_suffix(".json"), clip.with_suffix(".json"),
             last.with_suffix(".json")],
        )

    def test_a_corpus_of_nothing_but_audio_is_offered_in_full(self):
        """The layout the card has once every frame has shipped."""
        clip = self.clip("20260908T120030000000Z-abcdef123456")
        later = self.clip("20260908T120130000000Z-fedcba654321")

        self.assertEqual(
            [artefact.payload_path for artefact in self.corpus().pending()],
            [clip, later],
        )

    def test_a_clip_missing_its_sidecar_is_not_offered(self):
        (self.audio / "20260908T120030000000Z-orphan00000.aac").write_bytes(b"\xff\xf1")
        (self.audio / "20260908T120040000000Z-lonely00000.json").write_text("{}")

        self.assertEqual(self.corpus().pending(), [])

    def test_pairing_never_crosses_a_directory(self):
        """Half an artefact here and half there is two halves, not a pair."""
        stem = "20260908T120030000000Z-abcdef123456"
        (self.root / (stem + ".jpg")).write_bytes(jpeg())
        (self.audio / (stem + ".json")).write_text("{}")

        self.assertEqual(self.corpus().pending(), [])

    def test_discard_removes_the_clip_from_the_directory_it_was_found_in(self):
        corpus = self.corpus()
        clip = self.clip("20260908T120030000000Z-abcdef123456")
        artefact = corpus.pending()[0]

        self.assertTrue(corpus.discard(artefact))

        self.assertFalse(clip.exists())
        self.assertFalse(clip.with_suffix(".json").exists())
        self.assertEqual(corpus.pending(), [])
        self.assertEqual(corpus.status()["discarded"], 1)

    def test_discarding_a_clip_leaves_the_frame_accounting_alone(self):
        """The bound counts what this corpus wrote, so only that comes off it.

        Subtracting bytes that were never added would walk the total down and
        quietly disable the backstop that protects the card.
        """
        corpus = self.corpus()
        self.frame(corpus)
        counted = corpus.status()["bytes"]
        self.clip("20260908T120030000000Z-abcdef123456", payload=b"\xff\xf1" + b"0" * 4096)

        clip_artefact = [
            artefact for artefact in corpus.pending()
            if artefact.payload_path.suffix == ".aac"
        ][0]
        self.assertTrue(corpus.discard(clip_artefact))

        self.assertEqual(corpus.status()["bytes"], counted)

    def test_the_size_backstop_never_deletes_a_clip_it_does_not_own(self):
        """Two pruners over one directory would fight; this one owns the root.

        The clips are bounded by ``AudioClipStore``'s own cap and retention
        window, so a corpus over its bound prunes its own oldest frames and
        leaves a clip that has not shipped yet exactly where it is.
        """
        image = jpeg()
        corpus = self.corpus(max_bytes=MIN_MAX_BYTES)
        clip = self.clip("20260908T115900000000Z-000000000000")
        first = self.frame(corpus)
        pair_bytes = first.stat().st_size + first.with_suffix(".json").stat().st_size
        corpus._max_bytes = pair_bytes + 10  # room for exactly one pair
        for _ in range(2):
            corpus.record(image, payload={"results": []}, source="test")
            self.now[0] += timedelta(seconds=1)

        self.assertFalse(first.exists(), "the oldest frame went")
        self.assertTrue(clip.exists(), "the clip, older still, stayed")
        self.assertTrue(clip.with_suffix(".json").exists())
        self.assertEqual(corpus.pending()[0].payload_path, clip)

    def test_the_walk_is_bounded_and_never_follows_a_symlink(self):
        deep = self.audio / "2026" / "09"
        self.clip("20260908T120030000000Z-toodeep00000", directory=deep)
        outside = Path(self.temporary.name) / "elsewhere"
        self.clip("20260908T120040000000Z-outside00000", directory=outside)
        try:
            (self.root / "linked").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover - platform
            self.skipTest("symlinks are unavailable here")

        self.assertEqual(self.corpus().pending(), [])
