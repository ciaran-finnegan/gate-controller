import tempfile
import unittest
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from gate_controller.authorisation import (
    AuthorisationError, AuthorisationRefreshWorker, AuthorisedPlateCache,
    CloudflarePlateFetcher, MAX_NORMALISED_PLATE_LENGTH, MAX_PLATE_ROWS,
    MAX_PLATE_SNAPSHOT_BYTES,
)


class FakeClient:
    def __init__(self, *, json_response=None):
        self.json_response = json_response
        self.requests = []

    def get_json(self, path, *, max_response_bytes=None):
        self.requests.append(type("Request", (), {
            "path": path, "max_response_bytes": max_response_bytes,
        })())
        return self.json_response


class AuthorisedPlateCacheTests(unittest.TestCase):
    def test_cloudflare_plate_fetcher_reads_worker_snapshot_with_controller_id(self):
        client = FakeClient(json_response={
            "plates": [{"plate": "241D123"}], "controller_id": "primary",
        })

        rows = CloudflarePlateFetcher(client, "primary")()

        self.assertEqual(rows, [{"plate": "241D123"}])
        self.assertEqual(
            client.requests[0].path, "/api/controller/plates?controller_id=primary"
        )
        self.assertEqual(client.requests[0].max_response_bytes, MAX_PLATE_SNAPSHOT_BYTES)

    def test_cloudflare_plate_fetcher_rejects_a_snapshot_for_another_controller(self):
        client = FakeClient(json_response={
            "plates": [{"plate": "241D123"}], "controller_id": "secondary",
        })

        with self.assertRaisesRegex(AuthorisationError, "controller"):
            CloudflarePlateFetcher(client, "primary")()

    def test_cloudflare_plate_fetcher_rejects_an_unbound_plate_list(self):
        client = FakeClient(json_response=[{"plate": "241D123"}])

        with self.assertRaisesRegex(AuthorisationError, "controller"):
            CloudflarePlateFetcher(client, "primary")()

    def test_oversized_cloudflare_plate_row_set_does_not_replace_snapshot(self):
        client = FakeClient(json_response={
            "plates": [{"plate": f"{index}"} for index in range(MAX_PLATE_ROWS + 1)],
            "controller_id": "primary",
        })
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plates.csv"
            path.write_text("plate\n12D3456\n", encoding="utf-8")
            cache = AuthorisedPlateCache(path)
            worker = AuthorisationRefreshWorker(
                cache, CloudflarePlateFetcher(client, "primary")
            )

            self.assertFalse(worker.run_once())
            self.assertEqual(cache.get(), ("12D3456",))

    def test_overlong_normalised_cloudflare_plate_does_not_replace_snapshot(self):
        client = FakeClient(json_response={
            "plates": [{"plate": "A" * (MAX_NORMALISED_PLATE_LENGTH + 1)}],
            "controller_id": "primary",
        })
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plates.csv"
            path.write_text("plate\n12D3456\n", encoding="utf-8")
            cache = AuthorisedPlateCache(path)
            worker = AuthorisationRefreshWorker(
                cache, CloudflarePlateFetcher(client, "primary")
            )

            self.assertFalse(worker.run_once())
            self.assertEqual(cache.get(), ("12D3456",))

    def test_atomic_snapshot_replace_fsyncs_the_containing_directory(self):
        real_fsync = os.fsync
        fsynced_directory = []

        def record_fsync(descriptor):
            fsynced_directory.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
            real_fsync(descriptor)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plates.csv"
            path.write_text("plate\n12D3456\n", encoding="utf-8")
            cache = AuthorisedPlateCache(path)

            with patch("gate_controller.authorisation.os.fsync", side_effect=record_fsync):
                cache.replace(("12E3456",))

        self.assertEqual(fsynced_directory, [False, True])

    def test_keeps_last_known_good_plates_until_a_complete_refresh_arrives(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plates.csv"
            path.write_text("plate,name\n12D3456,Ada\n", encoding="utf-8")
            cache = AuthorisedPlateCache(path)

            self.assertEqual(cache.get(), ("12D3456",))
            path.write_text("plate,name\n", encoding="utf-8")
            cache.reload_local()
            self.assertEqual(cache.get(), ())
            path.write_text("plate,name\n12E3456,Bea\n", encoding="utf-8")
            cache.reload_local()
            self.assertEqual(cache.get(), ("12E3456",))

    def test_background_refresh_applies_additions_and_revocations_atomically(self):
        responses = iter([
            [{"plate": "12D3456"}],
            [{"plate": "12E3456"}],
        ])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plates.csv"
            path.write_text("plate,name\n12A3456,Local\n", encoding="utf-8")
            cache = AuthorisedPlateCache(path)
            worker = AuthorisationRefreshWorker(cache, fetch=lambda: next(responses))

            self.assertTrue(worker.run_once())
            self.assertEqual(cache.get(), ("12D3456",))
            self.assertTrue(worker.run_once())
            self.assertEqual(cache.get(), ("12E3456",))

    def test_network_failure_keeps_recent_snapshot_but_expiry_fails_closed(self):
        now = [datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plates.csv"
            path.write_text("plate,name\n12D3456,Ada\n", encoding="utf-8")
            os.utime(path, (now[0].timestamp(), now[0].timestamp()))
            cache = AuthorisedPlateCache(
                path, max_staleness=timedelta(minutes=5), clock=lambda: now[0]
            )
            worker = AuthorisationRefreshWorker(
                cache, fetch=lambda: (_ for _ in ()).throw(TimeoutError("offline"))
            )

            self.assertFalse(worker.run_once())
            self.assertEqual(cache.get(), ("12D3456",))
            now[0] += timedelta(minutes=6)
            with self.assertRaisesRegex(Exception, "stale"):
                cache.get()

    def test_old_local_snapshot_is_stale_immediately_after_restart(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plates.csv"
            path.write_text("plate,name\n12D3456,Ada\n", encoding="utf-8")
            old = (now - timedelta(hours=1)).timestamp()
            os.utime(path, (old, old))

            cache = AuthorisedPlateCache(
                path, max_staleness=timedelta(minutes=5), clock=lambda: now
            )

            with self.assertRaisesRegex(Exception, "stale"):
                cache.get()

    def test_future_local_snapshot_fails_closed_after_wall_clock_rollback(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plates.csv"
            path.write_text("plate,name\n12D3456,Ada\n", encoding="utf-8")
            future = (now + timedelta(hours=1)).timestamp()
            os.utime(path, (future, future))

            cache = AuthorisedPlateCache(
                path, max_staleness=timedelta(minutes=5), clock=lambda: now
            )

            with self.assertRaisesRegex(Exception, "stale"):
                cache.get()
            self.assertTrue(cache.status()["stale"])

    def test_refresh_failure_is_visible_in_snapshot_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plates.csv"
            path.write_text("plate,name\n12D3456,Ada\n", encoding="utf-8")
            cache = AuthorisedPlateCache(path)
            worker = AuthorisationRefreshWorker(
                cache, fetch=lambda: (_ for _ in ()).throw(TimeoutError("offline"))
            )

            worker.run_once()
            status = cache.status()

            self.assertTrue(status["available"])
            self.assertIn("offline", status["last_error"])
            self.assertIsNotNone(status["refreshed_at"])

    def test_get_uses_snapshot_without_network_io(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plates.csv"
            path.write_text("plate,name\n12D3456,Ada\n", encoding="utf-8")
            cache = AuthorisedPlateCache(path)
            fetch_calls = []
            worker = AuthorisationRefreshWorker(cache, fetch=lambda: fetch_calls.append(1) or [])

            self.assertEqual(cache.get(), ("12D3456",))
            self.assertEqual(cache.get(), ("12D3456",))
            self.assertEqual(fetch_calls, [])
            self.assertTrue(worker.run_once())
            self.assertEqual(fetch_calls, [1])

    def test_get_does_not_read_the_csv_on_the_recognition_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plates.csv"
            path.write_text("plate,name\n12D3456,Ada\n", encoding="utf-8")
            cache = AuthorisedPlateCache(path)

            with patch.object(cache, "_read_complete_file", side_effect=OSError("slow disk")):
                self.assertEqual(cache.get(), ("12D3456",))

    def test_refreshes_before_each_burst_even_when_metadata_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plates.csv"
            path.write_text("plate,name\n12D3456,Ada\n", encoding="utf-8")
            cache = AuthorisedPlateCache(path)

            with patch.object(cache, "_file_version", return_value=(1, 1, 1)):
                self.assertEqual(cache.get(), ("12D3456",))
                path.write_text("plate,name\n12E3456,Bea\n", encoding="utf-8")
                cache.reload_local()
                self.assertEqual(cache.get(), ("12E3456",))
            path.write_text("not-a-csv-header\ntruncated", encoding="utf-8")
            cache.reload_local()
            self.assertEqual(cache.get(), ("12E3456",))
            path.write_text("plate,name\ntruncated", encoding="utf-8")
            cache.reload_local()
            self.assertEqual(cache.get(), ("12E3456",))

    def test_refresh_worker_logs_failure_once_and_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plates.csv"
            path.write_text("plate,name\n12D3456,Ada\n", encoding="utf-8")
            cache = AuthorisedPlateCache(path)
            state = {"fail": True}

            def fetch():
                if state["fail"]:
                    raise TimeoutError("offline")
                return [{"plate": "12D3456"}]

            worker = AuthorisationRefreshWorker(cache, fetch=fetch)

            with self.assertLogs("gate_controller.authorisation", level="WARNING") as logs:
                self.assertFalse(worker.run_once())
                self.assertFalse(worker.run_once())
            self.assertEqual(len(logs.output), 1)
            self.assertIn("stage=plates_refresh_failed error_type=TimeoutError", logs.output[0])

            state["fail"] = False
            with self.assertLogs("gate_controller.authorisation", level="INFO") as logs:
                self.assertTrue(worker.run_once())
            self.assertIn("stage=plates_refresh_recovered failures=2", logs.output[0])


class AnUnchangedSnapshotIsNotRewritten(unittest.TestCase):
    """The refresh runs every 30 s; the plate list changes a few times a year.

    Rewriting an identical CSV 2,880 times a day wore the SD card for nothing
    and moved the file's mtime every 30 s. What must survive the change is the
    staleness rule: it is measured from the last successful *refresh*, not from
    the last write, and after a restart it is read back off the file's mtime.
    """

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "plates.csv"
        self.path.write_text("plate\n12D3456\n", encoding="utf-8")
        self.now = [datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)]
        os.utime(self.path, (self.now[0].timestamp(), self.now[0].timestamp()))

    def cache(self, **kwargs):
        return AuthorisedPlateCache(self.path, clock=lambda: self.now[0], **kwargs)

    def writes(self):
        """Every atomic rename onto the CSV -- the write this fix is about."""
        renames = []
        real = os.replace
        return patch(
            "gate_controller.authorisation.os.replace",
            side_effect=lambda src, dst: (renames.append(dst), real(src, dst))[1],
        ), renames

    def test_a_refresh_that_changes_nothing_does_not_touch_the_card(self):
        cache = self.cache()
        worker = AuthorisationRefreshWorker(cache, fetch=lambda: [{"plate": "12D3456"}])
        before = self.path.stat()
        spy, renames = self.writes()

        with spy:
            self.now[0] += timedelta(seconds=30)
            self.assertTrue(worker.run_once())
            self.now[0] += timedelta(seconds=30)
            self.assertTrue(worker.run_once())

        self.assertEqual(renames, [], "an identical snapshot was written back to the card")
        self.assertEqual(self.path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "plate\n12D3456\n")
        self.assertEqual(cache.get(), ("12D3456",))

    def test_a_snapshot_that_did_change_is_still_written(self):
        cache = self.cache()
        worker = AuthorisationRefreshWorker(cache, fetch=lambda: [{"plate": "12E3456"}])
        spy, renames = self.writes()

        with spy:
            self.assertTrue(worker.run_once())

        self.assertEqual(renames, [self.path])
        self.assertEqual(cache.get(), ("12E3456",))
        self.assertIn("12E3456", self.path.read_text(encoding="utf-8"))

    def test_a_csv_rewritten_underneath_us_is_written_back(self):
        """The skip is a content comparison against what we put there. If
        somebody else has replaced the file, ours goes back over it."""
        cache = self.cache()
        worker = AuthorisationRefreshWorker(cache, fetch=lambda: [{"plate": "12D3456"}])
        self.assertTrue(worker.run_once())
        self.path.write_text("plate\n99Z9999\n", encoding="utf-8")
        spy, renames = self.writes()

        with spy:
            self.assertTrue(worker.run_once())

        self.assertEqual(renames, [self.path])
        self.assertEqual(cache.get(), ("12D3456",))

    def test_staleness_is_measured_from_the_last_refresh_not_the_last_write(self):
        cache = self.cache(max_staleness=timedelta(minutes=5))
        worker = AuthorisationRefreshWorker(cache, fetch=lambda: [{"plate": "12D3456"}])
        spy, renames = self.writes()

        with spy:
            for _ in range(40):  # twenty minutes of refreshes that change nothing
                self.now[0] += timedelta(seconds=30)
                self.assertTrue(worker.run_once())
                self.assertEqual(cache.get(), ("12D3456",))

        self.assertEqual(renames, [])
        self.assertEqual(cache.status()["refreshed_at"], self.now[0].isoformat())
        self.now[0] += timedelta(minutes=6)
        with self.assertRaisesRegex(AuthorisationError, "stale"):
            cache.get()

    def test_a_restart_reads_the_age_of_the_last_refresh_not_the_last_change(self):
        """``reload_local`` dates the snapshot by the file's mtime. Skipping the
        write must not leave that mtime at the last time the list changed, or a
        fresh snapshot would fail closed the moment the service restarted."""
        cache = self.cache(max_staleness=timedelta(minutes=5))
        worker = AuthorisationRefreshWorker(cache, fetch=lambda: [{"plate": "12D3456"}])
        spy, renames = self.writes()

        with spy:
            for _ in range(40):
                self.now[0] += timedelta(seconds=30)
                self.assertTrue(worker.run_once())

        self.assertEqual(renames, [], "the mtime is kept honest by a touch, not by a rewrite")
        restarted = self.cache(max_staleness=timedelta(minutes=5))
        self.assertEqual(restarted.get(), ("12D3456",))
        self.assertFalse(restarted.status()["stale"])

    def test_a_refresh_after_a_failure_still_recovers_when_nothing_changed(self):
        cache = self.cache()
        state = {"fail": True}

        def fetch():
            if state["fail"]:
                raise TimeoutError("offline")
            return [{"plate": "12D3456"}]

        worker = AuthorisationRefreshWorker(cache, fetch=fetch)
        with self.assertLogs("gate_controller.authorisation", level="WARNING"):
            self.assertFalse(worker.run_once())
        self.assertIn("offline", cache.status()["last_error"])

        state["fail"] = False
        spy, renames = self.writes()
        with spy, self.assertLogs("gate_controller.authorisation", level="INFO") as logs:
            self.now[0] += timedelta(seconds=30)
            self.assertTrue(worker.run_once())

        self.assertEqual(renames, [])
        self.assertIn("stage=plates_refresh_recovered failures=1", logs.output[0])
        self.assertIsNone(cache.status()["last_error"])
