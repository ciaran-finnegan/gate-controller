"""The owner's pause switch: how it is read, and what a bad value does (nothing).

The behaviour it drives -- every automatic grant refused at the coordinator,
a person's command untouched -- is tested through the real path in
tests/test_one_pulse_per_car.py and tests/test_gate_jam_2026_10_10.py. Here
are the two readers, and the settings cache that keeps the app's value.
"""
import json
import tempfile
import unittest
from pathlib import Path

from gate_controller.automatic_open import (
    AutomaticOpenConfig, config_from_settings, is_valid_section, load_config,
)
from gate_controller.settings import MatchPolicyCache

ENVELOPE = {"controller_id": "primary", "settings_version": 1}


class EnvironmentTests(unittest.TestCase):
    def test_unset_is_on_from_the_default(self):
        self.assertEqual(load_config({}), AutomaticOpenConfig(enabled=True, source="default"))
        self.assertEqual(load_config({"GATE_AUTOMATIC_OPEN": ""}), AutomaticOpenConfig())
        self.assertEqual(load_config(None), AutomaticOpenConfig())

    def test_off_and_on_in_their_usual_spellings(self):
        for value in ("off", "OFF", " 0 ", "false", "no"):
            with self.subTest(value=value):
                self.assertEqual(load_config({"GATE_AUTOMATIC_OPEN": value}),
                                 AutomaticOpenConfig(enabled=False, source="environment"))
        for value in ("on", "On", "1", "true", "yes"):
            with self.subTest(value=value):
                self.assertEqual(load_config({"GATE_AUTOMATIC_OPEN": value}),
                                 AutomaticOpenConfig(enabled=True, source="environment"))

    def test_anything_else_is_rejected_whole_and_logged(self):
        for value in ("maybe", "paused", "2", "of"):
            with self.subTest(value=value), self.assertLogs(
                "gate_controller.automatic_open", level="ERROR"
            ) as logs:
                self.assertEqual(load_config({"GATE_AUTOMATIC_OPEN": value}), AutomaticOpenConfig())
            self.assertIn("key=GATE_AUTOMATIC_OPEN status=rejected", "\n".join(logs.output))


class SettingsSectionTests(unittest.TestCase):
    FALLBACK = AutomaticOpenConfig(enabled=True, source="environment")

    def test_only_a_strict_boolean_is_a_setting(self):
        self.assertEqual(config_from_settings({"enabled": False}, self.FALLBACK),
                         AutomaticOpenConfig(enabled=False, source="app"))
        self.assertEqual(config_from_settings({"enabled": True}, self.FALLBACK),
                         AutomaticOpenConfig(enabled=True, source="app"))
        for section in (None, {}, {"enabled": "no"}, {"enabled": 0}, {"enabled": None},
                        "off", 7, [], {"on": False}):
            with self.subTest(section=section):
                self.assertIs(config_from_settings(section, self.FALLBACK), self.FALLBACK)
                self.assertFalse(is_valid_section(section))

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "match-policy.json"

    def test_the_cache_keeps_the_apps_switch_and_a_restart_keeps_it_too(self):
        cache = MatchPolicyCache(self.path)
        cache.replace({**ENVELOPE, "automatic_open": {"enabled": False}})
        self.assertFalse(cache.automatic_open(self.FALLBACK).enabled)
        self.assertEqual(cache.automatic_open(self.FALLBACK).source, "app")
        self.assertEqual(
            json.loads(self.path.with_name("automatic-open.json").read_text()),
            {"automatic_open": {"enabled": False}},
        )

        restarted = MatchPolicyCache(self.path)
        self.assertFalse(restarted.automatic_open(self.FALLBACK).enabled)

    def test_a_malformed_section_keeps_the_previous_one(self):
        """For a switch that can only withhold pulses, a typo must not be the
        thing that resumes them -- and it must not touch the schedule either."""
        cache = MatchPolicyCache(self.path)
        cache.replace({**ENVELOPE, "automatic_open": {"enabled": False}})
        schedule = cache.get()
        for section in ({"enabled": "no"}, {"enabled": 1}, {}, None, "off", []):
            with self.subTest(section=section), self.assertLogs(
                "gate_controller.settings", level="WARNING"
            ):
                cache.replace({**ENVELOPE, "automatic_open": section})
            self.assertFalse(cache.automatic_open(self.FALLBACK).enabled)
            self.assertIs(cache.get(), schedule)
            self.assertIsNone(cache.status()["last_error"])
        self.assertFalse(MatchPolicyCache(self.path).automatic_open(self.FALLBACK).enabled,
                         "a malformed section was persisted over the good one")

    def test_a_pause_known_only_from_the_schedules_cache_survives_a_malformed_update(self):
        """A board whose only copy of the switch is the cached envelope (no side
        file yet): a malformed update must neither persist itself over that copy
        nor strip it away, or a restart would quietly resume automatic opening."""
        cache = MatchPolicyCache(self.path)
        cache.replace({**ENVELOPE, "automatic_open": {"enabled": False}})
        side_file = self.path.with_name("automatic-open.json")
        side_file.unlink()
        self.assertEqual(json.loads(self.path.read_text())["automatic_open"], {"enabled": False})
        loaded = MatchPolicyCache(self.path)
        self.assertFalse(loaded.automatic_open(self.FALLBACK).enabled, "loaded from the envelope")

        with self.assertLogs("gate_controller.settings", level="WARNING"):
            loaded.replace({**ENVELOPE, "automatic_open": {"enabled": "resume"}})

        self.assertFalse(loaded.automatic_open(self.FALLBACK).enabled)
        self.assertNotIn("automatic_open", json.loads(self.path.read_text()),
                         "the malformed section was cached with the schedule")
        self.assertEqual(json.loads(side_file.read_text()), {"automatic_open": {"enabled": False}})
        self.assertFalse(MatchPolicyCache(self.path).automatic_open(self.FALLBACK).enabled)

    def test_an_envelope_without_the_section_means_the_board_decides(self):
        cache = MatchPolicyCache(self.path)
        cache.replace({**ENVELOPE, "automatic_open": {"enabled": False}})
        cache.replace(dict(ENVELOPE))
        self.assertIs(cache.automatic_open(self.FALLBACK), self.FALLBACK)
        self.assertIs(MatchPolicyCache(self.path).automatic_open(self.FALLBACK), self.FALLBACK)

    def test_the_switch_beside_a_refused_schedule_is_still_adopted(self):
        cache = MatchPolicyCache(self.path)
        cache.replace({**ENVELOPE, "plate_matching": {"schema_version": 99},
                       "automatic_open": {"enabled": False}})
        self.assertFalse(cache.automatic_open(self.FALLBACK).enabled)
        self.assertIsNotNone(cache.status()["last_error"], "the schedule was refused")
        self.assertFalse(MatchPolicyCache(self.path).automatic_open(self.FALLBACK).enabled)

    def test_the_left_open_section_is_untouched_by_the_new_one(self):
        from gate_controller.gate_left_open import LeftOpenConfig

        cache = MatchPolicyCache(self.path)
        cache.replace({**ENVELOPE, "gate_left_open": {"enabled": False, "threshold_minutes": 10},
                       "automatic_open": {"enabled": False}})
        self.assertFalse(cache.gate_left_open(LeftOpenConfig()).enabled)
        self.assertFalse(cache.automatic_open(self.FALLBACK).enabled)


if __name__ == "__main__":
    unittest.main()
