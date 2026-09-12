"""Push-to-talk: the Baichuan talk client, the ADPCM encoder, and the bounded session."""

import configparser
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from gate_camera_control import baichuan
from gate_camera_control.adpcm import (
    ImaAdpcmEncoder, decode_block, pcm16_to_samples, samples_per_block,
)
from gate_camera_control.aes import Aes128
from gate_camera_control.__main__ import CameraControlServer, build_service
from gate_camera_control.state import StatePublisher, state_document
from gate_camera_control.talk import (
    HARD_MAX_SECONDS, MIN_SECONDS, TalkBusySession, TalkController, TalkUnavailable,
    ffmpeg_command,
)
from gate_controller.camera_control_state import read_camera_control_state
from tests.fake_baichuan import FakeBaichuanCamera
from tests.test_camera_control import FakeCamera, ManualClock, RecordingJournal


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RATE = 16000

# A stand-in for ffmpeg: real-time PCM16 mono at 16 kHz on stdout, a 440 Hz
# tone, for `limit` seconds or until it is terminated. Real process, real pipe,
# real SIGTERM -- the session's hard limit is a kill, and the test must see it.
FAKE_FFMPEG = r"""
import math, struct, sys, time
limit = float(sys.argv[1])
rate, chunk, produced = 16000, 320, 0
started = time.monotonic()
out = sys.stdout.buffer
while limit <= 0 or produced < limit * rate:
    out.write(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * (produced + i) / rate)))
                       for i in range(chunk)))
    out.flush()
    produced += chunk
    ahead = produced / rate - (time.monotonic() - started)
    if ahead > 0:
        time.sleep(ahead)
"""


def fake_spawn(limit_seconds):
    def spawn(command, **keywords):
        assert command[0] == sys.executable, command
        assert command[-1] == "pipe:1" and "-i" in command
        return subprocess.Popen([sys.executable, "-c", FAKE_FFMPEG, str(limit_seconds)],
                                **keywords)
    return spawn


class FakeGateway:
    """The MediaMTX loopback API as the controller uses it: one path, one kick."""

    def __init__(self, *, ready_after=0, publisher="9c1b0e4e-talk-session"):
        self.ready_after = ready_after
        self.publisher = publisher
        self.polls = 0
        self.kicks = []
        self.lock = threading.Lock()

    def __call__(self, request, timeout=None):
        url = request if isinstance(request, str) else request.full_url
        method = "GET" if isinstance(request, str) else request.get_method()
        with self.lock:
            if url.endswith("/v3/paths/get/talk") and method == "GET":
                self.polls += 1
                if self.polls <= self.ready_after or self.publisher is None:
                    raise urllib.error.HTTPError(url, 404, "not found", {}, None)
                body = json.dumps({
                    "name": "talk", "ready": True,
                    "source": {"type": "webRTCSession", "id": self.publisher},
                    "tracks": ["Opus"],
                }).encode("utf-8")
                return _FakeResponse(200, body)
            if "/v3/webrtcsessions/kick/" in url and method == "POST":
                self.kicks.append(url.rsplit("/", 1)[1])
                return _FakeResponse(200, b"")
        raise urllib.error.HTTPError(url, 404, "not found", {}, None)


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = BytesIO(body)

    def read(self, size=-1):
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_arguments):
        return False


def signal_to_noise(reference, decoded) -> float:
    pairs = list(zip(reference, decoded))
    noise = sum((left - right) ** 2 for left, right in pairs) / len(pairs)
    power = sum(left * left for left, _ in pairs) / len(pairs)
    return 10 * math.log10(power / max(noise, 1e-9))


def tone(samples, rate=SAMPLE_RATE, frequency=440, amplitude=8000):
    return [int(amplitude * math.sin(2 * math.pi * frequency * index / rate))
            for index in range(samples)]


class AesTests(unittest.TestCase):
    def test_fips_197_forward_cipher_vector(self):
        cipher = Aes128(bytes.fromhex("000102030405060708090a0b0c0d0e0f"))
        self.assertEqual(
            "69c4e0d86a7b0430d8cdb78070b4c55a",
            cipher.encrypt_block(bytes.fromhex("00112233445566778899aabbccddeeff")).hex(),
        )

    def test_nist_sp800_38a_cfb128_vector_and_partial_final_segment(self):
        cipher = Aes128(bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c"))
        iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plaintext = bytes.fromhex(
            "6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e51"
            "30c81c46a35ce411e5fbc1191a0a52eff69f2445df4f9b17ad2b417be66c3710"
        )
        expected = bytes.fromhex(
            "3b3fd92eb72dad20333449f8e83cfb4ac8a64537a0b3a93fcde3cdad9f1ce58b"
            "26751f67a3cbb140b1808cf187a4f4dfc04b05357c5d1c0eeac4c66f9ff7f2e6"
        )
        self.assertEqual(expected, cipher.cfb_encrypt(iv, plaintext))
        self.assertEqual(plaintext, cipher.cfb_decrypt(iv, expected))
        # The camera's XML is never a whole number of blocks.
        for length in (1, 15, 17, 33):
            with self.subTest(length=length):
                self.assertEqual(expected[:length], cipher.cfb_encrypt(iv, plaintext[:length]))
                self.assertEqual(plaintext[:length], cipher.cfb_decrypt(iv, expected[:length]))

    def test_key_and_block_sizes_are_enforced(self):
        with self.assertRaises(ValueError):
            Aes128(b"short")
        with self.assertRaises(ValueError):
            Aes128(bytes(16)).encrypt_block(bytes(15))
        with self.assertRaises(ValueError):
            Aes128(bytes(16)).cfb_encrypt(bytes(8), b"x")


class AdpcmTests(unittest.TestCase):
    def test_blocks_are_dvi_layout_and_round_trip_through_the_matching_decoder(self):
        encoder = ImaAdpcmEncoder(516)
        self.assertEqual(1025, encoder.samples_per_block)
        reference = tone(1025 * 4)
        decoded = []
        for index in range(4):
            block = encoder.encode_block(reference[index * 1025:(index + 1) * 1025])
            self.assertEqual(516, len(block))
            # Header: first sample as int16 LE, then the carried step index, then 0.
            self.assertEqual(reference[index * 1025],
                             int.from_bytes(block[0:2], "little", signed=True))
            self.assertEqual(0, block[3])
            decoded.extend(decode_block(block))
        self.assertEqual(len(reference), len(decoded))
        self.assertGreater(signal_to_noise(reference, decoded), 25.0)
        # The step index carries across blocks: a second block does not restart at 0.
        self.assertNotEqual(0, encoder.step_index)

    def test_low_nibble_holds_the_earlier_sample(self):
        encoder = ImaAdpcmEncoder(8)
        # Header sample 0, then a large positive step followed by silence.
        block = encoder.encode_block([0, 20000, 0, 0, 0, 0, 0, 0, 0])
        first_nibble, second_nibble = block[4] & 0x0F, block[4] >> 4
        self.assertEqual(7, first_nibble)  # the biggest positive step
        self.assertEqual(8, second_nibble & 8)  # then back down

    def test_input_bounds(self):
        encoder = ImaAdpcmEncoder(516)
        with self.assertRaises(ValueError):
            encoder.encode_block([0] * 10)
        with self.assertRaises(ValueError):
            ImaAdpcmEncoder(4)
        with self.assertRaises(ValueError):
            pcm16_to_samples(b"\x00")
        self.assertEqual([1, -2], pcm16_to_samples(b"\x01\x00\xfe\xff"))
        self.assertEqual(9, samples_per_block(8))
        with self.assertRaises(ValueError):
            decode_block(b"\x00\x00\x59\x00\x00")  # step index 89

    def test_extremes_clamp_rather_than_wrap(self):
        encoder = ImaAdpcmEncoder(516)
        block = encoder.encode_block([32767] + [-32768] * 512 + [32767] * 512)
        decoded = decode_block(block)
        self.assertTrue(all(-32768 <= sample <= 32767 for sample in decoded))


class BcMediaFramingTests(unittest.TestCase):
    def test_an_adpcm_block_is_framed_exactly_as_neolink_frames_it(self):
        block = bytes(516)
        frame = baichuan.bcmedia_adpcm(block)
        self.assertEqual(b"01wb", frame[:4])  # 0x62773130 little-endian
        self.assertEqual(520, int.from_bytes(frame[4:6], "little"))
        self.assertEqual(520, int.from_bytes(frame[6:8], "little"))
        self.assertEqual(0x0100, int.from_bytes(frame[8:10], "little"))
        self.assertEqual(256, int.from_bytes(frame[10:12], "little"))
        self.assertEqual(block, frame[12:12 + 516])
        # neolink pads the block, not the frame, to eight bytes.
        self.assertEqual(0, (len(frame) - 12) % 8)
        self.assertEqual(12 + 516 + 4, len(frame))
        with self.assertRaises(ValueError):
            baichuan.bcmedia_adpcm(b"\x00\x00\x00\x00")

    def test_headers_carry_the_offset_only_for_modern_classes(self):
        modern = baichuan.build_header(201, 77, 0, 5, payload_offset=40)
        legacy = baichuan.build_header(1, 0, 0, 1, response_code=0xDC12,
                                       message_class=baichuan.CLASS_LEGACY)
        self.assertEqual(24, len(modern))
        self.assertEqual(b"\xf0\xde\xbc\x0a", modern[:4])
        self.assertEqual(b"\x14\x64", modern[18:20])
        self.assertEqual(40, int.from_bytes(modern[20:24], "little"))
        self.assertEqual(20, len(legacy))
        self.assertEqual(b"\x12\xdc\x14\x65", legacy[16:20])

    def test_bc_xor_is_its_own_inverse_and_md5_is_the_protocol_spelling(self):
        data = b"<?xml version=\"1.0\"?><body/>"
        self.assertEqual(data, baichuan.bc_xor(baichuan.bc_xor(data, 250), 250))
        self.assertNotEqual(data, baichuan.bc_xor(data, 0))
        self.assertEqual(31, len(baichuan.md5_modern("adminnonce")))
        self.assertEqual(baichuan.md5_modern("adminnonce"), baichuan.md5_modern("adminnonce").upper())
        self.assertEqual(16, len(baichuan.aes_key("nonce", "password")))


class TalkAbilityParsingTests(unittest.TestCase):
    def test_the_first_adpcm_config_is_chosen_and_echoed_only_as_plain_tokens(self):
        xml = (
            '<?xml version="1.0" encoding="UTF-8" ?><body><TalkAbility version="1.1">'
            "<duplexList><duplex>FDX</duplex></duplexList>"
            "<audioStreamModeList><audioStreamMode>followVideoStream</audioStreamMode>"
            "</audioStreamModeList><audioConfigList>"
            "<audioConfig><audioType>aac</audioType><sampleRate>16000</sampleRate>"
            "<samplePrecision>16</samplePrecision><lengthPerEncoder>1024</lengthPerEncoder>"
            "<soundTrack>mono</soundTrack></audioConfig>"
            "<audioConfig><audioType>adpcm</audioType><sampleRate>8000</sampleRate>"
            "<samplePrecision>16</samplePrecision><lengthPerEncoder>512</lengthPerEncoder>"
            "<soundTrack>mono</soundTrack></audioConfig>"
            "</audioConfigList></TalkAbility></body>"
        )
        audio_format = baichuan.parse_talk_ability(xml)
        self.assertEqual(8000, audio_format.sample_rate)
        self.assertEqual(512, audio_format.length_per_encoder)
        self.assertEqual(260, audio_format.block_bytes)
        self.assertEqual("adpcm8000x512", audio_format.describe())
        config = baichuan.talk_config_xml(audio_format, 0)
        self.assertIn("<sampleRate>8000</sampleRate>", config)
        self.assertIn("<duplex>FDX</duplex>", config)

    def test_unsupported_or_hostile_abilities_are_refused(self):
        template = (
            '<?xml version="1.0" encoding="UTF-8" ?><body><TalkAbility version="1.1">'
            "<duplexList><duplex>{duplex}</duplex></duplexList>"
            "<audioStreamModeList><audioStreamMode>followVideoStream</audioStreamMode>"
            "</audioStreamModeList><audioConfigList><audioConfig>"
            "<audioType>{type}</audioType><sampleRate>{rate}</sampleRate>"
            "<samplePrecision>{precision}</samplePrecision>"
            "<lengthPerEncoder>{length}</lengthPerEncoder><soundTrack>{track}</soundTrack>"
            "</audioConfig></audioConfigList></TalkAbility></body>"
        )
        good = dict(duplex="FDX", type="adpcm", rate=16000, precision=16, length=1024, track="mono")
        for change in (
            {"type": "g711"}, {"rate": 44100}, {"precision": 8}, {"length": 1023},
            {"length": 65536}, {"track": "stereo"}, {"duplex": "F D X"},
            {"duplex": "FDX;"}, {"duplex": "x" * 33},
        ):
            with self.subTest(change=change), self.assertRaises(baichuan.TalkUnsupported):
                baichuan.parse_talk_ability(template.format(**{**good, **change}))
        with self.assertRaises(baichuan.TalkUnsupported):
            baichuan.parse_talk_ability('<?xml version="1.0"?><body/>')
        for broken in ("<body", template.format(**{**good, "duplex": "FDX</duplex><x>"})):
            with self.subTest(broken=broken[:20]), self.assertRaises(baichuan.BaichuanError):
                baichuan.parse_talk_ability(broken)


class BaichuanClientTests(unittest.TestCase):
    def setUp(self):
        self.camera = FakeBaichuanCamera("gate", "s3cret").start()
        self.addCleanup(self.camera.stop)

    def client(self, **keywords):
        options = {"port": self.camera.port, "timeout": 3.0}
        options.update(keywords)
        return baichuan.BaichuanClient("127.0.0.1", "gate", "s3cret", **options)

    def test_login_negotiates_the_nonce_and_talks_over_aes(self):
        client = self.client()
        client.login()
        self.assertTrue(client.logged_in)
        audio_format = client.talk_ability()
        self.assertEqual(16000, audio_format.sample_rate)
        client.talk_config(audio_format)
        encoder = ImaAdpcmEncoder(audio_format.block_bytes)
        reference = tone(encoder.samples_per_block * 5)
        for index in range(5):
            client.talk_send(encoder.encode_block(
                reference[index * encoder.samples_per_block:(index + 1) * encoder.samples_per_block]
            ))
        client.talk_reset()
        client.logout()
        self.assertFalse(client.logged_in)
        decoded = [sample for block in self.camera.blocks for sample in decode_block(block)]
        self.assertEqual(5, len(self.camera.blocks))
        self.assertGreater(signal_to_noise(reference, decoded), 25.0)
        self.assertEqual([1, 1, 10, 201, 202, 202, 202, 202, 202, 11, 2], self.camera.messages)
        self.assertEqual(1, self.camera.logins)
        self.assertEqual(1, self.camera.logouts)
        self.assertIn("<sampleRate>16000</sampleRate>", self.camera.configs[0])

    def test_a_busy_talk_channel_is_reset_once_and_then_retried(self):
        self.camera.busy_count = 1
        client = self.client()
        client.login()
        client.talk_config(client.talk_ability())
        self.assertEqual(1, self.camera.resets)
        self.assertEqual([1, 1, 10, 201, 11, 201], self.camera.messages)
        client.logout()

    def test_a_channel_that_stays_busy_is_reported_as_busy(self):
        self.camera.busy_count = 5
        client = self.client()
        client.login()
        with self.assertRaises(baichuan.TalkBusy):
            client.talk_config(client.talk_ability())
        client.logout()

    def test_wrong_credentials_and_an_absent_camera_are_distinguished(self):
        wrong = baichuan.BaichuanClient("127.0.0.1", "gate", "wrong", port=self.camera.port,
                                        timeout=3.0)
        with self.assertRaises(baichuan.BaichuanAuthError):
            wrong.login()
        self.assertFalse(wrong.logged_in)
        with socket.socket() as closed:
            closed.bind(("127.0.0.1", 0))
            port = closed.getsockname()[1]
        absent = baichuan.BaichuanClient("127.0.0.1", "gate", "s3cret", port=port, timeout=0.5)
        with self.assertRaises(baichuan.BaichuanUnreachable):
            absent.login()

    def test_audio_before_config_and_commands_before_login_are_refused_locally(self):
        client = self.client()
        with self.assertRaises(baichuan.BaichuanError):
            client.talk_ability()
        client.login()
        with self.assertRaises(baichuan.BaichuanError):
            client.talk_send(bytes(516))
        self.assertEqual([1, 1], self.camera.messages)
        client.logout()

    def test_the_client_never_exposes_a_camera_payload_in_an_error(self):
        self.camera.busy_count = 5
        client = self.client()
        client.login()
        try:
            client.talk_config(client.talk_ability())
        except baichuan.BaichuanError as error:
            text = str(error)
        self.assertNotIn("<", text)
        self.assertNotIn("s3cret", text)
        self.assertNotIn(self.camera.nonce, text)
        client.logout()

    def test_the_message_set_is_closed(self):
        source = (REPOSITORY_ROOT / "gate_camera_control/baichuan.py").read_text(encoding="utf-8")
        sent = set(int(value) for value in re.findall(r"build_header\(\s*MSG_(\w+)", source)
                   if False)
        names = set(re.findall(r"build_header\(\s*(MSG_[A-Z_]+)", source))
        self.assertEqual(
            {"MSG_LOGIN", "MSG_LOGOUT", "MSG_TALK"}, names,
        )
        self.assertEqual(
            {"MSG_TALK_ABILITY", "MSG_TALK_CONFIG", "MSG_TALK_RESET"},
            set(re.findall(r"self\._request\((MSG_[A-Z_]+)", source)),
        )
        self.assertNotIn("def send(", source)
        del sent


class TalkControllerTests(unittest.TestCase):
    def setUp(self):
        self.camera = FakeBaichuanCamera("gate", "s3cret").start()
        self.addCleanup(self.camera.stop)
        self.journal = []

    def client_factory(self, password="s3cret"):
        return lambda: baichuan.BaichuanClient(
            "127.0.0.1", "gate", password, port=self.camera.port, timeout=3.0,
        )

    def controller(self, *, ffmpeg_seconds=1.0, gateway=None, **keywords):
        options = {
            "enabled": True,
            "max_seconds": 30,
            "ffmpeg_binary": sys.executable,
            "publisher_wait": 2.0,
            "poll_interval": 0.05,
            "spawn": fake_spawn(ffmpeg_seconds),
            "opener": gateway or FakeGateway(),
            "journal": lambda stage, **fields: self.journal.append((stage, fields)),
        }
        options.update(keywords)
        return TalkController(self.client_factory(), **options)

    def wait_until_ended(self, controller, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = controller.snapshot()
            if not snapshot["active"]:
                return snapshot
            time.sleep(0.05)
        self.fail("talk session did not end in time")

    def test_the_probe_logs_in_asks_the_ability_and_sends_no_audio(self):
        controller = self.controller()
        self.assertEqual("not_probed", controller.snapshot()["reason"])
        self.assertTrue(controller.probe())
        self.assertTrue(controller.available())
        self.assertEqual([1, 1, 10, 2], self.camera.messages)
        self.assertEqual([], self.camera.blocks)
        self.assertEqual(("talk_probe", {"outcome": "ready"}), self.journal[-1])

    def test_probe_failures_name_their_cause_and_are_not_ready(self):
        cases = (
            (self.controller(ffmpeg_binary="/nonexistent/ffmpeg"), "ffmpeg_missing"),
            (TalkController(self.client_factory("wrong"), ffmpeg_binary=sys.executable,
                            opener=FakeGateway()), "camera_auth"),
        )
        self.camera.sample_rate = 44100
        cases += ((self.controller(), "unsupported"),)
        for controller, reason in cases:
            with self.subTest(reason=reason):
                self.assertFalse(controller.probe())
                self.assertEqual(reason, controller.snapshot()["reason"])
                with self.assertRaises(TalkUnavailable) as raised:
                    controller.arm()
                self.assertEqual(reason, raised.exception.reason)
        port = self.camera.port
        self.camera.stop()
        unreachable = TalkController(
            lambda: baichuan.BaichuanClient("127.0.0.1", "gate", "s3cret", port=port, timeout=0.5),
            ffmpeg_binary=sys.executable, opener=FakeGateway(),
        )
        self.assertFalse(unreachable.probe())
        self.assertEqual("camera_unreachable", unreachable.snapshot()["reason"])

    def test_disabled_talk_never_touches_the_camera(self):
        controller = self.controller(enabled=False)
        self.assertFalse(controller.probe())
        self.assertFalse(controller.refresh_if_due())
        self.assertEqual("not_enabled", controller.state_block()["reason"])
        with self.assertRaises(TalkUnavailable):
            controller.arm()
        self.assertEqual([], self.camera.messages)

    def test_a_session_forwards_the_publisher_to_the_camera_then_cleans_up(self):
        gateway = FakeGateway(ready_after=3)
        controller = self.controller(ffmpeg_seconds=1.0, gateway=gateway)
        controller.probe()
        armed = controller.arm()
        self.assertTrue(armed["active"])
        self.assertEqual("armed", armed["state"])
        self.assertEqual(30, armed["max_seconds"])
        with self.assertRaises(TalkBusySession):
            controller.arm()
        ended = self.wait_until_ended(controller)
        self.assertEqual("publisher_gone", ended["last_outcome"])
        self.assertEqual("ended", ended["state"])
        self.assertIsNone(ended["seconds_remaining"])
        self.assertGreaterEqual(len(self.camera.blocks), 12)  # a second is ~15.6 blocks
        decoded = [sample for block in self.camera.blocks for sample in decode_block(block)]
        self.assertGreater(signal_to_noise(tone(len(decoded)), decoded), 20.0)
        self.assertTrue(self.camera.talk_open is False)
        self.assertEqual(self.camera.logins, self.camera.logouts)
        self.assertEqual(["9c1b0e4e-talk-session"], gateway.kicks)
        self.assertGreater(gateway.polls, 3)
        stages = [stage for stage, _ in self.journal]
        self.assertEqual(
            ["talk_probe", "talk_armed", "talk_publisher", "talk_stream", "talk_ended"], stages,
        )
        self.assertEqual("publisher_gone", self.journal[-1][1]["reason"])
        # Nothing secret in the journal, and the session id is truncated.
        text = json.dumps(self.journal)
        self.assertNotIn("s3cret", text)
        self.assertNotIn(self.camera.nonce, text)
        self.assertNotIn("127.0.0.1", text)
        self.assertEqual(12, len(self.journal[1][1]["session"]))
        # And a second session can follow the first.
        controller.arm()
        self.wait_until_ended(controller)

    def test_the_hard_limit_kills_ffmpeg_and_ends_the_session(self):
        gateway = FakeGateway()
        controller = self.controller(ffmpeg_seconds=0, gateway=gateway)
        controller.probe()
        started = time.monotonic()
        controller.arm(MIN_SECONDS)
        ended = self.wait_until_ended(controller, timeout=MIN_SECONDS + 10)
        elapsed = time.monotonic() - started
        self.assertEqual("time_limit", ended["last_outcome"])
        self.assertLess(elapsed, MIN_SECONDS + 5)
        self.assertGreaterEqual(elapsed, MIN_SECONDS - 0.5)
        self.assertGreaterEqual(len(self.camera.blocks), 40)
        self.assertEqual(1, len(gateway.kicks))
        self.assertGreaterEqual(self.camera.resets, 1)

    def test_a_release_ends_a_waiting_or_streaming_session(self):
        waiting = self.controller(gateway=FakeGateway(ready_after=10_000), publisher_wait=30)
        waiting.probe()
        waiting.arm()
        time.sleep(0.2)
        self.assertEqual("waiting_for_publisher", waiting.snapshot()["state"])
        waiting.release()
        self.assertEqual("released", self.wait_until_ended(waiting)["last_outcome"])
        self.assertEqual([], self.camera.blocks)

        streaming = self.controller(ffmpeg_seconds=0)
        streaming.probe()
        streaming.arm()
        deadline = time.monotonic() + 5
        while streaming.snapshot()["state"] != "streaming" and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        streaming.release()
        self.assertEqual("released", self.wait_until_ended(streaming)["last_outcome"])
        self.assertGreater(len(self.camera.blocks), 0)

    def test_no_publisher_ends_the_session_without_opening_the_talk_channel(self):
        controller = self.controller(gateway=FakeGateway(publisher=None), publisher_wait=0.3)
        controller.probe()
        controller.arm()
        ended = self.wait_until_ended(controller)
        self.assertEqual("no_publisher", ended["last_outcome"])
        self.assertEqual([], self.camera.configs)

    def test_a_camera_that_keeps_the_channel_busy_ends_the_session_honestly(self):
        controller = self.controller()
        controller.probe()
        self.camera.busy_count = 10
        controller.arm()
        ended = self.wait_until_ended(controller)
        self.assertEqual("talk_busy", ended["last_outcome"])
        self.assertTrue(controller.available())

    def test_bounds_on_the_requested_length(self):
        controller = self.controller(max_seconds=20)
        controller.probe()
        for seconds in (MIN_SECONDS - 1, 21, HARD_MAX_SECONDS + 1, 0):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                controller.arm(seconds)
        self.assertEqual(20, controller.max_seconds)
        self.assertEqual(60, TalkController(self.client_factory(), max_seconds=999).max_seconds)

    def test_the_probe_runs_on_a_slow_clock(self):
        clock = ManualClock()
        controller = self.controller(clock=clock)
        self.assertTrue(controller.refresh_if_due())
        self.assertFalse(controller.refresh_if_due())
        clock.advance(60 * 60 + 1)
        self.assertTrue(controller.refresh_if_due())
        self.assertEqual(2, self.camera.logins)
        self.camera.sample_rate = 44100
        clock.advance(60 * 60 + 1)
        self.assertFalse(controller.refresh_if_due())
        clock.advance(15 * 60 - 1)
        self.assertFalse(controller.refresh_if_due())
        clock.advance(2)
        self.camera.sample_rate = 16000
        self.assertTrue(controller.refresh_if_due())

    def test_the_ffmpeg_command_is_fixed_loopback_and_credential_free(self):
        command = ffmpeg_command("/usr/bin/ffmpeg", "rtsp://127.0.0.1:8554/talk", 16000)
        self.assertEqual("/usr/bin/ffmpeg", command[0])
        self.assertIn("rtsp://127.0.0.1:8554/talk", command)
        self.assertEqual("pipe:1", command[-1])
        self.assertEqual(["-ar", "16000"], command[command.index("-ar"):command.index("-ar") + 2])
        self.assertEqual(["-f", "s16le"], command[command.index("-f"):command.index("-f") + 2])
        self.assertIn("-nostdin", command)
        self.assertNotIn("@", " ".join(command))


class TalkHttpTests(unittest.TestCase):
    def setUp(self):
        self.api_camera = FakeCamera()
        self.addCleanup(self.api_camera.close)
        self.talk_camera = FakeBaichuanCamera("gate", "s3cret").start()
        self.addCleanup(self.talk_camera.stop)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.journal = RecordingJournal()
        self.gateway = FakeGateway()
        self.service = self.build(enabled=True)
        self.server = CameraControlServer(("127.0.0.1", 0), self.service, logger=self.journal)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(lambda: self.service.talk and self.service.talk.stop())
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def build(self, *, enabled):
        return build_service(
            {
                "GATE_CAMERA_HOST": self.api_camera.host,
                "GATE_CAMERA_USERNAME": "gate",
                "GATE_CAMERA_PASSWORD": "s3cret",
                "GATE_CAMERA_IR_DEFAULT": "Off",
                "GATE_CAMERA_IR_LEASE_DEFAULT_MINUTES": "10",
                "GATE_CAMERA_IR_LEASE_MAX_MINUTES": "60",
                "GATE_CAMERA_TALK_ENABLED": "true" if enabled else "false",
                "GATE_CAMERA_TALK_MAX_SECONDS": "20",
            },
            token_path=Path(self.directory.name) / "token.json",
            lease_path=Path(self.directory.name) / "lease.json",
            logger=self.journal,
            connection_factory=self.api_camera.connection_factory,
            talk_client_factory=lambda: baichuan.BaichuanClient(
                "127.0.0.1", "gate", "s3cret", port=self.talk_camera.port, timeout=3.0,
            ),
            talk_options={
                "ffmpeg_binary": sys.executable, "publisher_wait": 0.5, "poll_interval": 0.05,
                "spawn": fake_spawn(0.5), "opener": self.gateway,
            },
        )

    def request(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        request = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method,
            headers={} if data is None else {"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def wait_until_idle(self):
        # Polled through the facade rather than HTTP: the read budget is ten
        # then two a second, and a test loop must not spend it.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not self.service.talk.snapshot()["active"]:
                status, body = self.request("GET", "/camera/talk")
                self.assertEqual(200, status)
                return body
            time.sleep(0.05)
        self.fail("talk session did not end")

    def test_talk_is_armed_reported_and_released_over_loopback_http(self):
        status, body = self.request("GET", "/camera/talk")
        self.assertEqual(200, status)
        self.assertEqual({"observed_at", "talk"}, set(body))
        self.assertEqual("not_probed", body["talk"]["reason"])
        self.assertEqual(20, body["talk"]["max_seconds"])

        status, body = self.request("POST", "/camera/talk", {"max_seconds": 10})
        self.assertEqual(503, status)
        self.assertEqual({"error": "talk_unavailable", "reason": "not_probed"}, body)

        self.service.talk.probe()
        status, body = self.request("POST", "/camera/talk", {"max_seconds": 10})
        self.assertEqual(200, status, body)
        self.assertEqual("armed", body["status"])
        self.assertTrue(body["talk"]["active"])
        self.assertEqual(32, len(body["talk"]["session_id"]))
        self.assertLessEqual(body["talk"]["seconds_remaining"], 10)
        status, body = self.request("POST", "/camera/talk")
        self.assertEqual(409, status)
        self.assertEqual({"error": "talk_busy"}, body)

        status, body = self.request("DELETE", "/camera/talk")
        self.assertEqual(200, status)
        self.assertEqual("released", body["status"])
        ended = self.wait_until_idle()["talk"]
        self.assertIn(ended["last_outcome"], {"released", "publisher_gone"})

        # An empty body arms with the configured maximum.
        status, body = self.request("POST", "/camera/talk", raw=b"")
        self.assertEqual(200, status, body)
        self.assertLessEqual(body["talk"]["seconds_remaining"], 20)
        self.wait_until_idle()
        self.assertGreater(len(self.talk_camera.blocks), 0)

    def test_bad_talk_requests_are_refused_before_the_camera(self):
        self.service.talk.probe()
        for payload in ({"max_seconds": 999}, {"max_seconds": "10"}, {"max_seconds": True},
                        {"seconds": 10}, [10]):
            with self.subTest(payload=payload):
                status, body = self.request("POST", "/camera/talk", payload)
                self.assertEqual(400, status)
                self.assertEqual({"error": "invalid_request"}, body)
        self.assertEqual([1, 1, 10, 2], self.talk_camera.messages)  # the probe only
        status, body = self.request("PUT", "/camera/talk")
        self.assertEqual(405, status)
        status, body = self.request("DELETE", "/camera/ir")
        self.assertEqual(405, status)
        status, body = self.request("GET", "/camera/talk?x=1")
        self.assertEqual(404, status)

    def test_talk_disabled_answers_503_not_enabled_and_never_dials_the_camera(self):
        service = self.build(enabled=False)
        self.assertIsNone(service.talk)
        self.assertEqual("not_enabled", service.talk_state()["talk"]["reason"])
        with self.assertRaises(TalkUnavailable):
            service.arm_talk(None)
        self.assertEqual("released", service.release_talk()["status"])
        self.assertEqual([], self.talk_camera.messages)

    def test_the_state_file_carries_the_talkback_block_the_controller_reads(self):
        path = Path(self.directory.name) / "state.json"
        talk = self.service.talk
        publisher = StatePublisher(
            path, self.service.controller.snapshot,
            talk_provider=talk.state_block, talk_refresher=talk.refresh_if_due,
        )
        publisher.publish_once()
        block = read_camera_control_state(path)["talkback"]
        self.assertEqual({"available": False, "reason": "not_probed", "active": False}, block)
        talk.probe()
        publisher.publish_once()
        self.assertEqual(
            {"available": True, "reason": "ready", "active": False},
            read_camera_control_state(path)["talkback"],
        )
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("s3cret", text)
        self.assertNotIn("127.0.0.1", text)
        self.assertNotIn("nonce", text)


class TalkDeploymentTests(unittest.TestCase):
    def read_unit(self, relative_path):
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str
        parser.read(REPOSITORY_ROOT / relative_path, encoding="utf-8")
        return parser["Service"]

    def test_the_camera_control_unit_has_room_for_ffmpeg_and_reaches_only_the_camera(self):
        service = self.read_unit("deployment/systemd/gate-camera-control.service")
        self.assertEqual("128M", service.get("MemoryMax"))
        self.assertEqual("any", service.get("IPAddressDeny"))
        self.assertEqual("localhost", service.get("IPAddressAllow"))
        self.assertNotIn("ffmpeg", (REPOSITORY_ROOT / "deployment/systemd/gate-camera-control.service")
                         .read_text(encoding="utf-8").split("[Service]")[0])

    def test_the_installer_publishes_every_talk_module(self):
        installer = (REPOSITORY_ROOT / "deployment/install-camera-control.sh").read_text(
            encoding="utf-8"
        )
        match = re.search(r"for module in ([^;]+); do", installer)
        self.assertIsNotNone(match)
        published = set(match.group(1).split())
        on_disk = {path.stem for path in (REPOSITORY_ROOT / "gate_camera_control").glob("*.py")}
        self.assertEqual(on_disk, published)

    def test_the_media_gateway_carries_a_publisher_only_talk_path(self):
        config = (REPOSITORY_ROOT / "deployment/media/mediamtx.yml").read_text(encoding="utf-8")
        self.assertIn("  talk:\n    source: publisher\n    overridePublisher: false\n", config)

    def test_the_environment_template_and_docs_name_the_talk_settings(self):
        example = (REPOSITORY_ROOT / "deployment/gate-camera-control.env.example").read_text(
            encoding="utf-8"
        )
        self.assertIn("GATE_CAMERA_TALK_ENABLED=false", example)
        self.assertIn("GATE_CAMERA_TALK_MAX_SECONDS=30", example)
        talkback = (REPOSITORY_ROOT / "docs/talkback.md").read_text(encoding="utf-8")
        for needle in ("GATE_MEDIA_TALKBACK_VERIFIED", "GATE_CAMERA_TALK_ENABLED",
                       "/camera/talk", "/talk/whip", "9000", "acceptance"):
            self.assertIn(needle, talkback)
        deployment = (REPOSITORY_ROOT / "docs/deployment.md").read_text(encoding="utf-8")
        self.assertIn("GATE_MEDIA_TALKBACK_VERIFIED", deployment)
        camera_control = (REPOSITORY_ROOT / "docs/camera-control.md").read_text(encoding="utf-8")
        self.assertIn("/camera/talk", camera_control)

    def test_the_isolated_packages_still_import_nothing_from_the_controller(self):
        for package in ("gate_camera_control", "gate_media_auth"):
            for source in (REPOSITORY_ROOT / package).glob("*.py"):
                text = source.read_text(encoding="utf-8")
                with self.subTest(source=source.name):
                    self.assertNotIn("gate_controller", text)
                    self.assertNotIn("import requests", text)
                    self.assertNotIn("cryptography", text)


if __name__ == "__main__":
    unittest.main()
