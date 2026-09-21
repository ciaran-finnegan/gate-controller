"""The RLC-811A setup script must reproduce *today's* camera configuration.

The script was written on the day of the cutover, 2026-09-11. Several of the
settings it wrote have since been changed deliberately, on measurement:
`Isp.constantFrameRate` 2 made the encoder drop frames, 10 fps at a fixed
6144 kbit/s spent the bitrate on pictures nobody read, and the camera clock
turned out to belong to `gate-camera-control` rather than to whoever last
opened the camera's web UI. A script that still carries the cutover-day values
is not a stale document, it is a loaded gun: running it puts the camera back.

So these tests drive the real entry point in its dry-run mode against saved
camera blocks (`--from-capture`), and assert on the list of writes it intends.
Nothing here reaches a camera, and nothing here reaches a helper the entry
point does not itself use.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPOSITORY_ROOT / "scripts" / "reolink" / "configure-rlc811a.py"
FIXTURES = REPOSITORY_ROOT / "tests" / "fixtures"
AS_FITTED = FIXTURES / "rlc811a-as-fitted"
CONFIGURED = FIXTURES / "rlc811a-configured"
OLD_FTP_BACKUP = AS_FITTED / "old-ftp-backup.json"

# Not credentials: the fixture camera's webhook slot carries this same string, so
# a configured camera reads back as already configured.
FIXTURE_SECRET = "PLACEHOLDERSECRET0123456789"
FIXTURE_FTP_PASSWORD = "fixtureFtpPasswordNotReal"


def param(step, *path):
    """Walk into a planned write's parameters."""
    value = step["param"]
    for key in path:
        value = value[key]
    return value


class DryRun:
    """One `--from-capture` run of the script, and what it intended to write."""

    def __init__(self, capture, source_fps="6", secret=FIXTURE_SECRET, extra=()):
        self.capture = capture
        self.source_fps = source_fps
        self.secret = secret
        self.extra = list(extra)

    def __enter__(self):
        self._temporary = tempfile.TemporaryDirectory()
        workspace = Path(self._temporary.name)
        controller_env = workspace / "gate-controller.env"
        controller_env.write_text(
            "GATE_REOLINK_WEBHOOK_SECRET=%s\n"
            "GATE_CLEAR_STREAM_SOURCE_FPS=%s\n"
            "GATE_TRIGGER_CAPTURE_DELAY_SECONDS=0\n" % (self.secret, self.source_fps),
            encoding="utf-8",
        )
        credentials = workspace / "ftp-user.credentials"
        credentials.write_text(
            "FTP_USER=ftp-user\nFTP_PASSWORD=%s\n" % FIXTURE_FTP_PASSWORD, encoding="utf-8"
        )
        plan_path = workspace / "plan.json"
        self.completed = subprocess.run(
            [sys.executable, str(SCRIPT),
             "--from-capture", str(self.capture),
             "--plan-json", str(plan_path),
             "--controller-env", str(controller_env),
             "--ftp-credentials", str(credentials),
             "--old-ftp-backup", str(OLD_FTP_BACKUP)] + self.extra,
            capture_output=True, text=True, timeout=120,
        )
        self.stdout = self.completed.stdout
        self.plan_text = plan_path.read_text(encoding="utf-8") if plan_path.exists() else ""
        self.plan = json.loads(self.plan_text) if self.plan_text else []
        return self

    def __exit__(self, *exception):
        self._temporary.cleanup()
        return False

    def commands(self):
        return [step["cmd"] for step in self.plan]

    def only(self, cmd):
        matching = [step for step in self.plan if step["cmd"] == cmd]
        assert len(matching) == 1, "expected exactly one %s, got %d" % (cmd, len(matching))
        return matching[0]


class TheScriptRuns(unittest.TestCase):
    def test_a_rehearsal_against_saved_blocks_succeeds_and_plans_writes(self):
        with DryRun(AS_FITTED) as run:
            self.assertEqual(run.completed.returncode, 0, run.completed.stderr)
            self.assertIn("DRY RUN: nothing was written", run.stdout)
            self.assertTrue(run.plan, "a camera as fitted needs writes")

    def test_it_refuses_to_write_to_a_camera_while_rehearsing(self):
        """--apply reads a real camera; a capture run must not be able to reach one."""
        with DryRun(AS_FITTED, extra=["--apply"]) as run:
            self.assertNotEqual(run.completed.returncode, 0)

    def test_it_refuses_a_password_on_the_command_line(self):
        """Anything in argv is visible in `ps` to every user on the Pi."""
        with DryRun(AS_FITTED, extra=["--password", "hunter2"]) as run:
            self.assertNotEqual(run.completed.returncode, 0)
            self.assertNotIn("hunter2", run.stdout)


class TheEncoderSettings(unittest.TestCase):
    """6 fps at 6144 kbit/s with a keyframe every second, measured 2026-09-17."""

    def test_the_main_stream_is_six_frames_at_6144_with_gop_one(self):
        with DryRun(AS_FITTED) as run:
            main = param(run.only("SetEnc"), "Enc", "mainStream")

        self.assertEqual(main["frameRate"], 6)
        self.assertEqual(main["bitRate"], 6144)
        self.assertEqual(main["gop"], 1)

    def test_the_streams_carry_audio(self):
        with DryRun(AS_FITTED) as run:
            self.assertEqual(param(run.only("SetEnc"), "Enc", "audio"), 1)

    def test_the_frame_rate_mode_is_constant(self):
        """At 2 the encoder drops frames on a quiet scene, and an empty
        driveway always is one: 10 fps configured delivered about 5. The setting
        reads back exactly as asked either way, so only this pin catches it."""
        with DryRun(AS_FITTED) as run:
            self.assertEqual(param(run.only("SetIsp"), "Isp", "constantFrameRate"), 1)

    def test_it_says_so_when_the_pi_is_told_a_different_source_rate(self):
        """GATE_CLEAR_STREAM_SOURCE_FPS must move with the camera: the session
        decoder reads a pipe with no timestamps and believes what it is told."""
        with DryRun(AS_FITTED, source_fps="10") as run:
            self.assertIn("GATE_CLEAR_STREAM_SOURCE_FPS", run.stdout)
            self.assertIn("WARNING", run.stdout)

        with DryRun(AS_FITTED, source_fps="6") as run:
            self.assertNotIn("WARNING: GATE_CLEAR_STREAM_SOURCE_FPS", run.stdout)


class TheExposureSettings(unittest.TestCase):
    def test_manual_quarter_millisecond_shutter_at_gain_sixteen_in_colour(self):
        with DryRun(AS_FITTED) as run:
            isp = param(run.only("SetIsp"), "Isp")

        self.assertEqual(isp["exposure"], "Manual")
        self.assertEqual(isp["shutter"], {"min": 4, "max": 4})
        self.assertEqual(isp["gain"], {"min": 16, "max": 16})
        self.assertEqual(isp["dayNight"], "Color")
        self.assertEqual(isp["hdr"], 0)

    def test_the_illuminators_are_off(self):
        with DryRun(AS_FITTED) as run:
            self.assertEqual(param(run.only("SetIrLights"), "IrLights", "state"), "Off")
            self.assertEqual(param(run.only("SetWhiteLed"), "WhiteLed", "state"), 0)


class TheClock(unittest.TestCase):
    """The camera was two hours out for four days after the cutover."""

    def test_ntp_is_turned_on(self):
        with DryRun(AS_FITTED) as run:
            ntp = param(run.only("SetNtp"), "Ntp")

        self.assertEqual(ntp["enable"], 1)

    def test_the_time_is_never_set_by_hand(self):
        """SetTime carries the *displayed* time and the firmware adds the DST
        hour on top, which is how the clock got two hours out. gate-camera-control
        owns the clock hourly; a write from here would fight it."""
        with DryRun(AS_FITTED) as run:
            self.assertNotIn("SetTime", run.commands())
            self.assertNotIn("Dst", run.plan_text)

    def test_a_camera_displaying_local_time_is_reported_not_corrected(self):
        with DryRun(AS_FITTED) as run:  # the fixture has DST on, as fitted
            self.assertIn("timeZone 0 with DST off", run.stdout)
            self.assertNotIn("SetTime", run.commands())


class TheLens(unittest.TestCase):
    def test_no_zoom_or_focus_position_is_ever_written(self):
        """The zoom is being re-derived from a physical re-aim. A position
        written from here would undo that silently and without a trace."""
        with DryRun(AS_FITTED) as run:
            for command in run.commands():
                self.assertNotIn("Zoom", command)
                self.assertNotIn("Focus", command)
            self.assertNotIn("ZoomFocus", run.plan_text)


class TheWebhookAndAlarms(unittest.TestCase):
    def test_the_webhook_asks_for_the_default_body(self):
        """A custom body is one the controller cannot parse, and bCustom is not
        something the read-back can skip: the firmware may keep it."""
        with DryRun(AS_FITTED) as run:
            hook = param(run.only("SetWebHook"), "WebHook")

        self.assertEqual(hook["indexEnable"], 1)
        self.assertEqual(hook["bCustom"], 0)
        self.assertEqual(hook["hookBody"], "")

    def test_vehicle_detection_keeps_the_frozen_sensitivity(self):
        with DryRun(AS_FITTED) as run:
            self.assertEqual(param(run.only("SetAiAlarm"), "AiAlarm", "sensitivity"), 80)

    def test_the_alarm_schedules_are_vehicle_only(self):
        with DryRun(AS_FITTED) as run:
            schedule = param(run.only("SetPushV20"), "Push", "schedule", "table")

        self.assertEqual(schedule["AI_VEHICLE"], "<168-char table, ones=168>")
        self.assertEqual(schedule["AI_PEOPLE"], "<168-char table, ones=0>")
        self.assertEqual(schedule["MD"], "<168-char table, ones=0>")


class TheConfiguredCamera(unittest.TestCase):
    """Run against the camera as it stands, the script must be a no-op.

    The `rlc811a-configured` capture holds the encoder, ISP, NTP, time and light
    blocks read from the gate camera on 2026-09-21, and the remaining blocks at
    the values this script intends. If a pinned value here ever drifts from the
    camera, this is the test that says so.
    """

    def test_it_rewrites_nothing_that_is_already_deliberate(self):
        with DryRun(CONFIGURED) as run:
            planned = run.commands()

        for command in ("SetEnc", "SetIsp", "SetNtp", "SetIrLights", "SetWhiteLed",
                        "SetWebHook", "SetPushV20", "SetPushCfg", "SetAiAlarm"):
            self.assertNotIn(command, planned)

    def test_only_the_ftp_credentials_are_rewritten(self):
        """`GetFtpV20` masks userName and password in every answer, so the
        script can never see that they already match and always rewrites them."""
        with DryRun(CONFIGURED) as run:
            self.assertEqual(run.commands(), ["SetFtpV20"])


class Credentials(unittest.TestCase):
    def test_no_secret_reaches_the_plan_file_or_the_console(self):
        with DryRun(AS_FITTED) as run:
            for secret in (FIXTURE_SECRET, FIXTURE_FTP_PASSWORD):
                self.assertNotIn(secret, run.plan_text)
                self.assertNotIn(secret, run.stdout)
                self.assertNotIn(secret, run.completed.stderr)

    def test_the_script_takes_no_credential_arguments(self):
        source = SCRIPT.read_text(encoding="utf-8")

        self.assertNotIn('add_argument("--password"', source)
        self.assertNotIn('add_argument("--secret"', source)


if __name__ == "__main__":
    unittest.main()
