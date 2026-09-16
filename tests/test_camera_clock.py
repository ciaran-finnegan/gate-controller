import unittest
from datetime import datetime, timezone

from gate_camera_control.clock import ClockReconciler, ClockWorker
from gate_camera_control.reolink import CameraBusy, CameraError, CameraUnreachable
from gate_camera_control.state import state_document


def camera_time(dt, *, time_zone=0, dst_enabled=0, is_dst=0):
    return {
        "Time": {
            "year": dt.year, "mon": dt.month, "day": dt.day, "hour": dt.hour,
            "min": dt.minute, "sec": dt.second, "hourFmt": 1, "timeFmt": "DD/MM/YYYY",
            "timeZone": time_zone, "isDst": is_dst,
        },
        "Dst": {"enable": dst_enabled, "offset": 1, "startMon": 3, "endMon": 10},
    }


class FakeClient:
    def __init__(self, state, *, error=None):
        self.state = state
        self.error = error
        self.writes = []

    def clock_state(self):
        if self.error is not None:
            raise self.error
        return {"Time": dict(self.state["Time"]), "Dst": dict(self.state["Dst"])}

    def set_clock(self, fields, dst):
        self.writes.append((dict(fields), dict(dst)))
        # The firmware displays exactly what it was sent when DST does not change.
        self.state = {"Time": dict(fields, isDst=0), "Dst": dict(dst)}


class ClockReconcilerTests(unittest.TestCase):
    NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)

    def _reconciler(self, client, **kwargs):
        journal = []
        reconciler = ClockReconciler(
            client, clock=lambda: 1000.0, utc_now=lambda: self.NOW,
            journal=lambda stage, **fields: journal.append((stage, fields)), **kwargs,
        )
        return reconciler, journal

    def test_a_clock_within_tolerance_is_left_alone(self):
        client = FakeClient(camera_time(self.NOW.replace(second=3)))
        reconciler, journal = self._reconciler(client)
        snapshot = reconciler.reconcile()
        self.assertEqual(client.writes, [])
        self.assertEqual((snapshot["outcome"], snapshot["synced"], snapshot["skew_seconds"]), ("ok", True, 3))
        self.assertEqual(journal[-1], ("clock_reconcile", {"outcome": "ok", "skew_seconds": "+3"}))

    def test_a_clock_two_hours_ahead_is_written_back_to_utc(self):
        client = FakeClient(camera_time(self.NOW.replace(hour=14)))
        reconciler, journal = self._reconciler(client)
        snapshot = reconciler.reconcile()
        self.assertEqual(len(client.writes), 1)
        fields, dst = client.writes[0]
        self.assertEqual((fields["hour"], fields["min"], fields["sec"], fields["timeZone"]), (12, 0, 0, 0))
        self.assertNotIn("isDst", fields, "a read-only field is never written back")
        self.assertEqual(dst["enable"], 0)
        self.assertEqual((snapshot["outcome"], snapshot["skew_seconds"], snapshot["corrections"]), ("corrected", 0, 1))
        self.assertEqual(journal[-1][1]["outcome"], "corrected")

    def test_a_camera_not_configured_to_display_utc_is_reported_but_never_written(self):
        for state in (
            camera_time(self.NOW.replace(hour=14), dst_enabled=1, is_dst=1),
            camera_time(self.NOW.replace(hour=14), time_zone=-3600),
        ):
            with self.subTest(state=state["Time"]):
                client = FakeClient(state)
                reconciler, _journal = self._reconciler(client)
                snapshot = reconciler.reconcile()
                self.assertEqual(client.writes, [])
                self.assertEqual((snapshot["outcome"], snapshot["synced"], snapshot["skew_seconds"]), ("skipped_config", False, 7200))

    def test_camera_errors_are_recorded_and_retried_sooner_than_the_hourly_pass(self):
        for error, outcome in (
            (CameraBusy(30), "camera_busy"),
            (CameraUnreachable("down"), "camera_unreachable"),
            (CameraError("odd"), "camera_error"),
        ):
            with self.subTest(outcome=outcome):
                client = FakeClient(camera_time(self.NOW), error=error)
                reconciler, _journal = self._reconciler(client, interval_seconds=3600, retry_seconds=300)
                snapshot = reconciler.reconcile()
                self.assertEqual((snapshot["outcome"], snapshot["synced"], snapshot["skew_seconds"]), (outcome, False, None))
                self.assertEqual(reconciler._next_at, 1300.0)

    def test_run_due_follows_the_schedule(self):
        client = FakeClient(camera_time(self.NOW))
        clock = {"now": 1000.0}
        reconciler = ClockReconciler(client, clock=lambda: clock["now"], utc_now=lambda: self.NOW, interval_seconds=3600)
        self.assertTrue(reconciler.run_due(), "the first pass runs immediately")
        self.assertFalse(reconciler.run_due())
        clock["now"] += 3600
        self.assertTrue(reconciler.run_due())

    def test_disabled_never_touches_the_camera(self):
        client = FakeClient(camera_time(self.NOW.replace(hour=14)), error=CameraError("must not be called"))
        reconciler = ClockReconciler(client, enabled=False)
        self.assertFalse(reconciler.run_due())
        self.assertEqual(reconciler.snapshot()["outcome"], "disabled")
        worker = ClockWorker(reconciler)
        worker.start()
        worker.stop()

    def test_the_published_document_carries_the_bounded_clock_block(self):
        client = FakeClient(camera_time(self.NOW.replace(hour=14)))
        reconciler, _journal = self._reconciler(client)
        reconciler.reconcile()
        document = state_document({"state": "Off", "default": "Off"}, clock_snapshot=reconciler.snapshot())
        clock = document["camera_control"]["clock"]
        self.assertEqual(set(clock), {"synced", "outcome", "skew_seconds", "checked_at", "corrections"})
        self.assertEqual((clock["outcome"], clock["synced"], clock["corrections"]), ("corrected", True, 1))
        self.assertNotIn("clock", state_document({"state": "Off", "default": "Off"})["camera_control"])
        garbage = state_document({"state": "Off", "default": "Off"}, clock_snapshot={"outcome": "???", "skew_seconds": "x", "checked_at": 5, "corrections": -1})
        self.assertEqual(garbage["camera_control"]["clock"], {"synced": False, "outcome": "camera_error", "skew_seconds": None, "checked_at": None, "corrections": 0})


if __name__ == "__main__":
    unittest.main()
