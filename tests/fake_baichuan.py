"""A stand-in Reolink camera on the Baichuan port, for tests and the Mac proof.

It speaks exactly the subset ``gate_camera_control.baichuan`` speaks -- nonce
handshake, modern login, AES-CFB after login, TalkAbility, TalkConfig, Talk,
TalkReset, Logout -- and records what it was sent so a test can decode the
audio the "camera" would have played.
"""

import hashlib
import socket
import threading
from xml.etree import ElementTree

from gate_camera_control.aes import Aes128
from gate_camera_control.baichuan import (
    AES_IV, BCMEDIA_ADPCM_MAGIC, CLASS_LEGACY, CLASS_MODERN, CLASS_MODERN_SHORT,
    MSG_LOGIN, MSG_LOGOUT, MSG_TALK, MSG_TALK_ABILITY, MSG_TALK_CONFIG, MSG_TALK_RESET,
    aes_key, bc_xor, build_header, has_payload_offset, md5_modern,
)


ABILITY_XML = (
    '<?xml version="1.0" encoding="UTF-8" ?>\n<body>\n<TalkAbility version="1.1">\n'
    "<duplexList><duplex>FDX</duplex></duplexList>\n"
    "<audioStreamModeList><audioStreamMode>followVideoStream</audioStreamMode>"
    "</audioStreamModeList>\n"
    "<audioConfigList><audioConfig><priority>0</priority><audioType>adpcm</audioType>"
    "<sampleRate>{rate}</sampleRate><samplePrecision>16</samplePrecision>"
    "<lengthPerEncoder>{length}</lengthPerEncoder><soundTrack>mono</soundTrack>"
    "</audioConfig></audioConfigList>\n</TalkAbility>\n</body>\n"
)
DEVICE_INFO_XML = (
    '<?xml version="1.0" encoding="UTF-8" ?>\n<body>\n<DeviceInfo version="1.1">\n'
    "<type>ipc</type><channelNum>1</channelNum>\n</DeviceInfo>\n</body>\n"
)


class FakeBaichuanCamera:
    """One-connection-at-a-time fake; ``frames`` collects the ADPCM blocks sent."""

    def __init__(self, username="admin", password="secret", *, sample_rate=16000,
                 length_per_encoder=1024, busy_count=0, reject_login=False,
                 nonce="abcdef0123456789"):
        self.username = username
        self.password = password
        self.sample_rate = sample_rate
        self.length_per_encoder = length_per_encoder
        self.busy_count = busy_count
        self.reject_login = reject_login
        self.nonce = nonce
        self.frames = []
        self.blocks = []
        self.messages = []
        self.logins = 0
        self.logouts = 0
        self.resets = 0
        self.configs = []
        self.talk_open = False
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="fake-baichuan", daemon=True)

    @property
    def port(self) -> int:
        return self._listener.getsockname()[1]

    def start(self):
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stopped.set()
        try:
            self._listener.close()
        except OSError:
            pass
        self._thread.join(timeout=5)

    def _serve(self) -> None:
        while not self._stopped.is_set():
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            try:
                self._handle(connection)
            except OSError:
                pass
            finally:
                try:
                    connection.close()
                except OSError:
                    pass

    def _handle(self, connection) -> None:
        connection.settimeout(5)
        buffer = bytearray()
        cipher = None
        logged_in = False
        while not self._stopped.is_set():
            message = self._read(connection, buffer)
            if message is None:
                return
            msg_id, channel, msg_num, response_code, message_class, offset, body = message
            self.messages.append(msg_id)
            if msg_id == MSG_LOGIN and message_class == CLASS_LEGACY:
                reply = (
                    '<?xml version="1.0" encoding="UTF-8" ?>\n<body>\n'
                    '<Encryption version="1.1">\n<type>aes</type>\n'
                    f"<nonce>{self.nonce}</nonce>\n</Encryption>\n</body>\n"
                ).encode("utf-8")
                encrypted = bc_xor(reply, channel)
                connection.sendall(build_header(
                    MSG_LOGIN, len(encrypted), channel, msg_num, response_code=0xDD01,
                    message_class=CLASS_MODERN_SHORT,
                ) + encrypted)
                continue
            if msg_id == MSG_LOGIN:
                text = bc_xor(body, channel).decode("utf-8")
                root = ElementTree.fromstring(text)
                user = root.findtext(".//userName")
                password = root.findtext(".//password")
                expected_user = md5_modern(self.username + self.nonce)
                expected_password = md5_modern(self.password + self.nonce)
                self.logins += 1
                if self.reject_login or user != expected_user or password != expected_password:
                    connection.sendall(build_header(MSG_LOGIN, 0, channel, msg_num,
                                                    response_code=401, payload_offset=0))
                    continue
                cipher = Aes128(aes_key(self.nonce, self.password))
                logged_in = True
                # The login reply itself is still BC-encrypted on real cameras.
                encrypted = bc_xor(DEVICE_INFO_XML.encode("utf-8"), channel)
                connection.sendall(build_header(MSG_LOGIN, len(encrypted), channel, msg_num,
                                                response_code=200, payload_offset=0)
                                   + encrypted)
                continue
            if not logged_in:
                connection.sendall(build_header(msg_id, 0, channel, msg_num,
                                                response_code=401, payload_offset=0))
                continue
            extension = cipher.cfb_decrypt(AES_IV, body[:offset]).decode("utf-8") if offset else ""
            payload = body[offset:]
            if msg_id == MSG_TALK_ABILITY:
                xml = ABILITY_XML.format(rate=self.sample_rate, length=self.length_per_encoder)
                encrypted = cipher.cfb_encrypt(AES_IV, xml.encode("utf-8"))
                connection.sendall(build_header(MSG_TALK_ABILITY, len(encrypted), channel,
                                                msg_num, response_code=200, payload_offset=0)
                                   + encrypted)
                continue
            if msg_id == MSG_TALK_CONFIG:
                config = cipher.cfb_decrypt(AES_IV, payload).decode("utf-8")
                self.configs.append(config)
                if self.busy_count > 0:
                    self.busy_count -= 1
                    connection.sendall(build_header(MSG_TALK_CONFIG, 0, channel, msg_num,
                                                    response_code=422, payload_offset=0))
                    continue
                self.talk_open = True
                connection.sendall(build_header(MSG_TALK_CONFIG, 0, channel, msg_num,
                                                response_code=200, payload_offset=0))
                continue
            if msg_id == MSG_TALK:
                if "<binaryData>1</binaryData>" not in extension or not self.talk_open:
                    connection.sendall(build_header(MSG_TALK, 0, channel, msg_num,
                                                    response_code=400, payload_offset=0))
                    continue
                self.frames.append(payload)
                self.blocks.extend(_blocks_from_frames(payload))
                connection.sendall(build_header(MSG_TALK, 0, channel, msg_num,
                                                response_code=200, payload_offset=0))
                continue
            if msg_id == MSG_TALK_RESET:
                self.resets += 1
                self.talk_open = False
                connection.sendall(build_header(MSG_TALK_RESET, 0, channel, msg_num,
                                                response_code=200, payload_offset=0))
                continue
            if msg_id == MSG_LOGOUT:
                self.logouts += 1
                connection.sendall(build_header(MSG_LOGOUT, 0, channel, msg_num,
                                                response_code=200, payload_offset=0))
                return
            connection.sendall(build_header(msg_id, 0, channel, msg_num,
                                            response_code=400, payload_offset=0))

    @staticmethod
    def _read(connection, buffer):
        while True:
            if len(buffer) >= 20:
                message_class = int.from_bytes(buffer[18:20], "little")
                header_len = 24 if has_payload_offset(message_class) else 20
                body_len = int.from_bytes(buffer[8:12], "little")
                if len(buffer) >= header_len + body_len:
                    msg_id = int.from_bytes(buffer[4:8], "little")
                    channel = buffer[12]
                    msg_num = int.from_bytes(buffer[14:16], "little")
                    response_code = int.from_bytes(buffer[16:18], "little")
                    offset = int.from_bytes(buffer[20:24], "little") if header_len == 24 else 0
                    body = bytes(buffer[header_len:header_len + body_len])
                    del buffer[:header_len + body_len]
                    return msg_id, channel, msg_num, response_code, message_class, offset, body
            try:
                chunk = connection.recv(65536)
            except socket.timeout:
                return None
            if not chunk:
                return None
            buffer += chunk


def _blocks_from_frames(payload: bytes):
    """Unwrap ``BcMedia`` ADPCM frames back into raw blocks."""
    blocks = []
    position = 0
    while position + 12 <= len(payload):
        magic = int.from_bytes(payload[position:position + 4], "little")
        if magic != BCMEDIA_ADPCM_MAGIC:
            raise ValueError("unexpected BcMedia frame")
        size = int.from_bytes(payload[position + 4:position + 6], "little")
        block = payload[position + 12:position + 12 + size - 4]
        blocks.append(bytes(block))
        position += 12 + (size - 4) + (-(size - 4)) % 8
    return blocks


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
