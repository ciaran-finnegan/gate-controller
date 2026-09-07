import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from gate_controller.match_policy import (
    DEFAULT_POLICY,
    LEVEL_STANDARD,
    LEVEL_STRICT,
    RECOMMENDED_POLICY,
    STRICT_POLICY,
    LEVELS,
    Band,
    MatchPolicy,
    MatchPolicyError,
    level_rule,
    parse_policy,
    safe_policy,
)
from gate_controller.matching import decide_access
from gate_controller.models import MatchDecision, PlateObservation, RelayResult
from gate_controller.processor import GateProcessor
from gate_controller.settings import (
    MatchPolicyCache,
    SettingsRefreshWorker,
    policy_from_settings,
)
from gate_controller.store import LocalStore
from gate_controller.telemetry import MatchPolicyTelemetry


RECOMMENDED_DOCUMENT = {
    "schema_version": 1,
    "timezone": "Europe/Dublin",
    "bands": [
        {"start": "08:00", "end": "22:00", "level": "standard"},
        {"start": "22:00", "end": "08:00", "level": "strict"},
    ],
}


def _dublin(year, month, day, hour, minute=0):
    """A UTC instant, so the tests exercise the timezone conversion itself."""
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


class ScheduleParsingTests(unittest.TestCase):
    def test_parses_the_recommended_day_and_night_schedule(self):
        policy = parse_policy(RECOMMENDED_DOCUMENT)

        self.assertEqual(policy, RECOMMENDED_POLICY)
        self.assertEqual(
            [band.label for band in policy.bands], ["08:00-22:00", "22:00-08:00"]
        )

    def test_accepts_a_single_band_covering_the_whole_day(self):
        policy = parse_policy(
            {"bands": [{"start": "00:00", "end": "24:00", "level": "standard"}]}
        )

        self.assertEqual(policy.bands[0].level, LEVEL_STANDARD)

    def test_the_withdrawn_relaxed_level_is_not_selectable(self):
        """`relaxed` reaches the gate as `strict`, exactly like a typo would.

        Measured against this matching code, `relaxed` admitted thousands of
        real Irish registrations for a single authorised plate, so it must not
        be reachable from a settings document by any spelling.
        """
        self.assertNotIn("relaxed", LEVELS)

        policy = parse_policy({"bands": [
            {"start": "00:00", "end": "12:00", "level": "relaxed"},
            {"start": "12:00", "end": "24:00", "level": "standard"},
        ]})

        self.assertEqual(policy.bands[0].level, LEVEL_STRICT)
        self.assertEqual(policy.bands[1].level, LEVEL_STANDARD)
        self.assertEqual(level_rule("relaxed").name, LEVEL_STRICT)

    def test_rejects_a_schedule_with_a_gap(self):
        with self.assertRaises(MatchPolicyError) as raised:
            parse_policy(
                {"bands": [{"start": "08:00", "end": "22:00", "level": "standard"}]}
            )

        self.assertIn("uncovered", str(raised.exception))

    def test_rejects_overlapping_bands(self):
        with self.assertRaises(MatchPolicyError) as raised:
            parse_policy({"bands": [
                {"start": "00:00", "end": "13:00", "level": "standard"},
                {"start": "12:00", "end": "24:00", "level": "strict"},
            ]})

        self.assertIn("overlap", str(raised.exception))

    def test_rejects_a_malformed_time(self):
        with self.assertRaises(MatchPolicyError):
            parse_policy({"bands": [
                {"start": "8:00", "end": "22:00", "level": "standard"},
                {"start": "22:00", "end": "08:00", "level": "strict"},
            ]})

    def test_rejects_an_unknown_timezone(self):
        with self.assertRaises(MatchPolicyError):
            parse_policy({**RECOMMENDED_DOCUMENT, "timezone": "Mars/Olympus_Mons"})

    def test_rejects_a_newer_schema_version(self):
        with self.assertRaises(MatchPolicyError):
            parse_policy({**RECOMMENDED_DOCUMENT, "schema_version": 99})

    def test_an_unknown_level_fails_closed_to_strict(self):
        policy = parse_policy({"bands": [
            {"start": "00:00", "end": "12:00", "level": "wide_open"},
            {"start": "12:00", "end": "24:00", "level": "standard"},
        ]})

        self.assertEqual(policy.bands[0].level, LEVEL_STRICT)

    def test_safe_policy_keeps_todays_behaviour_without_a_schedule(self):
        self.assertEqual(safe_policy(None), DEFAULT_POLICY)
        self.assertEqual(DEFAULT_POLICY.bands[0].level, LEVEL_STANDARD)

    def test_safe_policy_fails_closed_on_a_broken_schedule(self):
        self.assertEqual(safe_policy({"bands": "nonsense"}), STRICT_POLICY)


class ScheduleResolutionTests(unittest.TestCase):
    def test_daytime_resolves_to_the_daytime_band(self):
        # 13:00 UTC in July is 14:00 in Dublin.
        resolved = RECOMMENDED_POLICY.resolve(_dublin(2026, 7, 1, 13))

        self.assertEqual(resolved.band, "08:00-22:00")
        self.assertEqual(resolved.level, LEVEL_STANDARD)
        self.assertEqual(resolved.local_time, "14:00")

    def test_the_overnight_band_wraps_past_midnight(self):
        for hour, expected in ((23, "23:00"), (2, "02:00")):
            with self.subTest(hour=hour):
                resolved = RECOMMENDED_POLICY.resolve(
                    _dublin(2026, 1, 15, hour)  # winter: Dublin is UTC
                )
                self.assertEqual(resolved.band, "22:00-08:00")
                self.assertEqual(resolved.level, LEVEL_STRICT)
                self.assertEqual(resolved.local_time, expected)

    def test_summer_time_shifts_the_band_boundary(self):
        # 21:30 UTC is 22:30 in Dublin during Irish Standard Time, so the
        # strict band has already started even though UTC says otherwise.
        resolved = RECOMMENDED_POLICY.resolve(_dublin(2026, 7, 1, 21, 30))

        self.assertEqual(resolved.level, LEVEL_STRICT)
        self.assertEqual(resolved.local_time, "22:30")

    def test_a_band_boundary_belongs_to_the_band_it_starts(self):
        resolved = RECOMMENDED_POLICY.resolve(_dublin(2026, 1, 15, 8))

        self.assertEqual(resolved.level, LEVEL_STANDARD)

    def test_an_unloadable_timezone_uses_the_strictest_configured_level(self):
        policy = MatchPolicy(
            bands=RECOMMENDED_POLICY.bands, timezone_name="Mars/Olympus_Mons"
        )

        resolved = policy.resolve(_dublin(2026, 7, 1, 13))

        self.assertEqual(resolved.level, LEVEL_STRICT)
        self.assertEqual(resolved.band, "unresolved")

    def test_a_naive_moment_is_read_as_utc_not_as_local_time(self):
        """21:30 naive is 22:30 in Dublin in July, so it is in the night band.

        Reading a naive datetime as local wall time would put the same instant
        at 21:30 local and hand a 22:30 decision to the daytime band for the
        whole of Irish Summer Time.
        """
        naive = RECOMMENDED_POLICY.resolve(datetime(2026, 7, 1, 21, 30))
        aware = RECOMMENDED_POLICY.resolve(
            datetime(2026, 7, 1, 21, 30, tzinfo=timezone.utc)
        )

        self.assertEqual(naive.local_time, "22:30")
        self.assertEqual(naive.level, LEVEL_STRICT)
        self.assertEqual(naive, aware)


class BandTests(unittest.TestCase):
    def test_a_wrapping_band_reports_its_true_duration(self):
        self.assertEqual(Band(22 * 60, 8 * 60, LEVEL_STRICT).duration_minutes, 600)

    def test_a_plain_band_reports_its_true_duration(self):
        self.assertEqual(Band(8 * 60, 22 * 60, LEVEL_STANDARD).duration_minutes, 840)


class SettingsEnvelopeTests(unittest.TestCase):
    def test_an_envelope_without_a_schedule_keeps_todays_behaviour(self):
        policy = policy_from_settings(
            {"controller_id": "primary", "settings_version": 1}
        )

        self.assertEqual(policy, DEFAULT_POLICY)

    def test_an_envelope_carries_the_schedule(self):
        policy = policy_from_settings({
            "controller_id": "primary",
            "settings_version": 1,
            "plate_matching": RECOMMENDED_DOCUMENT,
        })

        self.assertEqual(policy, RECOMMENDED_POLICY)

    def test_a_newer_envelope_version_is_rejected(self):
        with self.assertRaises(MatchPolicyError):
            policy_from_settings({"settings_version": 99})


class MatchPolicyCacheTests(unittest.TestCase):
    def setUp(self):
        self._directory = TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "match-policy.json"

    def test_starts_on_todays_behaviour(self):
        cache = MatchPolicyCache(self.path)

        self.assertEqual(cache.get(), DEFAULT_POLICY)
        self.assertFalse(cache.status()["configured"])

    def test_adopts_and_persists_a_schedule(self):
        cache = MatchPolicyCache(self.path)

        cache.replace({
            "controller_id": "primary",
            "plate_matching": RECOMMENDED_DOCUMENT,
        })

        self.assertEqual(cache.get(), RECOMMENDED_POLICY)
        self.assertEqual(MatchPolicyCache(self.path).get(), RECOMMENDED_POLICY)

    def test_a_broken_document_fails_closed_to_exact_matches(self):
        cache = MatchPolicyCache(self.path)

        cache.replace({"plate_matching": {"bands": [{"start": "08:00"}]}})

        self.assertEqual(cache.get(), STRICT_POLICY)
        self.assertIsNotNone(cache.status()["last_error"])

    def test_a_broken_document_is_not_cached_over_a_good_one(self):
        cache = MatchPolicyCache(self.path)
        cache.replace({"plate_matching": RECOMMENDED_DOCUMENT})

        cache.replace({"plate_matching": {"bands": []}})

        self.assertEqual(json.loads(self.path.read_text())["plate_matching"],
                         RECOMMENDED_DOCUMENT)

    def test_a_rejection_survives_a_restart(self):
        """A restart must not hand back the fuzziness the rejection removed."""
        cache = MatchPolicyCache(self.path)
        cache.replace({"plate_matching": RECOMMENDED_DOCUMENT})
        cache.replace({"plate_matching": {"bands": []}})

        restarted = MatchPolicyCache(self.path)

        self.assertEqual(restarted.get(), STRICT_POLICY)
        self.assertTrue(restarted.status()["configured"])
        self.assertIsNotNone(restarted.status()["last_error"])

    def test_an_unreadable_rejection_marker_still_keeps_the_gate_closed(self):
        cache = MatchPolicyCache(self.path)
        cache.replace({"plate_matching": RECOMMENDED_DOCUMENT})
        cache.replace({"plate_matching": {"bands": []}})
        self.path.with_name(self.path.name + ".rejected").write_text(
            "{ not json", encoding="utf-8"
        )

        self.assertEqual(MatchPolicyCache(self.path).get(), STRICT_POLICY)

    def test_a_readable_document_clears_the_rejection(self):
        cache = MatchPolicyCache(self.path)
        cache.replace({"plate_matching": {"bands": []}})

        cache.replace({"plate_matching": RECOMMENDED_DOCUMENT})

        self.assertFalse(self.path.with_name(self.path.name + ".rejected").exists())
        self.assertEqual(MatchPolicyCache(self.path).get(), RECOMMENDED_POLICY)
        self.assertIsNone(MatchPolicyCache(self.path).status()["last_error"])

    def test_the_status_uses_the_key_names_the_worker_reads(self):
        """`bands`, `configured` and `last_error` are a cross-repo contract."""
        cache = MatchPolicyCache(self.path)
        cache.replace({"plate_matching": RECOMMENDED_DOCUMENT})

        status = cache.status()

        self.assertEqual(sorted(status), [
            "bands", "configured", "last_error", "refreshed_at", "timezone",
        ])
        self.assertEqual(status["bands"], [
            {"start": "08:00", "end": "22:00", "level": "standard"},
            {"start": "22:00", "end": "08:00", "level": "strict"},
        ])
        self.assertTrue(status["configured"])
        self.assertIsNone(status["last_error"])

    def test_a_rejection_reason_reaches_the_status_bounded(self):
        cache = MatchPolicyCache(self.path)

        cache.mark_refresh_error(RuntimeError("x" * 500))

        self.assertEqual(len(cache.status()["last_error"]), 200)

    def test_an_unusable_cache_file_is_ignored(self):
        self.path.write_text("{ not json", encoding="utf-8")

        self.assertEqual(MatchPolicyCache(self.path).get(), DEFAULT_POLICY)

    def test_a_failed_refresh_keeps_the_last_good_schedule(self):
        cache = MatchPolicyCache(self.path)
        cache.replace({"plate_matching": RECOMMENDED_DOCUMENT})

        def fetch():
            raise RuntimeError("cloud unreachable")

        self.assertFalse(SettingsRefreshWorker(cache, fetch).run_once())
        self.assertEqual(cache.get(), RECOMMENDED_POLICY)
        self.assertEqual(cache.status()["last_error"], "cloud unreachable")

    def test_a_successful_refresh_adopts_the_schedule(self):
        cache = MatchPolicyCache(self.path)

        worker = SettingsRefreshWorker(
            cache, lambda: {"plate_matching": RECOMMENDED_DOCUMENT}
        )

        self.assertTrue(worker.run_once())
        self.assertEqual(cache.get(), RECOMMENDED_POLICY)


if __name__ == "__main__":
    unittest.main()


class MatchPolicyTelemetryTests(unittest.TestCase):
    def test_the_band_and_rule_reach_the_wire_payload(self):
        decision = decide_access(
            [
                PlateObservation("12O3456", 0.96),
                PlateObservation("12O3456", 0.97),
            ],
            {"1203456"},
            RECOMMENDED_POLICY,
            now=_dublin(2026, 7, 1, 13),
        )

        payload = MatchPolicyTelemetry.from_decision(decision).to_wire()

        self.assertEqual(payload, {
            "band": "08:00-22:00",
            "level": "standard",
            "timezone": "Europe/Dublin",
            "local_time": "14:00",
            "rule": "ocr_confusion",
            "edit_distance": 1,
            "observed_plate": "12O3456",
            "authorised_plate": "1203456",
        })

    def test_a_denial_carries_the_near_miss_for_review(self):
        decision = decide_access(
            [
                PlateObservation("12O3456", 0.96),
                PlateObservation("12O3456", 0.97),
            ],
            {"1203456"},
            RECOMMENDED_POLICY,
            now=_dublin(2026, 1, 15, 2),
        )

        payload = MatchPolicyTelemetry.from_decision(decision).to_wire()

        self.assertEqual(payload["level"], "strict")
        self.assertEqual(payload["band"], "22:00-08:00")
        self.assertEqual(payload["near_miss_plate"], "1203456")
        self.assertEqual(payload["near_miss_distance"], 1)
        self.assertNotIn("authorised_plate", payload)

    def test_an_event_without_a_decision_carries_no_match_policy(self):
        self.assertIsNone(MatchPolicyTelemetry.from_decision(
            MatchDecision(allowed=False, reason="ocr_error")
        ))


class ProcessorSchedulingTests(unittest.TestCase):
    """The schedule is read at decision time and recorded on the event."""

    def _run(self, policy, now):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            frames = []
            for index, colour in enumerate((40, 200)):
                path = root / f"frame-{index}.jpg"
                Image.new("L", (16, 8), color=colour).save(path, format="JPEG")
                frames.append(path)
            recognizer = _TwoFrameRecognizer(PlateObservation("12O3456", 0.97))
            processor = GateProcessor(
                recognizer=recognizer,
                store=LocalStore(root / "gate.db"),
                relay=_AcceptingRelay(),
                authorised={"1203456"},
                clock=lambda: now,
                match_policy=lambda: policy,
            )
            return processor.process(tuple(frames))

    def test_a_daytime_burst_opens_and_records_the_daytime_band(self):
        result = self._run(RECOMMENDED_POLICY, _dublin(2026, 7, 1, 13))

        self.assertTrue(result.opened)
        self.assertEqual(result.decision.policy_band, "08:00-22:00")
        self.assertEqual(
            result.telemetry.to_wire()["match_policy"]["level"], "standard"
        )

    def test_the_same_burst_overnight_stays_closed(self):
        result = self._run(RECOMMENDED_POLICY, _dublin(2026, 1, 15, 2))

        self.assertFalse(result.opened)
        policy = result.telemetry.to_wire()["match_policy"]
        self.assertEqual(policy["band"], "22:00-08:00")
        self.assertEqual(policy["level"], "strict")
        self.assertEqual(policy["near_miss_plate"], "1203456")

    def test_an_unavailable_schedule_falls_back_to_todays_behaviour(self):
        def broken():
            raise RuntimeError("cache exploded")

        with TemporaryDirectory() as directory:
            root = Path(directory)
            frames = []
            for index, colour in enumerate((40, 200)):
                path = root / f"frame-{index}.jpg"
                Image.new("L", (16, 8), color=colour).save(path, format="JPEG")
                frames.append(path)
            processor = GateProcessor(
                recognizer=_TwoFrameRecognizer(PlateObservation("12O3456", 0.97)),
                store=LocalStore(root / "gate.db"),
                relay=_AcceptingRelay(),
                authorised={"1203456"},
                clock=lambda: _dublin(2026, 1, 15, 2),
                match_policy=broken,
            )

            result = processor.process(tuple(frames))

        self.assertTrue(result.opened)
        self.assertEqual(result.decision.policy_level, LEVEL_STANDARD)


class _TwoFrameRecognizer:
    def __init__(self, observation):
        self._observation = observation

    def recognise(self, path):
        return self._observation


class _AcceptingRelay:
    def trigger(self, source, idempotency_key=None, *, pre_activation_inhibit=None,
                on_activation=None):
        if pre_activation_inhibit is not None:
            inhibition = pre_activation_inhibit()
            if inhibition is not None:
                return RelayResult(
                    activated=False, reason=inhibition[1],
                    idempotency_key=idempotency_key,
                )
        if on_activation is not None:
            on_activation()
        return RelayResult(
            activated=True, reason="activated", idempotency_key=idempotency_key
        )
