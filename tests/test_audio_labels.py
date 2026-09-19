"""The labelling loop: propose, review, keep, and train on what stands."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from gate_controller.audio_labels import (
    LABELS, Candidate, LabelStore, Verdict, candidates_from_actuations,
    candidates_from_detections, training_rows, unreviewed, write_manifest,
)

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def verdict(clip_id="abc123", label="gate_motor_opening", source="human", minutes=0, **kw):
    return Verdict(clip_id=clip_id, label=label, source=source,
                   at=NOW + timedelta(minutes=minutes), **kw)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = LabelStore(Path(self._temporary.name) / "labels.jsonl")

    def test_a_verdict_is_appended_and_read_back(self):
        self.store.record(verdict())
        rows = self.store.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], "gate_motor_opening")

    def test_the_store_is_readable_without_this_code(self):
        self.store.record(verdict())
        line = self.store.path.read_text().strip()
        self.assertEqual(json.loads(line)["clip_id"], "abc123")

    def test_nothing_is_ever_overwritten(self):
        self.store.record(verdict(label="gate_motor_opening"))
        self.store.record(verdict(label="tractor", minutes=5))
        self.assertEqual(len(self.store.rows()), 2)
        self.assertEqual(len(self.store.history("abc123")), 2)

    def test_the_latest_verdict_stands(self):
        self.store.record(verdict(label="gate_motor_opening"))
        self.store.record(verdict(label="tractor", minutes=5))
        self.assertEqual(self.store.current()["abc123"]["label"], "tractor")

    def test_a_detector_never_overturns_a_person(self):
        # A re-run of the detector must not quietly undo review.
        self.store.record(verdict(label="tractor", source="human"))
        self.store.record(verdict(label="gate_motor_opening", source="detector", minutes=99))
        self.assertEqual(self.store.current()["abc123"]["label"], "tractor")

    def test_a_person_may_overturn_a_person(self):
        self.store.record(verdict(label="tractor", source="human"))
        self.store.record(verdict(label="gate_motor_opening", source="human", minutes=1))
        self.assertEqual(self.store.current()["abc123"]["label"], "gate_motor_opening")

    def test_disagreement_is_kept_because_it_is_evidence(self):
        # Two labels on one clip says those two are confusable, which is more
        # useful than either verdict alone.
        self.store.record(verdict(label="gate_motor_opening"))
        self.store.record(verdict(label="tractor", minutes=5))
        self.assertEqual(self.store.disputed(),
                         {"abc123": {"gate_motor_opening", "tractor"}})

    def test_an_unknown_label_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.record(verdict(label="gate-ish"))

    def test_an_unknown_source_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.record(Verdict("abc123", "tractor", "guesswork", NOW))

    def test_an_overlong_note_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.record(verdict(note="x" * 5000))

    def test_a_truncated_final_line_loses_only_itself(self):
        # The file is appended to under power loss; a half-written last line
        # must not cost every label before it.
        self.store.record(verdict(clip_id="one"))
        self.store.record(verdict(clip_id="two"))
        with self.store.path.open("a") as handle:
            handle.write('{"clip_id": "three", "lab')
        self.assertEqual(len(self.store.rows()), 2)

    def test_an_empty_store_reads_as_empty_not_as_an_error(self):
        self.assertEqual(self.store.rows(), [])
        self.assertEqual(self.store.current(), {})
        self.assertEqual(self.store.counts(), {})

    def test_counts_describe_the_corpus(self):
        self.store.record(verdict(clip_id="a", label="gate_motor_opening"))
        self.store.record(verdict(clip_id="b", label="gate_motor_opening"))
        self.store.record(verdict(clip_id="c", label="tractor"))
        self.assertEqual(self.store.counts(), {"gate_motor_opening": 2, "tractor": 1})

    def test_nothing_is_a_label_not_an_absence(self):
        # Somebody listened and heard nothing. That is worth as much as a
        # positive and there is no other way to record it.
        self.assertIn("nothing", LABELS)
        self.store.record(verdict(label="nothing"))
        self.assertEqual(self.store.counts(), {"nothing": 1})


class CandidateTests(unittest.TestCase):
    def test_the_same_span_proposed_twice_is_one_clip(self):
        a = Candidate(at=NOW, seconds=30.0, source="relay")
        b = Candidate(at=NOW, seconds=30.0, source="detector", confidence=0.9)
        self.assertEqual(a.clip_id, b.clip_id)

    def test_different_spans_are_different_clips(self):
        a = Candidate(at=NOW, seconds=30.0, source="relay")
        b = Candidate(at=NOW + timedelta(seconds=1), seconds=30.0, source="relay")
        self.assertNotEqual(a.clip_id, b.clip_id)

    def test_every_actuation_becomes_a_candidate_with_pre_roll(self):
        rows = [{"id": 1, "relay_activated_at": NOW.isoformat(), "source": "local"}]
        found = candidates_from_actuations(rows, before_seconds=5.0, seconds=90.0)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].at, NOW - timedelta(seconds=5))
        self.assertEqual(found[0].source, "relay")

    def test_an_event_that_never_fired_the_relay_is_not_a_gate_cycle(self):
        self.assertEqual(candidates_from_actuations([{"id": 2, "relay_activated_at": None}]), [])

    def test_detector_output_becomes_candidates_carrying_its_confidence(self):
        found = candidates_from_detections(
            [{"at": NOW.isoformat(), "seconds": 21.1, "confidence": 0.9}])
        self.assertEqual(found[0].confidence, 0.9)
        self.assertEqual(found[0].source, "detector")

    def test_a_zero_length_detection_is_dropped(self):
        self.assertEqual(candidates_from_detections([{"at": NOW.isoformat(), "seconds": 0}]), [])


class QueueTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.store = LabelStore(self.root / "labels.jsonl")
        self.older = Candidate(at=NOW - timedelta(hours=2), seconds=20.0, source="detector")
        self.newer = Candidate(at=NOW, seconds=20.0, source="detector")

    def test_the_queue_is_newest_first(self):
        queue = unreviewed([self.older, self.newer], self.store)
        self.assertEqual([c.at for c in queue], [self.newer.at, self.older.at])

    def test_a_clip_a_person_judged_leaves_the_queue(self):
        self.store.record(verdict(clip_id=self.newer.clip_id, label="tractor", source="human"))
        queue = unreviewed([self.older, self.newer], self.store)
        self.assertEqual([c.clip_id for c in queue], [self.older.clip_id])

    def test_a_machine_verdict_does_not_clear_the_queue(self):
        # The detector proposing it is why it is in the queue, so its own
        # opinion cannot be what removes it.
        self.store.record(verdict(clip_id=self.newer.clip_id, source="detector"))
        self.assertEqual(len(unreviewed([self.older, self.newer], self.store)), 2)

    def test_a_manifest_carries_the_queue_and_what_is_known(self):
        self.store.record(verdict(clip_id=self.newer.clip_id, label="tractor", source="human"))
        path = self.root / "queue.json"
        document = write_manifest(path, [self.older, self.newer], self.store)
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        by_clip = {item["clip_id"]: item for item in document["items"]}
        self.assertEqual(by_clip[self.newer.clip_id]["label"], "tractor")
        self.assertIsNone(by_clip[self.older.clip_id]["label"])
        self.assertIn("gate_clang", document["labels"])


class TrainingSetTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = LabelStore(Path(self._temporary.name) / "labels.jsonl")

    def test_only_human_verdicts_are_trained_on_by_default(self):
        # A model trained on its own detector's proposals learns to agree with
        # itself, which is the failure this loop exists to prevent.
        self.store.record(verdict(clip_id="a", source="human"))
        self.store.record(verdict(clip_id="b", source="detector"))
        rows = training_rows(self.store)
        self.assertEqual([r["clip_id"] for r in rows], ["a"])

    def test_cheaper_sources_can_be_included_deliberately(self):
        self.store.record(verdict(clip_id="a", source="human"))
        self.store.record(verdict(clip_id="b", source="relay"))
        rows = training_rows(self.store, sources=("human", "relay"))
        self.assertEqual({r["clip_id"] for r in rows}, {"a", "b"})

    def test_a_training_set_can_be_narrowed_to_some_labels(self):
        self.store.record(verdict(clip_id="a", label="gate_motor_opening"))
        self.store.record(verdict(clip_id="b", label="birds"))
        rows = training_rows(self.store, labels=("gate_motor_opening",))
        self.assertEqual([r["clip_id"] for r in rows], ["a"])

    def test_a_corrected_clip_trains_on_its_correction(self):
        self.store.record(verdict(clip_id="a", label="gate_motor_opening"))
        self.store.record(verdict(clip_id="a", label="tractor", minutes=10))
        self.assertEqual(training_rows(self.store)[0]["label"], "tractor")


if __name__ == "__main__":
    unittest.main()
