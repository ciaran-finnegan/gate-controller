import tempfile
import unittest
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from gate_controller.authorisation import (
    AuthorisationError, AuthorisationRefreshWorker, AuthorisedPlateCache,
    CloudflarePlateFetcher,
)


class FakeClient:
    def __init__(self, *, json_response=None):
        self.json_response = json_response
        self.requests = []

    def get_json(self, path):
        self.requests.append(type("Request", (), {"path": path})())
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
