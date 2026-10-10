"""No long-running gate service may permanently give up on starting.

On 2026-10-10 the gate's power was cycled. The Pi booted before the powerline
adapter had given eth0 its address, NetworkManager-wait-online failed, and
network-online.target was reached anyway. MediaMTX could not bind
192.168.0.33:8189, failed five times inside its 60 s start limit, and systemd
stopped trying ("Start request repeated too quickly"). The transcoder, which
Required it, stayed dead with it. Nobody had the camera stream or audio for 25
minutes, until someone ran `systemctl reset-failed` by hand.

A unit file is a deployment file, so this reads them: every service that is
meant to stay running must restart on its own, must not have a start limit
that can end its retries, and must not hard-require another unit whose failed
start would end them as a dependency failure.
"""
import configparser
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LONG_RUNNING_TYPES = {"simple", "exec", "notify", "notify-reload", "forking", "dbus"}

# Units deliberately outside this rule, with the reason. Keep this short; an
# entry here is a unit that can still be left dead after a bad boot.
EXEMPT = {
    # The controller itself drives the relay; its unit is changed only in a PR
    # reviewed for that (CLAUDE.md, docs/gate-operator.md). It still has the
    # 5-in-60 s start limit and the same failure mode -- follow-up.
    "file-monitor.service": "drives the relay; change reviewed separately",
}


def _units():
    paths = sorted((ROOT / "deployment" / "systemd").glob("*.service"))
    paths += sorted(ROOT.glob("*.service"))
    return paths


def _read(path):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read(path, encoding="utf-8")
    return parser


def _long_running(parser):
    return parser.get("Service", "Type", fallback="simple") in LONG_RUNNING_TYPES


class RestartPolicyTests(unittest.TestCase):
    def test_the_media_and_camera_services_are_among_the_units_checked(self):
        checked = {path.name for path in _units() if _long_running(_read(path))}
        for name in (
            "gate-media-auth.service", "gate-media-gateway.service",
            "gate-media-transcoder.service", "gate-camera-control.service",
        ):
            self.assertIn(name, checked)

    def test_every_exemption_names_a_unit_that_still_exists(self):
        names = {path.name for path in _units()}
        for name in EXEMPT:
            self.assertIn(name, names, f"{name} is exempt but no longer exists")

    def test_no_long_running_service_can_permanently_give_up_starting(self):
        for path in _units():
            parser = _read(path)
            if not _long_running(parser) or path.name in EXEMPT:
                continue
            with self.subTest(unit=path.name):
                self.assertEqual("always", parser.get("Service", "Restart", fallback="no"))
                # 0 disables the rate limit; any other interval with the
                # default burst can end the retries for good.
                self.assertEqual(
                    "0", parser.get("Unit", "StartLimitIntervalSec", fallback=None),
                    "set StartLimitIntervalSec=0 in [Unit]",
                )
                self.assertNotIn("StartLimitAction", parser["Unit"])
                restart_seconds = parser.get("Service", "RestartSec", fallback="100ms")
                self.assertRegex(restart_seconds, r"^([5-9]|[1-5][0-9]|60)s$",
                                 "pace retries between 5 s and 60 s")

    def test_no_long_running_service_hard_requires_another_gate_unit(self):
        # A Requires=/BindsTo= on another gate unit turns any failed start of
        # that unit -- a start limit, a missing file, one bad attempt before
        # it recovers on its own -- into a dependency failure here, which
        # Restart= never retries: the dependent stays failed even after the
        # prerequisite has restarted successfully. On 2026-10-10 the
        # transcoder was stranded exactly so. Use Wants= with After=.
        for path in _units():
            parser = _read(path)
            if not _long_running(parser) or path.name in EXEMPT:
                continue
            for key in ("Requires", "BindsTo"):
                for required in parser.get("Unit", key, fallback="").split():
                    with self.subTest(unit=path.name, key=key, requires=required):
                        self.assertFalse(required.startswith("gate-"),
                                         f"{path.name} {key}={required}")


class OneshotRestartTests(unittest.TestCase):
    def test_no_oneshot_unit_uses_a_restart_setting_systemd_refuses(self):
        # systemd 252 (the Pi) through 255 refuses RestartForceExitStatus= on
        # a Type=oneshot unit, and Restart= other than no/on-failure, with
        # "bad unit file setting": the unit does not load and its timer never
        # runs it. Measured with systemd-analyze verify on the Pi, 2026-10-10.
        for path in _units():
            parser = _read(path)
            if parser.get("Service", "Type", fallback="simple") != "oneshot":
                continue
            with self.subTest(unit=path.name):
                self.assertNotIn("RestartForceExitStatus", parser["Service"])
                self.assertIn(parser.get("Service", "Restart", fallback="no"),
                              ("no", "on-failure"))
