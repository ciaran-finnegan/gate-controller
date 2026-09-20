import base64
import hashlib
import hmac
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import gate_media_auth.__main__ as media_auth_main
from gate_media_auth.__main__ import authorize_body
from gate_media_config import (
    MediaConfigError,
    ensure_talk_credential,
    generate_talk_credential,
    parse_trusted_environment,
    talk_rtsp_url,
    validate_talk_credential_environment,
)
from gate_media_auth.token import TokenValidationError, validate_media_token


SECRET = "0123456789abcdef0123456789abcdef"
# This host's /etc/gate-media/talk.env, as the sidecar holds it.
_TALK_CREDENTIAL_VALUES = generate_talk_credential()
TALK_USERNAME = _TALK_CREDENTIAL_VALUES["GATE_TALK_RTSP_USERNAME"]
TALK_PASSWORD = _TALK_CREDENTIAL_VALUES["GATE_TALK_RTSP_PASSWORD"]
TALK_CREDENTIAL = (TALK_USERNAME, TALK_PASSWORD)


def authorize(payload, *, now, talk_credential=TALK_CREDENTIAL):
    return authorize_body(
        json.dumps(payload).encode("utf-8"), SECRET, now=now,
        talk_credential=talk_credential,
    )


def make_token(claims, secret=SECRET):
    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    header = encode(b'{"alg":"HS256","typ":"JWT"}')
    payload = encode(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(
        secret.encode("utf-8"), f"{header}.{payload}".encode("ascii"), hashlib.sha256
    ).digest()
    return f"{header}.{payload}.{encode(signature)}"


def valid_claims(now=1_700_000_000):
    return {
        "v": 1,
        "sub": "viewer-42",
        "controller": "primary",
        "path": "gate",
        "actions": ["read"],
        "iat": now - 1,
        "exp": now + 59,
        "nonce": "nonce-42",
    }


def valid_auth_request(now):
    return {
        "user": "",
        "password": "",
        "token": make_token(valid_claims(now)),
        "ip": "127.0.0.1",
        "action": "read",
        "path": "gate",
        "protocol": "webrtc",
        "id": "session-42",
        "query": "",
        "userAgent": "test-agent",
    }


def valid_talk_claims(now=1_700_000_000):
    claims = valid_claims(now)
    claims["path"] = "talk"
    claims["actions"] = ["publish"]
    return claims


def talk_publish_request(now, token=None):
    request = valid_auth_request(now)
    request["token"] = token or make_token(valid_talk_claims(now))
    request["action"] = "publish"
    request["path"] = "talk"
    return request


def local_rtsp_request(action, path):
    """The anonymous loopback request the transcoder makes.

    `talk` is the exception and has its own builder: it carries this host's
    credential, because it is the one loopback path that is not anonymous.
    """
    return {
        "user": "",
        "password": "",
        "token": "",
        "ip": "127.0.0.1",
        "action": action,
        "path": path,
        "protocol": "rtsp",
        "id": "local-transcoder",
        "query": "",
        "userAgent": "Lavf",
    }


def local_talk_read_request():
    request = local_rtsp_request("read", "talk")
    request["user"] = TALK_USERNAME
    request["password"] = TALK_PASSWORD
    return request


class MediaTokenTests(unittest.TestCase):
    def test_accepts_a_current_read_token_for_the_primary_gate(self):
        claims = valid_claims()

        result = validate_media_token(make_token(claims), SECRET, now=1_700_000_000)

        self.assertEqual(claims, result)

    def test_rejects_changed_payload_or_signature(self):
        token = make_token(valid_claims())
        header, payload, signature = token.split(".")
        changed_payload = f"{header}.{payload[:-1]}{'A' if payload[-1] != 'A' else 'B'}.{signature}"
        changed_signature = f"{header}.{payload}.{signature[:-1]}{'A' if signature[-1] != 'A' else 'B'}"

        for value in (changed_payload, changed_signature):
            with self.subTest(value=value), self.assertRaises(TokenValidationError):
                validate_media_token(value, SECRET, now=1_700_000_000)

    def test_rejects_expired_or_future_issued_tokens(self):
        expired = valid_claims()
        expired["exp"] = 1_699_999_999
        future = valid_claims()
        future["iat"] = 1_700_000_006

        for claims in (expired, future):
            with self.subTest(claims=claims), self.assertRaises(TokenValidationError):
                validate_media_token(make_token(claims), SECRET, now=1_700_000_000)

    def test_rejects_wrong_action_path_or_controller(self):
        for field, value in (
            ("actions", ["publish"]),
            ("path", "other"),
            ("controller", "secondary"),
        ):
            claims = valid_claims()
            claims[field] = value
            with self.subTest(field=field), self.assertRaises(TokenValidationError):
                validate_media_token(make_token(claims), SECRET, now=1_700_000_000)

    def test_a_talk_token_is_exactly_a_publish_on_talk_and_nothing_else(self):
        talk = make_token(valid_talk_claims())
        gate = make_token(valid_claims())

        self.assertEqual(
            valid_talk_claims(), validate_media_token(talk, SECRET, now=1_700_000_000, path="talk")
        )
        for token, path in ((talk, "gate"), (gate, "talk"), (talk, "other")):
            with self.subTest(path=path), self.assertRaises(TokenValidationError):
                validate_media_token(token, SECRET, now=1_700_000_000, path=path)
        for field, value in (("actions", ["read"]), ("actions", ["publish", "read"]),
                             ("path", "gate")):
            claims = valid_talk_claims()
            claims[field] = value
            with self.subTest(field=field), self.assertRaises(TokenValidationError):
                validate_media_token(make_token(claims), SECRET, now=1_700_000_000, path="talk")

    def test_rejects_malformed_base64_and_nonconstant_claim_shape(self):
        malformed = ("not-base64", "a.b.c", "eyJhbGciOiJIUzI1NiJ9.!.signature")
        extra_claim = valid_claims()
        extra_claim["role"] = "admin"

        for token in (*malformed, make_token(extra_claim)):
            with self.subTest(token=token), self.assertRaises(TokenValidationError):
                validate_media_token(token, SECRET, now=1_700_000_000)

    def test_uses_compare_digest_for_signature_comparison(self):
        source = (Path(__file__).resolve().parents[1] / "gate_media_auth/token.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("hmac.compare_digest", source)


class MediaAuthServerTests(unittest.TestCase):
    def test_returns_exact_200_for_valid_mediamtx_read_auth(self):
        payload = json.dumps(valid_auth_request(int(time.time())))

        status = authorize_body(payload.encode("utf-8"), SECRET, now=int(time.time()))

        self.assertEqual(200, status)

    def test_allows_only_the_hot_stream_and_transcoder_loopback_rtsp_operations(self):
        now = int(time.time())

        for action, path in (
            ("read", "camera"),
            ("read", "clear"),
            ("publish", "gate"),
        ):
            with self.subTest(action=action, path=path):
                self.assertEqual(200, authorize(local_rtsp_request(action, path), now=now))
        self.assertEqual(200, authorize(local_talk_read_request(), now=now))

    def test_rejects_every_near_miss_local_rtsp_operation(self):
        now = int(time.time())
        mutations = (
            ("user", "transcoder"),
            ("password", "secret"),
            ("token", "secret"),
            ("query", "token=secret"),
            ("ip", "::1"),
            ("ip", "127.0.0.2"),
            ("protocol", "webrtc"),
            ("action", "playback"),
            ("path", "other"),
        )
        allowed = (
            ("read", "camera"),
            ("read", "clear"),
            ("publish", "gate"),
        )

        for action, path in allowed:
            for field, value in mutations:
                payload = local_rtsp_request(action, path)
                payload[field] = value
                with self.subTest(action=action, path=path, field=field, value=value):
                    self.assertEqual(401, authorize(payload, now=now))
        for field, value in mutations:
            payload = local_talk_read_request()
            payload[field] = value
            with self.subTest(path="talk", field=field, value=value):
                self.assertEqual(401, authorize(payload, now=now))
        for action, path in (
            ("read", "gate"),
            ("publish", "camera"),
            ("publish", "clear"),
            # Only the browser, on a talk token over WebRTC, may publish talk.
            ("publish", "talk"),
        ):
            with self.subTest(action=action, path=path):
                self.assertEqual(401, authorize(local_rtsp_request(action, path), now=now))

    def test_reading_talk_over_loopback_needs_this_host_exact_credential(self):
        """The operator's live microphone is not readable by being on the Pi.

        `camera` and `clear` are the camera's own stream and stay anonymous;
        `talk` carries what the operator is saying into their phone, so the
        sidecar asks for the credential only gate-camera-control has.
        """
        now = int(time.time())

        self.assertEqual(200, authorize(local_talk_read_request(), now=now))
        rejected = [local_rtsp_request("read", "talk")]
        for field, value in (
            ("user", ""),
            ("password", ""),
            ("user", "gate-camera-control"),
            ("user", TALK_PASSWORD),
            ("password", TALK_USERNAME),
            ("password", TALK_PASSWORD[:-1]),
            ("password", TALK_PASSWORD + "x"),
            ("password", TALK_PASSWORD.upper()),
        ):
            payload = local_talk_read_request()
            payload[field] = value
            rejected.append(payload)
        for payload in rejected:
            with self.subTest(user=payload["user"][:4], password=payload["password"][:4]):
                self.assertEqual(401, authorize(payload, now=now))

    def test_mediamtx_echoes_the_rtsp_password_into_token_and_that_is_accepted(self):
        """Verified against a real MediaMTX 1.19.3 on 2026-09-20.

        For an RTSP Basic request it fills `token` with the same password it
        puts in `password`. The anonymous paths never see one, so requiring an
        empty `token` everywhere looked right and refused the very credential
        MediaMTX had just forwarded -- the talk pull 401'd every time. The field
        is still pinned to that one value, so nothing else can ride in on it.
        """
        now = int(time.time())
        echoed = local_talk_read_request()
        echoed["token"] = TALK_PASSWORD

        self.assertEqual(200, authorize(echoed, now=now))
        for value in (TALK_USERNAME, "anything-else", make_token(valid_talk_claims(now))):
            smuggled = local_talk_read_request()
            smuggled["token"] = value
            with self.subTest(token=value[:8]):
                self.assertEqual(401, authorize(smuggled, now=now))
        # The anonymous paths are unchanged: no user, no password, no token.
        for action, path in (("read", "camera"), ("read", "clear"), ("publish", "gate")):
            payload = local_rtsp_request(action, path)
            payload["token"] = TALK_PASSWORD
            with self.subTest(action=action, path=path):
                self.assertEqual(401, authorize(payload, now=now))

    def test_a_non_ascii_field_is_refused_rather_than_raising(self):
        """`hmac.compare_digest` raises TypeError on a str with non-ASCII in it.

        The fields come out of JSON, so a caller could pick them. Unhandled,
        that took the handler thread down instead of answering 401.
        """
        now = int(time.time())

        for field in ("user", "password", "token"):
            payload = local_talk_read_request()
            payload[field] = "caf\u00e9" * 4
            with self.subTest(field=field):
                self.assertEqual(401, authorize(payload, now=now))
        # And a valid request is still 200 with a credential that is pure ASCII.
        self.assertEqual(200, authorize(local_talk_read_request(), now=now))

    def test_a_host_with_no_credential_refuses_the_talk_read_outright(self):
        """Fail closed. A sidecar that cannot tell gate-camera-control from any
        other local process must not hand either of them the microphone."""
        now = int(time.time())

        self.assertEqual(
            401, authorize(local_talk_read_request(), now=now, talk_credential=None)
        )
        self.assertEqual(
            401, authorize(local_rtsp_request("read", "talk"), now=now, talk_credential=None)
        )
        # Everything else still works without one.
        for action, path in (("read", "camera"), ("read", "clear"), ("publish", "gate")):
            with self.subTest(action=action, path=path):
                self.assertEqual(
                    200,
                    authorize(local_rtsp_request(action, path), now=now, talk_credential=None),
                )

    def test_the_talk_credential_opens_nothing_but_the_talk_read(self):
        now = int(time.time())

        for action, path in (
            ("read", "camera"), ("read", "clear"), ("publish", "gate"),
            ("publish", "talk"), ("read", "gate"), ("publish", "camera"),
        ):
            payload = local_rtsp_request(action, path)
            payload["user"] = TALK_USERNAME
            payload["password"] = TALK_PASSWORD
            with self.subTest(action=action, path=path):
                self.assertEqual(401, authorize(payload, now=now))

    def test_a_talk_token_publishes_talk_over_webrtc_and_nothing_else(self):
        now = int(time.time())
        talk = make_token(valid_talk_claims(now))
        gate = make_token(valid_claims(now))

        self.assertEqual(
            200, authorize_body(json.dumps(talk_publish_request(now)).encode("utf-8"),
                                SECRET, now=now),
        )
        rejected = []
        with_gate_token = talk_publish_request(now, token=gate)
        rejected.append(with_gate_token)
        read_gate_with_talk_token = valid_auth_request(now)
        read_gate_with_talk_token["token"] = talk
        rejected.append(read_gate_with_talk_token)
        publish_gate = talk_publish_request(now)
        publish_gate["path"] = "gate"
        rejected.append(publish_gate)
        read_talk = talk_publish_request(now)
        read_talk["action"] = "read"
        rejected.append(read_talk)
        over_rtsp = talk_publish_request(now)
        over_rtsp["protocol"] = "rtsp"
        rejected.append(over_rtsp)
        for payload in rejected:
            with self.subTest(action=payload["action"], path=payload["path"],
                              protocol=payload["protocol"]):
                self.assertEqual(
                    401, authorize_body(json.dumps(payload).encode("utf-8"), SECRET, now=now),
                )

    def test_rejects_removal_of_every_required_mediamtx_auth_field(self):
        now = int(time.time())
        valid = valid_auth_request(now)

        for field in valid:
            incomplete = dict(valid)
            incomplete.pop(field)
            with self.subTest(field=field):
                status = authorize_body(
                    json.dumps(incomplete).encode("utf-8"), SECRET, now=now
                )
                self.assertEqual(401, status)

    def test_rejects_every_non_webrtc_protocol(self):
        now = int(time.time())
        for protocol in ("rtsp", "rtmp", "hls", "srt", ""):
            payload = valid_auth_request(now)
            payload["protocol"] = protocol
            with self.subTest(protocol=protocol):
                self.assertEqual(
                    401,
                    authorize_body(json.dumps(payload).encode("utf-8"), SECRET, now=now),
                )

    def test_returns_exact_401_for_duplicate_unknown_or_missing_token_fields(self):
        token = make_token(valid_claims(int(time.time())))
        payloads = (
            '{"token":"first","token":"second","action":"read","path":"gate"}',
            json.dumps({"token": token, "action": "read", "path": "gate", "extra": True}),
            json.dumps({"action": "read", "path": "gate"}),
            json.dumps({"token": "Bearer ", "action": "read", "path": "gate"}),
        )

        for payload in payloads:
            with self.subTest(payload=payload):
                status = authorize_body(payload.encode("utf-8"), SECRET, now=int(time.time()))
                self.assertEqual(401, status)

    def test_rejects_invalid_method_path_schema_and_oversized_requests(self):
        invalid_action = json.dumps({
            "token": make_token(valid_claims(int(time.time()))),
            "action": "publish",
            "path": "gate",
        })
        oversized = b"{" + (b"x" * 8_193) + b"}"

        status = authorize_body(invalid_action.encode("utf-8"), SECRET, now=int(time.time()))
        self.assertEqual(401, status)
        status = authorize_body(oversized, SECRET, now=int(time.time()))
        self.assertEqual(401, status)

    def test_handler_does_not_log_request_fields_or_echo_them_in_responses(self):
        source = (Path(__file__).resolve().parents[1] / "gate_media_auth/__main__.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("def log_message", source)
        self.assertIn("def send_error", source)
        self.assertNotIn("logger.info(payload", source)
        self.assertNotIn("logger.warning(payload", source)
        self.assertLessEqual(len('{"request_id":"0000000000000000"}'), 64)


class MediaAuthConfigurationTests(unittest.TestCase):
    def test_runtime_environment_rejects_gateway_credentials_and_invalid_flags(self):
        self.assertTrue(hasattr(media_auth_main, "validated_auth_environment"))
        valid = {
            "GATE_MEDIA_HMAC_SECRET": SECRET,
            "GATE_MEDIA_VIDEO_CONFIGURED": "false",
            "GATE_MEDIA_VIDEO_VERIFIED": "false",
            "GATE_MEDIA_LISTEN_CONFIGURED": "false",
            "GATE_MEDIA_LISTEN_VERIFIED": "false",
            "GATE_MEDIA_TALKBACK_CONFIGURED": "false",
        }

        # The talkback verified flag is optional and defaults to false, so an
        # auth file from before talkback existed keeps validating unchanged.
        self.assertEqual(
            {**valid, "GATE_MEDIA_TALKBACK_VERIFIED": "false"},
            media_auth_main.validated_auth_environment(valid),
        )
        verified = {**valid, "GATE_MEDIA_TALKBACK_CONFIGURED": "true",
                    "GATE_MEDIA_TALKBACK_VERIFIED": "true"}
        self.assertEqual(verified, media_auth_main.validated_auth_environment(verified))
        for extra in (
            {"MTX_PATHS_CAMERA_SOURCE": "rtsp://camera.example/stream"},
            {"GATE_MEDIA_VIDEO_CONFIGURED": " false"},
            {"GATE_MEDIA_TALKBACK_VERIFIED": "true"},
            {"GATE_MEDIA_TALKBACK_VERIFIED": "yes"},
        ):
            with self.subTest(extra=extra), self.assertRaises(MediaConfigError):
                media_auth_main.validated_auth_environment({**valid, **extra})


class TalkCredentialTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "talk.env"

    def test_the_sidecar_reads_only_the_two_credential_keys_from_its_environment(self):
        environment = {
            "GATE_MEDIA_HMAC_SECRET": SECRET,
            "GATE_TALK_RTSP_USERNAME": TALK_USERNAME,
            "GATE_TALK_RTSP_PASSWORD": TALK_PASSWORD,
            "PATH": "/usr/bin",
        }

        self.assertEqual(
            TALK_CREDENTIAL, media_auth_main.validated_talk_credential(environment)
        )
        self.assertIsNone(media_auth_main.validated_talk_credential({"PATH": "/usr/bin"}))
        for broken in (
            {"GATE_TALK_RTSP_USERNAME": TALK_USERNAME},
            {"GATE_TALK_RTSP_PASSWORD": TALK_PASSWORD},
            {"GATE_TALK_RTSP_USERNAME": "short", "GATE_TALK_RTSP_PASSWORD": TALK_PASSWORD},
            {"GATE_TALK_RTSP_USERNAME": TALK_USERNAME, "GATE_TALK_RTSP_PASSWORD": TALK_USERNAME},
            {"GATE_TALK_RTSP_USERNAME": "has:colon" * 2,
             "GATE_TALK_RTSP_PASSWORD": TALK_PASSWORD},
        ):
            with self.subTest(keys=sorted(broken)), self.assertRaises(MediaConfigError):
                media_auth_main.validated_talk_credential(broken)

    def test_the_credential_file_is_minted_once_and_never_rewritten(self):
        self.assertTrue(ensure_talk_credential(self.path))
        first = self.path.read_bytes()

        self.assertFalse(ensure_talk_credential(self.path))
        self.assertEqual(first, self.path.read_bytes())
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        self.assertEqual([self.path.name], [entry.name for entry in self.path.parent.iterdir()])
        values = validate_talk_credential_environment(parse_trusted_environment(self.path))
        self.assertEqual(
            {"GATE_TALK_RTSP_USERNAME", "GATE_TALK_RTSP_PASSWORD"}, set(values)
        )

    def test_the_zero_length_placeholder_both_installers_create_is_populated(self):
        self.path.touch(mode=0o600)

        self.assertTrue(ensure_talk_credential(self.path))
        self.assertTrue(
            validate_talk_credential_environment(parse_trusted_environment(self.path))
        )

    def test_two_credentials_minted_on_one_host_are_never_the_same(self):
        seen = {
            tuple(sorted(generate_talk_credential().items())) for _ in range(20)
        }

        self.assertEqual(20, len(seen))

    def test_the_url_carries_the_credential_and_refuses_to_build_a_second_one(self):
        credential = generate_talk_credential()

        url = talk_rtsp_url("rtsp://127.0.0.1:8554/talk", credential)

        self.assertEqual(
            f"rtsp://{credential['GATE_TALK_RTSP_USERNAME']}:"
            f"{credential['GATE_TALK_RTSP_PASSWORD']}@127.0.0.1:8554/talk",
            url,
        )
        for base in (url, "127.0.0.1:8554/talk", "rtsp://", ""):
            with self.subTest(base=base[:12]), self.assertRaises(MediaConfigError):
                talk_rtsp_url(base, credential)


class IsolationTests(unittest.TestCase):
    def test_media_package_has_no_controller_or_relay_import_or_call_path(self):
        root = Path(__file__).resolve().parents[1]
        sources = [root / "gate_media_config.py"]
        sources.extend((root / "gate_media_auth").glob("*.py"))
        sources.extend((root / "gate_media_gateway").glob("*.py"))
        sources.extend((root / "gate_media_transcoder").glob("*.py"))
        forbidden = ("gate_controller.relay", "gate_controller.actuation", "PiRelay")
        for source in sources:
            contents = source.read_text(encoding="utf-8")
            for value in forbidden:
                with self.subTest(source=source.name, value=value):
                    self.assertNotIn(value, contents)


if __name__ == "__main__":
    unittest.main()
