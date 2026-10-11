"""The night-vision lease: the ISP is switched for IR, and put back exactly.

Every test here goes through `CameraControlServer` -> `CameraControlService`
-> `IrController` + `NightModeController` -> the real `ReolinkClient` -> a fake
RLC-811A api.cgi on a socket -- the path the app's Night vision tile takes. The
fake camera starts in the configuration read live on 2026-10-11 (`LIVE_ISP`:
Manual 1/250 s, gain 16, `dayNight` Color), which is the picture every lease
must leave behind. Only the clock is the test's.
"""

import copy
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
from gate_camera_control.night import RESTORE_RETRY_SECONDS
from gate_camera_control.reolink import NIGHT_DAY_NIGHT, CameraError
from tests.test_camera_control import LIVE_ISP, FakeCamera, ManualClock
from tests.test_camera_refocus import LevelJournal

NIGHT_ISP = {**copy.deepcopy(LIVE_ISP), "dayNight": NIGHT_DAY_NIGHT}
AUDIT_ID = "0d8f5a1c-4b2e-4e9a-9f1b-3c7d2e6a8b90"


class NightVisionLeaseTests(unittest.TestCase):
    def setUp(self):
        self.camera = FakeCamera()
        self.addCleanup(self.camera.close)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.journal = LevelJournal()
        self.clock = ManualClock()
        self.record_path = Path(self.directory.name) / "night-isp.json"
        self.lease_path = Path(self.directory.name) / "lease.json"
        self.service = self.build()
        self.server = CameraControlServer(("127.0.0.1", 0), self.service, logger=self.journal)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def build(self, **settings):
        environment = {
            "GATE_CAMERA_HOST": self.camera.host,
            "GATE_CAMERA_USERNAME": "gate",
            "GATE_CAMERA_PASSWORD": "s3cret",
            "GATE_CAMERA_IR_DEFAULT": "Off",
            "GATE_CAMERA_IR_LEASE_DEFAULT_MINUTES": "10",
            "GATE_CAMERA_IR_LEASE_MAX_MINUTES": "60",
        }
        environment.update(settings)
        return build_service(
            environment,
            token_path=Path(self.directory.name) / "token.json",
            lease_path=self.lease_path,
            spotlight_lease_path=Path(self.directory.name) / "spotlight-lease.json",
            refocus_record_path=Path(self.directory.name) / "refocus-return.json",
            night_record_path=self.record_path,
            logger=self.journal,
            connection_factory=self.camera.connection_factory,
            clock=self.clock,
        )

    def request(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method,
            headers={} if data is None else {"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def lease(self, minutes=10, key=AUDIT_ID):
        body = {"state": "Auto", "lease_minutes": minutes}
        if key is not None:
            body["idempotency_key"] = key
        return self.request("POST", "/camera/ir", body)

    def camera_writes(self):
        return [c for c in self.camera.commands if c in ("GetIsp", "SetIsp", "SetIrLights")]

    def record(self):
        return json.loads(self.record_path.read_text(encoding="utf-8"))

    # -- the lease --------------------------------------------------------------
    def test_a_lease_snapshots_the_isp_to_disk_then_switches_to_night_then_lights_ir(self):
        seen = []

        def at_isp_write(payload):
            # The moment SetIsp arrives, the snapshot is already on disk and is
            # exactly the block the camera still holds.
            seen.append((self.record(), stat.S_IMODE(os.stat(self.record_path).st_mode),
                         copy.deepcopy(self.camera.isp), self.camera.ir_state))

        self.camera.hooks["SetIsp"] = at_isp_write
        status, body = self.lease()

        self.assertEqual(200, status, body)
        self.assertEqual("completed", body["status"])
        self.assertEqual("Auto", body["ir"]["state"])
        self.assertEqual(600, body["ir"]["lease_seconds_remaining"])
        self.assertEqual({"enabled": True, "active": True, "restore_failed": False,
                          "last_error": None}, body["night_vision"])
        self.assertEqual(["GetIsp", "SetIsp", "SetIrLights"], self.camera_writes())
        record, mode, isp_at_write, ir_at_write = seen[0]
        self.assertEqual(LIVE_ISP, record["isp"])
        self.assertEqual(0o600, mode)
        self.assertEqual(LIVE_ISP, isp_at_write)
        self.assertEqual("Off", ir_at_write)
        # Exactly one field differs from what was read, and it is dayNight.
        self.assertEqual([NIGHT_ISP], self.camera.isp_writes)
        self.assertEqual(NIGHT_ISP, self.camera.isp)
        self.assertEqual("Auto", self.camera.ir_state)
        self.assertTrue(self.record_path.exists())
        [(_, snapshot_line)] = self.journal.find("isp_snapshot")
        self.assertIn("day_night=Color", snapshot_line)
        [(level, night_line)] = self.journal.find("isp_night")
        self.assertEqual("info", level)
        self.assertIn("outcome=completed", night_line)
        self.assertIn(f"key={AUDIT_ID[:12]}", night_line)
        [(_, set_line)] = self.journal.find("ir_set")
        self.assertIn("night_mode=true", set_line)
        self.assertIn(f"key={AUDIT_ID[:12]}", set_line)

    def test_expiry_restores_the_isp_exactly_as_it_was_read_then_turns_ir_off(self):
        self.lease(minutes=10)
        self.camera.commands.clear()

        self.clock.advance(599)
        self.assertFalse(self.service.controller.run_due_revert())
        self.assertEqual(NIGHT_ISP, self.camera.isp)
        self.clock.advance(2)
        self.assertTrue(self.service.controller.run_due_revert())

        self.assertEqual(LIVE_ISP, self.camera.isp)
        self.assertEqual("Off", self.camera.ir_state)
        self.assertEqual(["SetIsp", "SetIrLights"], self.camera_writes())
        self.assertEqual(LIVE_ISP, self.camera.isp_writes[-1])
        self.assertFalse(self.record_path.exists())
        self.assertFalse(self.lease_path.exists())
        [(_, restore)] = self.journal.find("isp_restore")
        self.assertIn("outcome=completed reason=expired", restore)
        [(_, revert)] = self.journal.find("ir_revert")
        self.assertIn("outcome=completed reason=expired state=Off", revert)
        status, body = self.request("GET", "/camera/state")
        self.assertEqual({"enabled": True, "active": False, "restore_failed": False,
                          "last_error": None}, body["night_vision"])
        self.assertIsNone(body["ir"]["effective_until"])

    def test_an_off_from_the_app_restores_the_isp_first_and_names_the_request(self):
        self.lease(key="lease-" + AUDIT_ID)
        self.camera.commands.clear()

        status, body = self.request("POST", "/camera/ir", {
            "state": "Off", "idempotency_key": "off-" + AUDIT_ID,
        })

        self.assertEqual(200, status, body)
        self.assertEqual(LIVE_ISP, self.camera.isp)
        self.assertEqual("Off", self.camera.ir_state)
        self.assertEqual(["SetIsp", "SetIrLights"], self.camera_writes())
        self.assertFalse(self.record_path.exists())
        self.assertFalse(body["night_vision"]["active"])
        [(_, restore)] = self.journal.find("isp_restore")
        self.assertIn("outcome=completed reason=requested", restore)
        self.assertIn(f"key={('off-' + AUDIT_ID)[:12]}", restore)
        # An operator's Off and an expiry are different lines.
        [(_, revert)] = self.journal.find("ir_revert")
        self.assertIn("reason=requested", revert)
        self.assertNotIn("reason=expired", revert)
        self.assertIn(f"key={('off-' + AUDIT_ID)[:12]}", revert)

    def test_a_lease_extended_from_the_app_does_not_snapshot_the_night_values(self):
        self.lease(minutes=5)
        self.camera.commands.clear()
        self.clock.advance(120)

        status, body = self.lease(minutes=10, key="second-" + AUDIT_ID)

        self.assertEqual(200, status, body)
        self.assertEqual(600, body["ir"]["lease_seconds_remaining"])
        # No second read: the snapshot stays the picture from before the lease.
        self.assertNotIn("GetIsp", self.camera_writes())
        self.assertEqual(LIVE_ISP, self.record()["isp"])
        self.assertEqual(1, len(self.journal.find("isp_snapshot")))
        self.assertEqual(NIGHT_ISP, self.camera.isp)

        self.clock.advance(601)
        self.assertTrue(self.service.controller.run_due_revert())
        self.assertEqual(LIVE_ISP, self.camera.isp)
        self.assertEqual("Off", self.camera.ir_state)

    def test_nothing_is_ever_written_to_the_isp_that_was_not_in_the_snapshot(self):
        # A camera found with its own, different settings: the lease must hand
        # those back, not the ones in any script or document.
        self.camera.isp = {**copy.deepcopy(LIVE_ISP), "exposure": "Auto", "dayNight": "Auto",
                           "shutter": {"max": 8, "min": 0}, "gain": {"max": 62, "min": 1},
                           "antiFlicker": "50HZ"}
        found = copy.deepcopy(self.camera.isp)

        self.lease()
        self.clock.advance(601)
        self.assertTrue(self.service.controller.run_due_revert())

        self.assertEqual([{**found, "dayNight": NIGHT_DAY_NIGHT}, found], self.camera.isp_writes)
        self.assertEqual(found, self.camera.isp)

    # -- restarts ----------------------------------------------------------------
    def test_a_restart_mid_lease_restores_the_isp_before_anything_else(self):
        self.lease(minutes=30)
        self.assertEqual(NIGHT_ISP, self.camera.isp)
        self.assertEqual("Auto", self.camera.ir_state)
        self.camera.commands.clear()

        # The process dies mid-lease. A fresh service reads both records.
        restarted = self.build()
        restarted.night_mode.restore_on_start()
        self.assertEqual(LIVE_ISP, self.camera.isp)
        self.assertEqual("Auto", self.camera.ir_state)  # the picture first
        restarted.controller.restore_default_on_start()

        self.assertEqual("Off", self.camera.ir_state)
        self.assertEqual(["SetIsp", "SetIrLights"], self.camera_writes())
        self.assertFalse(self.record_path.exists())
        self.assertFalse(self.lease_path.exists())
        self.assertEqual("warning", self.journal.find("startup_isp_restore")[0][0])
        [(_, restore)] = self.journal.find("isp_restore")
        self.assertIn("outcome=completed reason=startup", restore)
        # The announcement, then the completed revert with its reason.
        self.assertIn("reason=startup", self.journal.find("startup_revert")[-1][1])

    def test_a_restart_with_only_the_isp_record_left_still_restores_it(self):
        # The IR lease record is gone (a wiped directory, a lost write) but the
        # ISP snapshot survived: it is restored, and kept being retried if not.
        self.lease(minutes=30)
        self.lease_path.unlink()
        self.camera.commands.clear()
        self.camera.isp_write_refuse = True

        restarted = self.build()
        restarted.night_mode.restore_on_start()
        self.assertEqual(NIGHT_ISP, self.camera.isp)
        self.assertTrue(restarted.night_vision_snapshot()["restore_failed"])
        self.assertEqual("error", self.journal.find("isp_restore")[-1][0])

        # No IR lease stands, so the IR worker's tick carries the retry.
        self.camera.isp_write_refuse = False
        self.assertFalse(restarted.controller.run_due_revert())
        self.clock.advance(RESTORE_RETRY_SECONDS[0] + 1)
        self.assertTrue(restarted.controller.run_due_revert())
        self.assertEqual(LIVE_ISP, self.camera.isp)
        self.assertFalse(self.record_path.exists())
        self.assertFalse(restarted.night_vision_snapshot()["active"])

    def test_a_restart_with_no_record_never_touches_the_isp(self):
        self.camera.isp["dayNight"] = NIGHT_DAY_NIGHT  # set by hand, not by a lease

        restarted = self.build()
        restarted.night_mode.restore_on_start()
        restarted.controller.restore_default_on_start()

        self.assertEqual([], self.camera.isp_writes)
        self.assertEqual(NIGHT_DAY_NIGHT, self.camera.isp["dayNight"])

    def test_a_record_that_cannot_be_read_is_never_written_back_and_is_said_loudly(self):
        self.record_path.write_text('{"isp": {"dayNight": 1}, "set_at": "no"}')
        os.chmod(self.record_path, 0o600)
        self.camera.isp["dayNight"] = NIGHT_DAY_NIGHT

        restarted = self.build()
        restarted.night_mode.restore_on_start()
        restarted.controller.restore_default_on_start()

        self.assertEqual([], self.camera.isp_writes)
        self.assertEqual("error", self.journal.find("isp_record_corrupt")[0][0])
        # Kept as evidence, reported as not restored, and no new lease is
        # taken over an original nobody can read.
        self.assertTrue(self.record_path.exists())
        self.assertEqual({"enabled": True, "active": True, "restore_failed": True,
                          "last_error": "record_corrupt"}, restarted.night_vision_snapshot())
        with self.assertRaises(CameraError):
            restarted.controller.set_state("Auto", 10)
        self.assertEqual([], self.camera.isp_writes)
        self.assertEqual(NIGHT_DAY_NIGHT, self.camera.isp["dayNight"])

    # -- failures ----------------------------------------------------------------
    def test_an_isp_that_cannot_be_read_is_never_changed(self):
        self.camera.isp_read_refuse = True

        status, body = self.lease()

        self.assertEqual(502, status)
        self.assertEqual({"error": "camera_error"}, body)
        self.assertEqual(["GetIsp"], self.camera_writes())
        self.assertEqual(LIVE_ISP, self.camera.isp)
        self.assertEqual("Off", self.camera.ir_state)
        self.assertFalse(self.record_path.exists())
        self.assertFalse(self.lease_path.exists())
        status, body = self.request("GET", "/camera/state")
        self.assertIsNone(body["ir"]["effective_until"])
        self.assertFalse(body["night_vision"]["active"])

    def test_a_firmware_without_the_night_mode_gets_no_mode_change(self):
        self.camera.isp_range = {"dayNight": ["Auto", "Color"]}

        status, body = self.lease()

        self.assertEqual(502, status)
        self.assertEqual({"error": "camera_error"}, body)
        self.assertEqual(["GetIsp"], self.camera_writes())
        self.assertEqual("Off", self.camera.ir_state)
        self.assertEqual("error", self.journal.find("isp_night")[0][0])
        self.assertIn("outcome=unsupported", self.journal.find("isp_night")[0][1])

    def test_a_firmware_that_reports_no_range_is_taken_at_its_word(self):
        self.camera.isp_range = None

        status, body = self.lease()

        self.assertEqual(200, status, body)
        self.assertEqual(NIGHT_ISP, self.camera.isp)

    def test_a_night_switch_the_camera_refuses_keeps_the_lease_so_it_still_reverts(self):
        self.camera.isp_write_refuse = True

        status, body = self.lease()

        self.assertEqual(502, status)
        self.assertEqual("Off", self.camera.ir_state)  # IR was never reached
        self.assertEqual(["GetIsp", "SetIsp"], self.camera_writes())
        # The write may have landed, so the records stay and the expiry puts
        # the picture back, then IR -- both idempotent against a camera that
        # never changed.
        self.assertTrue(self.record_path.exists())
        self.assertTrue(self.lease_path.exists())
        self.camera.isp_write_refuse = False
        self.clock.advance(601)
        self.assertTrue(self.service.controller.run_due_revert())
        self.assertEqual(LIVE_ISP, self.camera.isp)
        self.assertEqual("Off", self.camera.ir_state)
        self.assertFalse(self.record_path.exists())

    def test_a_restore_that_fails_is_retried_reported_and_leaves_ir_on_until_it_takes(self):
        self.lease(minutes=10)
        self.camera.commands.clear()
        self.camera.isp_write_refuse = True
        self.clock.advance(601)

        self.assertFalse(self.service.controller.run_due_revert())

        # The picture could not be put back, so the illuminator stays and the
        # whole revert is retried; nothing is half-reverted.
        self.assertEqual(NIGHT_ISP, self.camera.isp)
        self.assertEqual("Auto", self.camera.ir_state)
        self.assertEqual(["SetIsp"], self.camera_writes())
        self.assertTrue(self.record_path.exists())
        status, body = self.request("GET", "/camera/state")
        self.assertTrue(body["ir"]["revert_failed"])
        self.assertEqual({"enabled": True, "active": True, "restore_failed": True,
                          "last_error": "camera_error"}, body["night_vision"])
        [(level, line)] = self.journal.find("isp_restore")
        self.assertEqual("error", level)
        self.assertIn("outcome=camera_error reason=expired", line)
        self.assertIn("attempt=1", line)
        [(_, revert)] = self.journal.find("ir_revert")
        self.assertIn("outcome=camera_error", revert)

        # Still failing at the first retry; the backoff grows.
        self.clock.advance(6)
        self.assertFalse(self.service.controller.run_due_revert())
        self.assertEqual(2, len(self.journal.find("isp_restore")))
        self.assertIn("attempt=2", self.journal.find("isp_restore")[-1][1])

        # The camera answers again: the picture, then IR, then all clear.
        self.camera.isp_write_refuse = False
        self.clock.advance(11)
        self.assertTrue(self.service.controller.run_due_revert())
        self.assertEqual(LIVE_ISP, self.camera.isp)
        self.assertEqual("Off", self.camera.ir_state)
        self.assertEqual(["SetIsp", "SetIsp", "SetIsp", "SetIrLights"], self.camera_writes())
        self.assertFalse(self.record_path.exists())
        self.assertIn("outcome=completed reason=retry", self.journal.find("isp_restore")[-1][1])
        status, body = self.request("GET", "/camera/state")
        self.assertFalse(body["ir"]["revert_failed"])
        self.assertEqual({"enabled": True, "active": False, "restore_failed": False,
                          "last_error": None}, body["night_vision"])

    def test_an_off_whose_restore_fails_is_answered_honestly_and_retried_before_expiry(self):
        self.lease(minutes=30)
        self.camera.commands.clear()
        self.camera.isp_write_refuse = True

        status, body = self.request("POST", "/camera/ir", {"state": "Off"})

        # The camera refused the picture; IR was not touched and the lease stands.
        self.assertEqual(502, status)
        self.assertEqual({"error": "camera_error"}, body)
        self.assertEqual(["SetIsp"], self.camera_writes())
        self.assertEqual("Auto", self.camera.ir_state)
        self.assertTrue(self.lease_path.exists())
        status, body = self.request("GET", "/camera/state")
        self.assertIsNotNone(body["ir"]["effective_until"])
        self.assertTrue(body["night_vision"]["restore_failed"])
        self.assertIn("reason=requested", self.journal.find("isp_restore")[0][1])

        # The operator asked for Off, so the worker retries on the restore's
        # backoff rather than waiting the remaining 30 minutes.
        self.camera.isp_write_refuse = False
        self.assertFalse(self.service.controller.run_due_revert())
        self.clock.advance(RESTORE_RETRY_SECONDS[0] + 1)
        self.assertTrue(self.service.controller.run_due_revert())
        self.assertEqual(LIVE_ISP, self.camera.isp)
        self.assertEqual("Off", self.camera.ir_state)
        self.assertFalse(self.lease_path.exists())
        self.assertFalse(self.record_path.exists())
        self.assertIn("reason=retry", self.journal.find("ir_revert")[-1][1])

    # -- the switch itself -------------------------------------------------------
    def test_with_the_isp_switch_off_the_lease_is_ir_only_as_before(self):
        self.server.service = self.build(GATE_CAMERA_NIGHT_VISION_ISP="false")

        status, body = self.lease()

        self.assertEqual(200, status, body)
        self.assertEqual("Auto", self.camera.ir_state)
        self.assertEqual(["SetIrLights"], self.camera_writes())
        self.assertEqual(LIVE_ISP, self.camera.isp)
        self.assertEqual({"enabled": False, "active": False, "restore_failed": False,
                          "last_error": None}, body["night_vision"])
        self.assertNotIn("night_mode", self.journal.find("ir_set")[0][1])

    def test_a_lease_that_turns_ir_off_under_an_auto_default_changes_no_picture(self):
        self.camera.ir_state = "Auto"
        self.server.service = self.build(GATE_CAMERA_IR_DEFAULT="Auto")

        status, body = self.request("POST", "/camera/ir", {"state": "Off", "lease_minutes": 5})

        self.assertEqual(200, status, body)
        self.assertEqual("Off", self.camera.ir_state)
        self.assertEqual(["SetIrLights"], self.camera_writes())
        self.assertFalse(body["night_vision"]["active"])

    def test_the_spotlight_lease_never_touches_the_isp(self):
        status, body = self.request("POST", "/camera/spotlight", {"state": "On", "lease_minutes": 5})

        self.assertEqual(200, status, body)
        self.assertNotIn("GetIsp", self.camera.commands)
        self.assertNotIn("SetIsp", self.camera.commands)
        self.assertFalse(body["night_vision"]["active"])

    def test_the_heartbeat_document_carries_the_lease_and_whether_the_picture_is_switched(self):
        from gate_camera_control.state import state_document
        from gate_controller.camera_control_state import validated_camera_control

        self.lease(minutes=10)
        document = state_document(
            self.service.controller.snapshot(), now=int(self.clock.now),
            night_snapshot=self.service.night_vision_snapshot(),
        )
        parsed = validated_camera_control(document["camera_control"])

        self.assertEqual("Auto", parsed["ir"]["state"])
        self.assertIsNotNone(parsed["ir"]["effective_until"])
        self.assertEqual({"enabled": True, "active": True, "restore_failed": False},
                         parsed["night_vision"])
        text = json.dumps(document)
        self.assertNotIn("192.168", text)
        self.assertNotIn("s3cret", text)
        # No ISP value reaches the heartbeat: the picture settings stay on the Pi.
        self.assertNotIn("shutter", text)
        self.assertNotIn("Black", text)


if __name__ == "__main__":
    unittest.main()
