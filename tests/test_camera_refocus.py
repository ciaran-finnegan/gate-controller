"""The camera-control service's zoom nudge, driven over its real HTTP surface.

Every test here goes through `CameraControlServer` -> `CameraControlService`
-> `RefocusController` -> the real `ReolinkClient` -> a fake RLC-811A api.cgi
on a socket, which is the path a refocus takes in production. Only the clock
and the settle sleeps are the test's.
"""

import json
import os
import stat
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

from gate_camera_control.__main__ import CameraControlServer, build_service
from gate_camera_control.focus import (
    MAX_PER_DAY, MIN_INTERVAL_SECONDS, RETURN_RETRY_DELAYS, SETTLE_SECONDS,
)
from gate_camera_control.reolink import ZOOM_MAX, ReolinkClient
from tests.test_camera_control import FakeCamera, ManualClock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class LevelJournal:
    """A logger that keeps the level each line was written at."""

    def __init__(self):
        self.lines = []

    def info(self, message):
        self.lines.append(("info", message))

    def warning(self, message):
        self.lines.append(("warning", message))

    def error(self, message):
        self.lines.append(("error", message))

    def find(self, stage):
        return [
            (level, line) for level, line in self.lines
            if f"stage={stage} " in f"{line} "
        ]


class RecordingSleep:
    """Settle waits advance the shared clock instead of the wall clock."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.hook = None

    def __call__(self, seconds):
        self.calls.append(seconds)
        if self.hook is not None:
            self.hook(seconds)
        self.clock.advance(seconds)


class RefocusHttpTests(unittest.TestCase):
    def setUp(self):
        self.camera = FakeCamera()
        self.addCleanup(self.camera.close)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.journal = LevelJournal()
        self.clock = ManualClock()
        self.sleep = RecordingSleep(self.clock)
        self.record_path = Path(self.directory.name) / "refocus-return.json"
        self.service = self.build()
        self.server = CameraControlServer(("127.0.0.1", 0), self.service, logger=self.journal)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def build(self):
        return build_service(
            {
                "GATE_CAMERA_HOST": self.camera.host,
                "GATE_CAMERA_USERNAME": "gate",
                "GATE_CAMERA_PASSWORD": "s3cret",
                "GATE_CAMERA_IR_DEFAULT": "Off",
                "GATE_CAMERA_IR_LEASE_DEFAULT_MINUTES": "10",
                "GATE_CAMERA_IR_LEASE_MAX_MINUTES": "60",
            },
            token_path=Path(self.directory.name) / "token.json",
            lease_path=Path(self.directory.name) / "lease.json",
            spotlight_lease_path=Path(self.directory.name) / "spotlight-lease.json",
            refocus_record_path=self.record_path,
            logger=self.journal,
            connection_factory=self.camera.connection_factory,
            clock=self.clock,
            refocus_options={"sleep": self.sleep},
        )

    def request(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (
            None if body is None else json.dumps(body).encode("utf-8")
        )
        request = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method,
            headers={} if data is None else {"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def refocus(self, body=None):
        status, headers, payload = self.request(
            "POST", "/camera/refocus", body if body is not None else {"reason": "daily"},
        )
        return status, (json.loads(payload) if payload else None), headers

    # -- the nudge -----------------------------------------------------------
    def test_a_nudge_steps_one_position_and_returns_to_exactly_where_it_started(self):
        status, body, headers = self.refocus()

        self.assertEqual(200, status, body)
        self.assertEqual("no-store", headers["Cache-Control"])
        self.assertEqual("completed", body["status"])
        self.assertEqual({"before": 2, "stepped_to": 3, "after": 2}, body["zoom"])
        # The camera's own autofocus ran on the way back: 86 -> 78, the
        # 2026-10-05 numbers.
        self.assertEqual({"before": 86, "after": 78}, body["focus"])
        self.assertEqual(2, self.camera.zoom_pos)
        self.assertEqual(
            [{"channel": 0, "op": "ZoomPos", "pos": 3}, {"channel": 0, "op": "ZoomPos", "pos": 2}],
            self.camera.zoom_ops,
        )
        # Read, step, read back, return, read back: and never a focus write.
        self.assertEqual(
            ["GetZoomFocus", "StartZoomFocus", "GetZoomFocus", "StartZoomFocus", "GetZoomFocus"],
            [c for c in self.camera.commands if "Zoom" in c],
        )
        self.assertEqual([SETTLE_SECONDS, SETTLE_SECONDS], self.sleep.calls)
        self.assertFalse(self.record_path.exists())
        [(level, line)] = self.journal.find("refocus")
        self.assertEqual("info", level)
        for field in ("outcome=completed", "reason=daily", "zoom_before=2", "zoom_step=3",
                      "zoom_after=2", "focus_before=86", "focus_after=78"):
            self.assertIn(field, line)

    def test_at_the_long_end_the_nudge_steps_down_and_comes_back(self):
        self.camera.zoom_pos = ZOOM_MAX
        self.camera.focus_after_zoom = {ZOOM_MAX: 200, ZOOM_MAX - 1: 190}

        status, body, _ = self.refocus()

        self.assertEqual(200, status, body)
        self.assertEqual({"before": ZOOM_MAX, "stepped_to": ZOOM_MAX - 1, "after": ZOOM_MAX},
                         body["zoom"])
        self.assertEqual(ZOOM_MAX, self.camera.zoom_pos)

    def test_the_position_is_on_durable_storage_before_the_lens_moves(self):
        seen = []

        def at_step(_seconds):
            if not seen:
                seen.append((json.loads(self.record_path.read_text()),
                             stat.S_IMODE(os.stat(self.record_path).st_mode),
                             self.camera.zoom_pos))

        self.sleep.hook = at_step
        status, _body, _ = self.refocus()

        self.assertEqual(200, status)
        record, mode, zoom_while_stepped = seen[0]
        self.assertEqual(3, zoom_while_stepped)
        self.assertEqual(2, record["zoom"])
        self.assertEqual(0o600, mode)
        self.assertFalse(self.record_path.exists())

    def test_a_step_the_camera_refuses_still_returns_and_proves_the_zoom(self):
        self.camera.zoom_refuse = True

        def unrefuse(_seconds):
            self.camera.zoom_refuse = False

        self.sleep.hook = unrefuse
        status, body, _ = self.refocus()

        self.assertEqual(200, status, body)
        self.assertEqual("step_failed", body["status"])
        self.assertEqual(2, body["zoom"]["after"])
        self.assertEqual(2, self.camera.zoom_pos)

    # -- a return that will not take ------------------------------------------
    def test_a_return_that_fails_is_retried_reported_loudly_and_finished_in_the_background(self):
        def stick_after_step(_seconds):
            # The step went through; from now on the motor ignores moves.
            self.camera.zoom_stuck = True

        self.sleep.hook = stick_after_step
        status, body, _ = self.refocus()

        self.assertEqual(502, status)
        self.assertEqual({"error": "zoom_return_failed",
                          "zoom": {"expected": 2, "observed": 3}}, body)
        # Every in-request retry was taken, the last one past the 60 s breaker.
        for delay in RETURN_RETRY_DELAYS:
            self.assertIn(delay, self.sleep.calls)
        self.assertGreater(max(RETURN_RETRY_DELAYS), 60)
        failed = self.journal.find("refocus_return_failed")
        self.assertEqual("error", failed[0][0])
        self.assertIn("expected=2", failed[0][1])
        self.assertIn("observed=3", failed[0][1])
        # The record stays until the zoom is proven back.
        self.assertEqual(2, json.loads(self.record_path.read_text())["zoom"])

        # No further nudge while the zoom is not where it started.
        self.clock.advance(MIN_INTERVAL_SECONDS + 1)
        status, body, _ = self.refocus()
        self.assertEqual(409, status)
        self.assertEqual({"error": "refocus_busy", "reason": "zoom_return_pending"}, body)

        # The background worker gets it back once the motor answers.
        self.sleep.hook = None
        self.camera.zoom_stuck = False
        self.assertTrue(self.service.refocus_controller.run_due_return())
        self.assertEqual(2, self.camera.zoom_pos)
        self.assertFalse(self.record_path.exists())
        self.assertFalse(self.service.refocus_controller.return_pending())
        status, body, _ = self.refocus()
        self.assertEqual(200, status, body)

    def test_a_restart_part_way_through_a_nudge_puts_the_zoom_back_first(self):
        # The previous process wrote its record and stepped, then died.
        self.record_path.write_text(json.dumps({"zoom": 2, "focus": 86, "started_at": 1.0}))
        os.chmod(self.record_path, 0o600)
        self.camera.zoom_pos = 3

        restarted = self.build()
        restarted.refocus_controller.restore_on_start()

        self.assertEqual(2, self.camera.zoom_pos)
        self.assertFalse(self.record_path.exists())
        self.assertEqual("warning", self.journal.find("startup_zoom_return")[0][0])

    def test_a_restart_with_no_record_never_moves_the_lens(self):
        self.camera.zoom_pos = 5

        self.build().refocus_controller.restore_on_start()

        self.assertEqual([], self.camera.zoom_ops)
        self.assertEqual(5, self.camera.zoom_pos)

    def test_a_record_that_cannot_be_read_moves_nothing_and_says_so(self):
        self.record_path.write_text("{not json")
        self.camera.zoom_pos = 3

        self.build().refocus_controller.restore_on_start()

        self.assertEqual([], self.camera.zoom_ops)
        self.assertEqual("warning", self.journal.find("refocus_record_corrupt")[0][0])

    # -- budgets ----------------------------------------------------------------
    def test_nudges_are_spaced_and_capped_per_day_service_wide(self):
        self.assertEqual(200, self.refocus()[0])

        status, body, headers = self.refocus()
        self.assertEqual(429, status)
        self.assertEqual("rate_limited", body["error"])
        self.assertGreater(int(headers["Retry-After"]), 0)
        self.assertEqual(2, len(self.camera.zoom_ops))  # nothing new was sent

        for _ in range(MAX_PER_DAY - 1):
            self.clock.advance(MIN_INTERVAL_SECONDS)
            self.assertEqual(200, self.refocus()[0])
        self.clock.advance(MIN_INTERVAL_SECONDS)
        status, body, _ = self.refocus()
        self.assertEqual(429, status)
        self.assertEqual(MAX_PER_DAY * 2, len(self.camera.zoom_ops))

    def test_a_second_nudge_while_one_runs_is_refused_not_queued(self):
        answers = []

        def overlap(_seconds):
            if not answers:
                answers.append(self.refocus())

        self.sleep.hook = overlap
        status, _body, _ = self.refocus()

        self.assertEqual(200, status)
        self.assertEqual(409, answers[0][0])
        self.assertEqual({"error": "refocus_busy", "reason": "in_progress"}, answers[0][1])
        self.assertEqual(2, len(self.camera.zoom_ops))

    # -- the request ------------------------------------------------------------
    def test_the_body_may_say_why_but_never_where(self):
        for body in ({"reason": "elsewhere"}, {"pos": 5}, {"reason": "daily", "zoom": 3},
                     {"op": "FocusPos"}, ["daily"]):
            with self.subTest(body=body):
                status, payload, _ = self.refocus(body)
                self.assertEqual(400, status)
                self.assertEqual({"error": "invalid_request"}, payload)
        self.assertEqual([], self.camera.zoom_ops)

    def test_an_empty_post_is_a_manual_nudge(self):
        status, _headers, payload = self.request("POST", "/camera/refocus", raw=b"")

        self.assertEqual(200, status)
        self.assertEqual("manual", json.loads(payload)["reason"])

    def test_only_post_is_served_and_no_query_is_taken(self):
        for method in ("GET", "HEAD", "PUT", "DELETE"):
            with self.subTest(method=method):
                status, _headers, _payload = self.request(method, "/camera/refocus")
                self.assertEqual(405, status)
        status, _headers, _payload = self.request("POST", "/camera/refocus?pos=5", {})
        self.assertEqual(404, status)
        self.assertEqual([], self.camera.zoom_ops)

    def test_an_unusable_lens_reading_moves_nothing(self):
        self.camera.zoom_focus_value = {"ZoomFocus": {"zoom": {"pos": 2}, "focus": {"pos": 999}}}

        status, body, _ = self.refocus()

        self.assertEqual(502, status)
        self.assertEqual({"error": "camera_error"}, body)
        self.assertEqual([], self.camera.zoom_ops)


class ZoomCommandSurfaceTests(unittest.TestCase):
    def test_the_client_can_move_only_the_zoom_and_only_within_range(self):
        camera = FakeCamera()
        self.addCleanup(camera.close)
        client = ReolinkClient(camera.host, "gate", "s3cret",
                               connection_factory=camera.connection_factory)
        for position in (-1, ZOOM_MAX + 1, True, 2.0, "3"):
            with self.subTest(position=position), self.assertRaises(ValueError):
                client.zoom_to(position)
        self.assertEqual([], camera.zoom_ops)

    def test_no_focus_position_or_autofocus_change_exists_anywhere_in_the_service(self):
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (REPOSITORY_ROOT / "gate_camera_control").glob("*.py")
        )
        for forbidden in ("FocusPos", "SetAutoFocus", "SetZoomFocus", '"op": "Focus'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
        self.assertEqual(1, source.count('"op": "ZoomPos"'))

    def test_the_setup_script_still_sends_no_zoom_command_of_any_kind(self):
        script = (REPOSITORY_ROOT / "scripts/reolink/configure-rlc811a.py").read_text(
            encoding="utf-8")
        self.assertIn('FORBIDDEN_COMMANDS = ("SetTime", "SetZoomFocus", "StartZoomFocus", '
                      '"SetAutoFocus")', script)

    def test_the_installer_publishes_the_focus_module(self):
        installer = (REPOSITORY_ROOT / "deployment/install-camera-control.sh").read_text(
            encoding="utf-8")
        self.assertRegex(installer, r"for module in [^;]*\bfocus\b[^;]*; do")


if __name__ == "__main__":
    unittest.main()
