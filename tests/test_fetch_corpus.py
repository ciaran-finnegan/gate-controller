"""The way back out of the archive, and the things it must not do."""
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fetch_corpus  # noqa: E402


class ArchiveFromEnvironment(unittest.TestCase):
    def test_says_which_credentials_are_missing_without_printing_any(self):
        with self.assertRaises(SystemExit) as raised:
            fetch_corpus.Archive.from_environment({
                "GATE_CLOUDFLARE_API_URL": "https://gate.example",
                "GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET": "s3cret",
            })

        message = str(raised.exception)
        self.assertIn("GATE_CLOUDFLARE_ACCESS_CLIENT_ID", message)
        self.assertNotIn("s3cret", message)

    def test_the_token_travels_as_headers_and_never_in_the_url(self):
        archive = fetch_corpus.Archive("https://gate.example/", "id-1", "s3cret")
        captured = {}

        def fake_urlopen(request_object, timeout=None):
            captured["url"] = request_object.full_url
            captured["headers"] = request_object.headers
            return io.BytesIO(json.dumps({"artefacts": []}).encode())

        with mock.patch.object(fetch_corpus.request, "urlopen", fake_urlopen):
            archive.list(kind="audio", since="2026-09-16")

        self.assertNotIn("s3cret", captured["url"])
        self.assertIn("kind=audio", captured["url"])
        self.assertEqual(captured["headers"]["Cf-access-client-secret"], "s3cret")


class Filenames(unittest.TestCase):
    def test_a_name_sorts_by_time_and_is_unique_on_the_digest(self):
        name = fetch_corpus._filename({
            "captured_at": "2026-09-16T19:23:25.000Z",
            "media_type": "audio/aac",
            "artefact_id": "3fa9c1deadbeef",
        })

        self.assertEqual(name, "20260916T192325Z-3fa9c1.aac")

    def test_an_unknown_media_type_still_lands_somewhere_openable(self):
        name = fetch_corpus._filename({
            "captured_at": "2026-09-16T19:23:25Z",
            "media_type": "application/x-unheard-of",
            "artefact_id": "abcdef123456",
        })

        self.assertTrue(name.endswith(".bin"), name)


class Fetching(unittest.TestCase):
    def setUp(self):
        self.archive = fetch_corpus.Archive("https://gate.example", "id-1", "secret")
        self.listing = [{
            "artefact_id": "a" * 64, "kind": "audio", "media_type": "audio/aac",
            "captured_at": "2026-09-16T19:23:25Z", "byte_size": 4,
        }]

    def _args(self, out, sidecars=False):
        return mock.Mock(kind="audio", since=None, until=None, limit=10,
                         out=str(out), sidecars=sidecars)

    def test_what_is_already_here_is_not_downloaded_twice(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            reads = []
            with mock.patch.object(self.archive, "list", return_value=self.listing), \
                 mock.patch.object(self.archive, "read",
                                   side_effect=lambda *a, **k: reads.append(a) or b"\xff\xf1ab"):
                fetch_corpus.command_fetch(self.archive, self._args(directory))
                fetch_corpus.command_fetch(self.archive, self._args(directory))

            self.assertEqual(len(reads), 1, "the second pass re-downloaded it")
            written = list(Path(directory).glob("*.aac"))
            self.assertEqual(len(written), 1)
            self.assertEqual(written[0].read_bytes(), b"\xff\xf1ab")


if __name__ == "__main__":
    unittest.main()
