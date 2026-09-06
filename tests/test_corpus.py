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
