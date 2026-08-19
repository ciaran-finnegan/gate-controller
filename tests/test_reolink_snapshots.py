import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from urllib.error import HTTPError

from PIL import Image

from gate_controller.reolink_snapshots import (
    MAX_REOLINK_SNAPSHOT_COUNT,
    MAX_REOLINK_SNAPSHOT_TIMEOUT_SECONDS,
    ReolinkSnapshotClient,
    ReolinkSnapshotSampler,
    RejectRedirects,
    SnapshotFailure,
    SnapshotResponse,
    load_reolink_snapshot_config,
)
from gate_controller.worker import BurstCollector, CompletedImageHandler, StartupReconciler


def jpeg_bytes(color=(20, 40, 60)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(output, format="JPEG")
    return output.getvalue()


class MutableClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class FakeClient:
    def __init__(self, snapshots=(), *, login_error=None, snapshot_error=None, on_snapshot=None):
        self.snapshots = list(snapshots)
        self.login_error = login_error
        self.snapshot_error = snapshot_error
        self.on_snapshot = on_snapshot
        self.login_calls = 0
        self.snapshot_calls = 0
        self.logout_calls = 0

    def login(self, timeout):
        self.login_calls += 1
        if self.login_error:
            raise self.login_error
        return "private-camera-token"

    def snapshot(self, token, sequence, timeout):
        self.snapshot_calls += 1
        if self.on_snapshot:
            self.on_snapshot(sequence)
        if self.snapshot_error:
            raise self.snapshot_error
        return self.snapshots.pop(0)

    def logout(self, token, timeout):
        self.logout_calls += 1


class FakeResponse:
    def __init__(self, body: bytes, *, status=200, content_type="application/json"):
        self.body = body
        self.status = status
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]


class DripResponse(FakeResponse):
    def __init__(self, body: bytes, clock: MutableClock, *, seconds_per_read: float,
                 content_type="image/jpeg"):
        super().__init__(body, content_type=content_type)
        self.clock = clock
        self.seconds_per_read = seconds_per_read
        self.offset = 0

    def read1(self, limit=-1):
        self.clock.value += self.seconds_per_read
        if self.offset >= len(self.body):
            return b""
        end = len(self.body) if limit < 0 else min(len(self.body), self.offset + limit)
        chunk = self.body[self.offset:end]
        self.offset = end
        return chunk


class ScriptedOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class ReolinkSnapshotConfigurationTests(unittest.TestCase):
    def test_complete_private_https_configuration_uses_two_additional_snapshots(self):
        config = load_reolink_snapshot_config({
            "GATE_REOLINK_SNAPSHOT_BASE_URL": "https://192.168.0.54",
            "GATE_REOLINK_SNAPSHOT_USERNAME": "camera-user",
            "GATE_REOLINK_SNAPSHOT_PASSWORD": "camera-password",
            "GATE_REOLINK_SNAPSHOT_ALLOW_SELF_SIGNED": "true",
        }, Path("/var/lib/gate-controller/uploads"), max_candidate_bytes=8 * 1024 * 1024)

        self.assertTrue(config.enabled)
        self.assertEqual(config.candidate_count, 2)
        self.assertEqual(config.timeout_seconds, 2.25)
        self.assertEqual(
            config.output_directory,
            Path("/var/lib/gate-controller/uploads/.reolink-snapshots"),
        )
        self.assertTrue(config.allow_self_signed)
        self.assertNotIn("camera-password", repr(config))

    def test_missing_all_camera_credentials_disables_augmentation_but_partial_config_fails(self):
        disabled = load_reolink_snapshot_config(
            {}, Path("/state/uploads"), max_candidate_bytes=1024
        )

        self.assertFalse(disabled.enabled)
        self.assertEqual(disabled.disabled_reason, "unconfigured")
        with self.assertRaisesRegex(ValueError, "configured together"):
            load_reolink_snapshot_config({
                "GATE_REOLINK_SNAPSHOT_BASE_URL": "https://192.168.0.54",
            }, Path("/state/uploads"), max_candidate_bytes=1024)

    def test_base_url_must_be_a_clean_private_or_loopback_literal_https_origin(self):
        unsafe = (
            "http://192.168.0.54",
            "https://camera.local",
            "https://8.8.8.8",
            "https://0.0.0.0",
            "https://192.0.2.1",
            "https://admin:secret@192.168.0.54",
            "https://192.168.0.54/cgi-bin/api.cgi",
            "https://192.168.0.54?token=secret",
            "https://192.168.0.54#camera",
        )
        for base_url in unsafe:
            with self.subTest(base_url=base_url), self.assertRaisesRegex(
                ValueError, "private literal HTTPS origin"
            ):
                load_reolink_snapshot_config({
                    "GATE_REOLINK_SNAPSHOT_BASE_URL": base_url,
                    "GATE_REOLINK_SNAPSHOT_USERNAME": "user",
                    "GATE_REOLINK_SNAPSHOT_PASSWORD": "password",
                }, Path("/state/uploads"), max_candidate_bytes=1024)

    def test_count_timeout_bytes_and_tls_opt_in_have_conservative_parsing_and_ceilings(self):
        base = {
            "GATE_REOLINK_SNAPSHOT_BASE_URL": "https://192.168.0.54",
            "GATE_REOLINK_SNAPSHOT_USERNAME": "user",
            "GATE_REOLINK_SNAPSHOT_PASSWORD": "password",
        }
        unsafe = (
            {"GATE_REOLINK_SNAPSHOT_COUNT": str(MAX_REOLINK_SNAPSHOT_COUNT + 1)},
            {"GATE_REOLINK_SNAPSHOT_COUNT": "0"},
            {"GATE_REOLINK_SNAPSHOT_TIMEOUT_SECONDS": str(MAX_REOLINK_SNAPSHOT_TIMEOUT_SECONDS + 0.1)},
            {"GATE_REOLINK_SNAPSHOT_TIMEOUT_SECONDS": "nan"},
            {"GATE_REOLINK_SNAPSHOT_MAX_BYTES": "1048577"},
            {"GATE_REOLINK_SNAPSHOT_ALLOW_SELF_SIGNED": "sometimes"},
        )
        for override in unsafe:
            with self.subTest(override=override), self.assertRaisesRegex(
                ValueError, "Reolink snapshot"
            ):
                load_reolink_snapshot_config(
                    base | override, Path("/state/uploads"), max_candidate_bytes=1024 * 1024
                )


class ReolinkSnapshotClientTests(unittest.TestCase):
    def create_config(self, root: Path):
        return load_reolink_snapshot_config({
            "GATE_REOLINK_SNAPSHOT_BASE_URL": "https://192.168.0.54",
            "GATE_REOLINK_SNAPSHOT_USERNAME": "private-user",
            "GATE_REOLINK_SNAPSHOT_PASSWORD": "private-password",
            "GATE_REOLINK_SNAPSHOT_ALLOW_SELF_SIGNED": "true",
        }, root / "uploads", max_candidate_bytes=1024 * 1024)

    def test_login_snapshot_and_logout_keep_credentials_out_of_the_base_url(self):
        login = FakeResponse(
            b'[{"cmd":"Login","code":0,"value":{"Token":{"name":"private-token","leaseTime":3600}}}]'
        )
        snap = FakeResponse(jpeg_bytes(), content_type="image/jpeg")
        logout = FakeResponse(b'[{"cmd":"Logout","code":0}]')
        opener = ScriptedOpener((login, snap, logout))
        with tempfile.TemporaryDirectory() as directory:
            client = ReolinkSnapshotClient(self.create_config(Path(directory)), opener=opener)

            token = client.login(timeout=0.5)
            image = client.snapshot(token, sequence=1, timeout=0.9)
            client.logout(token, timeout=0.2)

        self.assertEqual(token, "private-token")
        self.assertEqual(image.content_type, "image/jpeg")
        self.assertTrue(image.data.startswith(b"\xff\xd8\xff"))
        login_request, snapshot_request, logout_request = [item[0] for item in opener.requests]
        self.assertNotIn("private-user", login_request.full_url)
        self.assertNotIn("private-password", login_request.full_url)
        self.assertIsNotNone(login_request.data)
        self.assertIn("cmd=Snap", snapshot_request.full_url)
        self.assertIn("cmd=Logout", logout_request.full_url)

    def test_redirects_are_never_followed_or_returned_as_camera_data(self):
        redirect = HTTPError(
            "https://192.168.0.54/cgi-bin/api.cgi", 302, "Found",
            {"Location": "https://example.com/steal"}, None,
        )
        with tempfile.TemporaryDirectory() as directory:
            client = ReolinkSnapshotClient(
                self.create_config(Path(directory)), opener=ScriptedOpener((redirect,))
            )

            with self.assertRaisesRegex(SnapshotFailure, "redirect_rejected"):
                client.login(timeout=0.5)

        self.assertIsNone(RejectRedirects().redirect_request(None, None, 302, "Found", {}, None))

    def test_transport_timeout_has_a_safe_explicit_failure_category(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ReolinkSnapshotClient(
                self.create_config(Path(directory)),
                opener=ScriptedOpener((TimeoutError("private transport detail"),)),
            )

            with self.assertRaisesRegex(SnapshotFailure, "^timeout$"):
                client.login(timeout=0.5)

    def test_snapshot_body_read_obeys_one_absolute_timeout(self):
        clock = MutableClock()
        response = DripResponse(
            jpeg_bytes() * 2000, clock, seconds_per_read=0.3,
        )
        with tempfile.TemporaryDirectory() as directory:
            client = ReolinkSnapshotClient(
                self.create_config(Path(directory)),
                opener=ScriptedOpener((response,)), clock=clock,
            )

            with self.assertRaisesRegex(SnapshotFailure, "^timeout$"):
                client.snapshot("private-token", sequence=1, timeout=0.5)


class ReolinkSnapshotSamplerTests(unittest.TestCase):
    def create_config(self, root: Path, **override):
        environment = {
            "GATE_REOLINK_SNAPSHOT_BASE_URL": "https://192.168.0.54",
            "GATE_REOLINK_SNAPSHOT_USERNAME": "private-user",
            "GATE_REOLINK_SNAPSHOT_PASSWORD": "private-password",
            "GATE_REOLINK_SNAPSHOT_ALLOW_SELF_SIGNED": "true",
        } | override
        return load_reolink_snapshot_config(
            environment, root / "uploads", max_candidate_bytes=1024 * 1024
        )

    def test_ftp_candidate_flushes_while_two_snapshots_form_a_later_progressive_burst(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "uploads").mkdir()
            clock = MutableClock()
            received_at = datetime(2026, 8, 19, 18, 30, tzinfo=timezone.utc)
            emitted = []
            ftp_collector = BurstCollector(
                emitted.append, quiet_window=0.5, ranker=lambda paths: paths,
                clock=clock, include_received_at=True,
            )
            augmentation_collector = BurstCollector(
                emitted.append, quiet_window=0.5, ranker=lambda paths: paths,
                clock=clock, include_received_at=True,
            )

            def advance(_):
                clock.value += 0.4

            client = FakeClient((
                SnapshotResponse(jpeg_bytes((10, 20, 30)), "image/jpeg"),
                SnapshotResponse(jpeg_bytes((40, 50, 60)), "image/jpeg"),
            ), on_snapshot=advance)
            sampler = ReolinkSnapshotSampler(
                self.create_config(root), augmentation_collector.add,
                client_factory=lambda _: client, clock=clock,
                run_id=lambda: "capture",
            )
            ftp = root / "uploads" / "ftp.jpg"
            ftp.write_bytes(jpeg_bytes())
            handler = CompletedImageHandler(
                ftp_collector, on_first_completed=sampler.request,
                ignored_roots=(sampler.output_directory,),
                arrival_clock=lambda: received_at,
            )

            handler.schedule_candidate(ftp)
            self.assertEqual(handler.retry_pending(), 1)
            self.assertTrue(sampler.active)
            clock.value = 0.5
            self.assertTrue(ftp_collector.flush_due())
            self.assertEqual(emitted, [((ftp,), received_at)])
            with self.assertLogs("gate_controller.reolink_snapshots", level="INFO") as logs:
                sampler.run_once(Event())
            clock.value = 1.91
            self.assertTrue(augmentation_collector.flush_due())

            paths, captured_received_at = emitted[1]
            self.assertEqual(len(paths), 2)
            self.assertEqual(captured_received_at, received_at)
            self.assertEqual(client.snapshot_calls, 2)
            self.assertEqual(client.logout_calls, 1)
            combined = "\n".join(logs.output)
            self.assertIn("source=camera_ftp subtype=unverified", combined)
            self.assertIn("augmentation=reolink_snapshot", combined)
            self.assertIn("candidate_count=2", combined)
            self.assertNotIn("private-user", combined)
            self.assertNotIn("private-password", combined)
            self.assertNotIn("private-camera-token", combined)

    def test_failure_is_isolated_and_keeps_the_already_released_ftp_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "uploads").mkdir()
            clock = MutableClock()
            emitted = []
            collector = BurstCollector(
                emitted.append, quiet_window=0.5, ranker=lambda paths: paths, clock=clock,
            )
            client = FakeClient(login_error=SnapshotFailure("timeout"))
            sampler = ReolinkSnapshotSampler(
                self.create_config(root), collector.add,
                client_factory=lambda _: client, clock=clock,
            )
            ftp = root / "uploads" / "ftp.jpg"
            ftp.write_bytes(jpeg_bytes())
            collector.add(ftp)
            sampler.request()
            clock.value = 0.5
            self.assertTrue(collector.flush_due())

            with self.assertLogs("gate_controller.reolink_snapshots", level="WARNING") as logs:
                sampler.run_once(Event())

            self.assertFalse(collector.flush_due())
            self.assertEqual(emitted, [(ftp,)])
            self.assertIn("reason=timeout", "\n".join(logs.output))
            self.assertTrue(ftp.exists())

    def test_one_failed_run_does_not_fail_or_poison_the_sampler_component(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clients = iter((
                FakeClient(login_error=RuntimeError("private camera detail")),
                FakeClient((SnapshotResponse(jpeg_bytes(), "image/jpeg"),)),
            ))
            collected = []
            sampler = ReolinkSnapshotSampler(
                self.create_config(root, GATE_REOLINK_SNAPSHOT_COUNT="1"),
                lambda path, received_at: collected.append(path),
                client_factory=lambda _: next(clients),
                run_id=lambda: "capture",
            )

            self.assertTrue(sampler.request())
            with self.assertLogs("gate_controller.reolink_snapshots", level="WARNING"):
                self.assertTrue(sampler.run_once(Event()))
            self.assertFalse(sampler.active)

            self.assertTrue(sampler.request())
            with self.assertLogs("gate_controller.reolink_snapshots", level="INFO"):
                self.assertTrue(sampler.run_once(Event()))

            self.assertEqual(len(collected), 1)
            self.assertFalse(sampler.active)

    def test_run_setup_failure_is_isolated_and_the_next_request_can_succeed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_ids = iter((RuntimeError("private entropy detail"), "capture"))
            collected = []

            def next_run_id():
                result = next(run_ids)
                if isinstance(result, BaseException):
                    raise result
                return result

            client = FakeClient((SnapshotResponse(jpeg_bytes(), "image/jpeg"),))
            sampler = ReolinkSnapshotSampler(
                self.create_config(root, GATE_REOLINK_SNAPSHOT_COUNT="1"),
                lambda path, received_at: collected.append(path),
                client_factory=lambda _: client,
                run_id=next_run_id,
            )

            self.assertTrue(sampler.request())
            with self.assertLogs("gate_controller.reolink_snapshots", level="WARNING"):
                self.assertTrue(sampler.run_once(Event()))
            self.assertFalse(sampler.active)

            self.assertTrue(sampler.request())
            with self.assertLogs("gate_controller.reolink_snapshots", level="INFO"):
                self.assertTrue(sampler.run_once(Event()))

            self.assertEqual(len(collected), 1)

    def test_single_flight_rejects_a_second_request_while_sampling_is_active(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            sampler = ReolinkSnapshotSampler(
                self.create_config(Path(directory)), lambda *_: None, clock=clock,
            )

            self.assertTrue(sampler.request())
            self.assertFalse(sampler.request())

    def test_invalid_content_oversized_and_non_jpeg_responses_are_removed(self):
        cases = (
            (SnapshotResponse(jpeg_bytes(), "text/html"), "invalid_content"),
            (SnapshotResponse(b"x" * (1024 * 1024 + 1), "image/jpeg"), "output_limit"),
            (SnapshotResponse(b"not-a-jpeg", "image/jpeg"), "invalid_jpeg"),
        )
        for response, reason in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                client = FakeClient((response,))
                sampler = ReolinkSnapshotSampler(
                    self.create_config(
                        root, GATE_REOLINK_SNAPSHOT_COUNT="1",
                        GATE_REOLINK_SNAPSHOT_MAX_BYTES=str(1024 * 1024),
                    ), lambda *_: self.fail("invalid sample reached the collector"),
                    client_factory=lambda _: client, run_id=lambda: "capture",
                )
                sampler.request()

                with self.assertLogs("gate_controller.reolink_snapshots", level="WARNING") as logs:
                    sampler.run_once(Event())

                self.assertIn(f"reason={reason}", "\n".join(logs.output))
                self.assertEqual(list(sampler.output_directory.glob("*.jpg")), [])

    def test_generated_snapshot_directory_is_ignored_and_shutdown_cleans_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uploads = root / "uploads"
            uploads.mkdir()
            config = self.create_config(root)
            config.output_directory.mkdir(mode=0o700)
            generated = config.output_directory / "old.jpg"
            generated.write_bytes(jpeg_bytes())
            ftp = uploads / "camera.jpg"
            ftp.write_bytes(jpeg_bytes())
            collected = []
            sample_requests = []
            collector = BurstCollector(lambda paths: collected.append(paths))
            sampler = ReolinkSnapshotSampler(config, collector.add)
            handler = CompletedImageHandler(
                collector, on_first_completed=lambda received_at: sample_requests.append(received_at),
                ignored_roots=(config.output_directory,),
            )
            reconciler = StartupReconciler(uploads, handler, max_image_age=8, clock=lambda: ftp.stat().st_mtime)

            while reconciler.run_batch():
                pass
            handler.retry_pending()
            sampler.close()

            self.assertEqual(len(sample_requests), 1)
            self.assertFalse(generated.exists())
            self.assertEqual(handler.pending_count, 0)

    def test_trigger_only_exposes_candidates_to_ocr_and_has_no_actuation_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ocr_candidates = []
            relay_calls = []
            client = FakeClient((SnapshotResponse(jpeg_bytes(), "image/jpeg"),))
            sampler = ReolinkSnapshotSampler(
                self.create_config(root, GATE_REOLINK_SNAPSHOT_COUNT="1"),
                lambda path, received_at: ocr_candidates.append((path, received_at)),
                client_factory=lambda _: client,
                run_id=lambda: "capture",
            )

            self.assertNotIn("relay", sampler.__dict__)
            self.assertTrue(sampler.request())
            sampler.run_once(Event())

            self.assertEqual(len(ocr_candidates), 1)
            self.assertEqual(relay_calls, [])


if __name__ == "__main__":
    unittest.main()
