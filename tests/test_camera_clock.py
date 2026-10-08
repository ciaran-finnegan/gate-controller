import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from gate_camera_control.clock import ClockReconciler, ClockWorker, pi_clock_is_synced
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
    """A camera whose firmware displays what it is sent, unless told otherwise.

    ``dst_shift_hours`` reproduces the RLC-811A: when a ``SetTime`` changes the
    DST flag, the display lands that many hours from the time it was sent.
    ``ntp`` is the camera's NTP block; ``None`` means the camera will not say.
    """

    def __init__(self, state, *, error=None, dst_shift_hours=0, ntp=None, ntp_error=None):
        self.state = state
        self.error = error
        self.dst_shift_hours = dst_shift_hours
        self.ntp = ntp
        self.ntp_error = ntp_error
        self.writes = []
        self.ntp_writes = []

    def clock_state(self):
        if self.error is not None:
            raise self.error
        return {"Time": dict(self.state["Time"]), "Dst": dict(self.state["Dst"])}

    def set_clock(self, fields, dst):
        self.writes.append((dict(fields), dict(dst)))
        displayed = dict(fields, isDst=0)
        if int(dst["enable"]) != int(self.state["Dst"]["enable"]) and self.dst_shift_hours:
            sent = datetime(fields["year"], fields["mon"], fields["day"],
                            fields["hour"], fields["min"], fields["sec"])
            landed = sent + timedelta(hours=self.dst_shift_hours)
            displayed.update(year=landed.year, mon=landed.month, day=landed.day,
                             hour=landed.hour, min=landed.minute, sec=landed.second)
        self.state = {"Time": displayed, "Dst": dict(dst)}

    def ntp_state(self):
        if self.ntp_error is not None:
            raise self.ntp_error
        return dict(self.ntp)

    def set_ntp(self, ntp):
        self.ntp_writes.append(dict(ntp))
        self.ntp = dict(ntp)


NTP_ON = {"enable": 1, "server": "pool.ntp.org", "port": 123, "interval": 60}
NTP_OFF = {"enable": 0, "server": "pool.ntp.org", "port": 123, "interval": 60}


class ClockReconcilerTests(unittest.TestCase):
    NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)

    def _reconciler(self, client, *, pi_synced=True, **kwargs):
        journal = []
        reconciler = ClockReconciler(
            client, clock=lambda: 1000.0, utc_now=lambda: self.NOW,
            pi_clock_synced=lambda: pi_synced,
            journal=lambda stage, **fields: journal.append((stage, fields)), **kwargs,
        )
        return reconciler, journal

    def test_a_clock_within_tolerance_is_left_alone(self):
        client = FakeClient(camera_time(self.NOW.replace(second=3)), ntp=NTP_ON)
        reconciler, journal = self._reconciler(client)
        snapshot = reconciler.reconcile()
        self.assertEqual(client.writes, [])
        self.assertEqual(client.ntp_writes, [])
        self.assertEqual((snapshot["outcome"], snapshot["synced"], snapshot["skew_seconds"]), ("ok", True, 3))
        self.assertEqual(journal[-1], ("clock_reconcile", {
            "outcome": "ok", "skew_seconds": "+3", "time_zone": 0, "dst": 0, "ntp": "on",
        }))
        self.assertEqual(reconciler._next_at, 1000.0 + 3600.0, "a clock found right waits the full hour")

    def test_a_clock_two_hours_ahead_is_written_back_to_utc(self):
        client = FakeClient(camera_time(self.NOW.replace(hour=14)), ntp=NTP_ON)
        reconciler, journal = self._reconciler(client)
        snapshot = reconciler.reconcile()
        self.assertEqual(len(client.writes), 1)
        fields, dst = client.writes[0]
        self.assertEqual((fields["hour"], fields["min"], fields["sec"], fields["timeZone"]), (12, 0, 0, 0))
        self.assertNotIn("isDst", fields, "a read-only field is never written back")
        self.assertEqual(dst["enable"], 0)
        self.assertEqual((snapshot["outcome"], snapshot["skew_seconds"], snapshot["corrections"]), ("corrected", 0, 1))
        self.assertEqual(journal[-1][1]["outcome"], "corrected")

    def test_a_correction_is_looked_at_again_soon_not_in_an_hour(self):
        """A writer that keeps moving the clock is caught within minutes."""
        client = FakeClient(camera_time(self.NOW.replace(hour=14)), ntp=NTP_ON)
        reconciler, _journal = self._reconciler(client, interval_seconds=3600, retry_seconds=300)
        reconciler.reconcile()
        self.assertEqual(reconciler._next_at, 1300.0)

    def test_a_zone_somebody_chose_is_corrected_too(self):
        """The camera stamps every alarm +0000 whatever it displays.

        A phone app's "sync with phone time" from another time zone writes
        that zone into the camera. Reporting it as the operator's choice and
        leaving it (what `skipped_config` did) left every webhook `stale` for
        good: there is no zone this system can honour but UTC.
        """
        client = FakeClient(camera_time(self.NOW.replace(hour=20), time_zone=-28800), ntp=NTP_ON)
        reconciler, journal = self._reconciler(client)

        snapshot = reconciler.reconcile()

        self.assertEqual(len(client.writes), 1)
        fields, dst = client.writes[0]
        self.assertEqual((fields["timeZone"], fields["hour"], dst["enable"]), (0, 12, 0))
        self.assertEqual((snapshot["outcome"], snapshot["synced"], snapshot["skew_seconds"]), ("corrected", True, 0))
        self.assertEqual((journal[-1][1]["time_zone"], journal[-1][1]["dst"]), (-28800, 0),
                         "the journal keeps the writer's signature")

    def test_a_zone_that_happens_to_read_right_is_still_put_back_to_utc(self):
        client = FakeClient(camera_time(self.NOW, time_zone=-3600, dst_enabled=1), ntp=NTP_ON)
        reconciler, _journal = self._reconciler(client)
        snapshot = reconciler.reconcile()
        self.assertEqual(len(client.writes), 1)
        self.assertEqual(snapshot["outcome"], "corrected")
        self.assertEqual((client.state["Time"]["timeZone"], client.state["Dst"]["enable"]), (0, 0))

    def test_utc_with_the_dst_flag_flipped_back_on_is_corrected(self):
        """Not a choice anybody makes.

        This camera keeps being put back into `timeZone 0` with DST on, and the
        firmware then counts the offset twice and lands two hours out -- which
        is what rejected every webhook as stale in September.
        """
        client = FakeClient(camera_time(self.NOW.replace(hour=14), dst_enabled=1, is_dst=1), ntp=NTP_ON)
        reconciler, _journal = self._reconciler(client)

        snapshot = reconciler.reconcile()

        self.assertEqual(len(client.writes), 1)
        fields, dst = client.writes[0]
        self.assertEqual(fields["timeZone"], 0)
        self.assertEqual(dst["enable"], 0, "the double-count is turned off with the same write")
        self.assertEqual(snapshot["outcome"], "corrected")

    def test_a_firmware_that_shifts_the_display_when_dst_changes_gets_a_second_write(self):
        """SetTime carries the displayed time and the firmware moves it by the
        DST hour when the flag changes in the same call. The first write turns
        DST off and lands an hour out; the second changes nothing but the time,
        so the camera displays exactly what it is sent."""
        client = FakeClient(
            camera_time(self.NOW.replace(hour=14), dst_enabled=1, is_dst=1),
            dst_shift_hours=-1, ntp=NTP_ON,
        )
        reconciler, journal = self._reconciler(client)

        snapshot = reconciler.reconcile()

        self.assertEqual(len(client.writes), 2)
        first, second = client.writes
        self.assertEqual((first[1]["enable"], second[1]["enable"]), (0, 0))
        self.assertEqual((second[0]["hour"], second[0]["timeZone"]), (12, 0))
        self.assertEqual(client.state["Time"]["hour"], 12)
        self.assertEqual((snapshot["outcome"], snapshot["skew_seconds"], snapshot["corrections"]), ("corrected", 0, 1))
        self.assertEqual(journal[-1][1]["outcome"], "corrected")

    def test_a_correction_that_will_not_hold_is_reported_not_claimed(self):
        class StubbornClient(FakeClient):
            def set_clock(self, fields, dst):
                self.writes.append((dict(fields), dict(dst)))
                # Whatever is written, the camera displays two hours ahead.
                self.state = {"Time": dict(fields, hour=14, isDst=0), "Dst": dict(dst)}

        client = StubbornClient(camera_time(self.NOW.replace(hour=14)), ntp=NTP_ON)
        reconciler, journal = self._reconciler(client, retry_seconds=300)

        snapshot = reconciler.reconcile()

        self.assertEqual(len(client.writes), 2, "one write to fix the configuration, one more for the time")
        self.assertEqual((snapshot["outcome"], snapshot["synced"], snapshot["skew_seconds"], snapshot["corrections"]),
                         ("correction_failed", False, 7200, 0))
        self.assertEqual(journal[-1][1]["outcome"], "correction_failed")
        self.assertEqual(reconciler._next_at, 1300.0)

    def test_nothing_is_written_while_the_pi_clock_is_unsynced(self):
        """The Pi has no RTC. After a power cut this service is up before
        timesyncd has stepped the clock, and the first pass used to write the
        Pi's boot-time clock into a camera that was right."""
        client = FakeClient(camera_time(self.NOW.replace(hour=14), dst_enabled=1), ntp=NTP_OFF)
        reconciler, journal = self._reconciler(client, pi_synced=False, retry_seconds=300)

        snapshot = reconciler.reconcile()

        self.assertEqual(client.writes, [])
        self.assertEqual(client.ntp_writes, [], "no camera write of any kind until the Pi is trusted")
        self.assertEqual((snapshot["outcome"], snapshot["synced"], snapshot["skew_seconds"]), ("pi_unsynced", False, 7200))
        self.assertEqual(journal[-1][1]["outcome"], "pi_unsynced")
        self.assertEqual(reconciler._next_at, 1300.0, "asked again soon, not in an hour")

    def test_a_right_clock_is_still_reported_ok_while_the_pi_is_unsynced(self):
        """`pi_unsynced` is about writing; a camera that agrees with the Pi is
        still reported as agreeing."""
        client = FakeClient(camera_time(self.NOW), ntp=NTP_ON)
        reconciler, _journal = self._reconciler(client, pi_synced=False)
        self.assertEqual(reconciler.reconcile()["outcome"], "ok")

    def test_ntp_switched_off_by_the_writer_is_turned_back_on(self):
        for state, name in ((camera_time(self.NOW), "ok"), (camera_time(self.NOW.replace(hour=14)), "corrected")):
            with self.subTest(outcome=name):
                client = FakeClient(state, ntp={"enable": 0, "server": "time.example.net", "port": 123, "interval": 1440})
                reconciler, journal = self._reconciler(client)
                snapshot = reconciler.reconcile()
                self.assertEqual(snapshot["outcome"], name)
                self.assertEqual(client.ntp_writes, [
                    {"enable": 1, "server": "time.example.net", "port": 123, "interval": 1440},
                ], "the operator's server is kept; only the switch moves")
                self.assertEqual(journal[-1][1]["ntp"], "restored")

    def test_ntp_with_no_server_is_given_the_commissioning_defaults(self):
        client = FakeClient(camera_time(self.NOW), ntp={"enable": 0, "server": "", "port": 0, "interval": 0})
        reconciler, _journal = self._reconciler(client)
        reconciler.reconcile()
        self.assertEqual(client.ntp_writes, [{"enable": 1, "server": "pool.ntp.org", "port": 123, "interval": 60}])

    def test_a_camera_that_will_not_answer_ntp_still_has_its_clock_set(self):
        client = FakeClient(camera_time(self.NOW.replace(hour=14)), ntp_error=CameraError("no ntp"))
        reconciler, journal = self._reconciler(client)
        snapshot = reconciler.reconcile()
        self.assertEqual(snapshot["outcome"], "corrected")
        self.assertEqual(journal[-1][1]["ntp"], "unknown")

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
        client = FakeClient(camera_time(self.NOW), ntp=NTP_ON)
        clock = {"now": 1000.0}
        reconciler = ClockReconciler(
            client, clock=lambda: clock["now"], utc_now=lambda: self.NOW,
            pi_clock_synced=lambda: True, interval_seconds=3600,
        )
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
        client = FakeClient(camera_time(self.NOW.replace(hour=14)), ntp=NTP_ON)
        reconciler, _journal = self._reconciler(client)
        reconciler.reconcile()
        document = state_document({"state": "Off", "default": "Off"}, clock_snapshot=reconciler.snapshot())
        clock = document["camera_control"]["clock"]
        self.assertEqual(set(clock), {"synced", "outcome", "skew_seconds", "checked_at", "corrections"})
        self.assertEqual((clock["outcome"], clock["synced"], clock["corrections"]), ("corrected", True, 1))
        self.assertNotIn("clock", state_document({"state": "Off", "default": "Off"})["camera_control"])
        garbage = state_document({"state": "Off", "default": "Off"}, clock_snapshot={"outcome": "???", "skew_seconds": "x", "checked_at": 5, "corrections": -1})
        self.assertEqual(garbage["camera_control"]["clock"], {"synced": False, "outcome": "camera_error", "skew_seconds": None, "checked_at": None, "corrections": 0})
        for outcome in ("pi_unsynced", "correction_failed"):
            with self.subTest(outcome=outcome):
                waiting = state_document({"state": "Off", "default": "Off"}, clock_snapshot={
                    "outcome": outcome, "skew_seconds": 7200, "checked_at": "2026-09-16T12:00:00+00:00", "corrections": 0,
                })["camera_control"]["clock"]
                self.assertEqual((waiting["outcome"], waiting["synced"]), (outcome, False))


class PiClockIsSyncedTests(unittest.TestCase):
    def test_the_timesyncd_stamp_is_the_definite_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            stamp = os.path.join(directory, "synchronized")
            open(stamp, "w").close()
            self.assertTrue(pi_clock_is_synced(stamp_path=stamp, adjtimex=lambda: 5))

    def test_without_the_stamp_the_kernel_clock_state_decides(self):
        with tempfile.TemporaryDirectory() as directory:
            stamp = os.path.join(directory, "synchronized")
            self.assertTrue(pi_clock_is_synced(stamp_path=stamp, adjtimex=lambda: 0))
            self.assertTrue(pi_clock_is_synced(stamp_path=stamp, adjtimex=lambda: 1), "a leap second pending is still synced")
            self.assertFalse(pi_clock_is_synced(stamp_path=stamp, adjtimex=lambda: 5), "TIME_ERROR is an undisciplined clock")

    def test_when_nothing_can_answer_the_clock_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as directory:
            stamp = os.path.join(directory, "synchronized")
            self.assertFalse(pi_clock_is_synced(stamp_path=stamp, adjtimex=lambda: None))

    def test_the_default_probe_runs_without_raising(self):
        # Whether this host is synced is the host's business; the probe must
        # only ever answer, never take the service down.
        self.assertIn(pi_clock_is_synced(), (True, False))


if __name__ == "__main__":
    unittest.main()
