"""A minimal Baichuan (TCP 9000) client that can do exactly one thing: talk.

Reolink cameras take talk-back audio only over their proprietary Baichuan
protocol; there is no RTSP backchannel and no ``api.cgi`` command for it.
``reolink_aio`` 0.21 implements the transport, the login and the encryption
but not the talk messages, and it pulls in aiohttp, orjson and pycryptodomex;
neolink implements talk in Rust. This module is the intersection those two
agree on, written against ``socket`` and ``hashlib`` alone:

- login: a header-only legacy ``Login`` (class ``0x6514``, encryption request
  ``0xdc12``) that returns a nonce, then a modern ``Login`` (class ``0x6414``)
  carrying MD5 hashes of user and password concatenated with that nonce;
- after login, XML is AES-128-CFB encrypted with the key
  ``MD5(nonce + "-" + password)[:16]`` and the fixed IV ``0123456789abcdef``;
- ``TalkAbility`` (10) reports the audio format, ``TalkConfig`` (201) opens the
  channel, ``Talk`` (202) carries ``BcMedia`` ADPCM frames as an unencrypted
  binary payload, ``TalkReset`` (11) closes it, and ``Logout`` (2) ends the
  session.

The message set is closed: nothing here can send an arbitrary command, and no
method returns or logs a camera payload -- callers see typed results or a
typed error with no body attached.
"""

import hashlib
import re
import select
import socket
import time
from xml.etree import ElementTree

from .aes import Aes128


DEFAULT_PORT = 9000
DEFAULT_TIMEOUT_SECONDS = 5.0
MAGIC = b"\xf0\xde\xbc\x0a"
MAGIC_REVERSED = b"\x0a\xbc\xde\xf0"
MSG_LOGIN = 1
MSG_LOGOUT = 2
MSG_TALK_ABILITY = 10
MSG_TALK_RESET = 11
MSG_TALK_CONFIG = 201
MSG_TALK = 202
CLASS_LEGACY = 0x6514
CLASS_MODERN_SHORT = 0x6614
CLASS_MODERN = 0x6414
ENCRYPTION_REQUEST_AES = 0xDC12
XML_KEY = (0x1F, 0x2D, 0x3C, 0x4B, 0x5A, 0x69, 0x78, 0xFF)
AES_IV = b"0123456789abcdef"
MAX_BODY_BYTES = 256 * 1024
BCMEDIA_ADPCM_MAGIC = 0x62773130
BCMEDIA_ADPCM_DATA_MAGIC = 0x0100
BCMEDIA_PAD = 8
TALK_BUSY_STATUS = 422
_TOKEN = re.compile(r"^[A-Za-z0-9_]{1,32}$")
_XML_HEADER = b"<?xml"


class BaichuanError(Exception):
    """Any camera failure on the Baichuan channel; carries no camera payload."""


class BaichuanUnreachable(BaichuanError):
    """The camera did not answer on port 9000."""


class BaichuanAuthError(BaichuanError):
    """The camera refused the configured credentials."""


class BaichuanRefused(BaichuanError):
    """The camera answered a command with a non-200 status."""

    def __init__(self, msg_id: int, status: int):
        super().__init__(f"camera refused message {msg_id} with status {status}")
        self.msg_id = msg_id
        self.status = status


class TalkBusy(BaichuanRefused):
    """Another client holds the talk channel (422), even after one reset."""


class TalkUnsupported(BaichuanError):
    """The camera offers no ADPCM talk format this client can produce."""


class TalkAudioFormat:
    """The one talk format negotiated from ``TalkAbility``."""

    __slots__ = ("duplex", "audio_stream_mode", "sample_rate", "sample_precision",
                 "length_per_encoder", "sound_track")

    def __init__(self, *, duplex, audio_stream_mode, sample_rate, sample_precision,
                 length_per_encoder, sound_track):
        self.duplex = duplex
        self.audio_stream_mode = audio_stream_mode
        self.sample_rate = sample_rate
        self.sample_precision = sample_precision
        self.length_per_encoder = length_per_encoder
        self.sound_track = sound_track

    @property
    def block_bytes(self) -> int:
        """One DVI ADPCM block: four header bytes plus a nibble per sample."""
        return self.length_per_encoder // 2 + 4

    def describe(self) -> str:
        return f"adpcm{self.sample_rate}x{self.length_per_encoder}"


def bc_xor(data: bytes, offset: int) -> bytes:
    """The legacy "BC" cipher: a fixed 8-byte XOR key, rotated by the channel byte."""
    offset &= 0xFF
    return bytes(byte ^ XML_KEY[(offset + index) % 8] ^ offset for index, byte in enumerate(data))


def md5_modern(text: str) -> str:
    """The protocol's MD5 spelling: upper-case hex, last character dropped."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:31].upper()


def aes_key(nonce: str, password: str) -> bytes:
    return md5_modern(f"{nonce}-{password}")[:16].encode("utf-8")


def build_header(msg_id: int, body_len: int, channel: int, msg_num: int, *,
                 response_code: int = 0, message_class: int = CLASS_MODERN,
                 payload_offset=None) -> bytes:
    header = (MAGIC + msg_id.to_bytes(4, "little") + body_len.to_bytes(4, "little")
              + bytes((channel & 0xFF, 0)) + (msg_num & 0xFFFF).to_bytes(2, "little")
              + (response_code & 0xFFFF).to_bytes(2, "little")
              + (message_class & 0xFFFF).to_bytes(2, "little"))
    if has_payload_offset(message_class):
        header += (payload_offset or 0).to_bytes(4, "little")
    return header


def has_payload_offset(message_class: int) -> bool:
    return message_class in (CLASS_MODERN, 0x0000)


def bcmedia_adpcm(block: bytes) -> bytes:
    """Wrap one ADPCM block in the ``BcMedia`` frame the camera plays."""
    block = bytes(block)
    if len(block) <= 4:
        raise ValueError("ADPCM block must carry a header and data")
    padding = (-len(block)) % BCMEDIA_PAD
    return (BCMEDIA_ADPCM_MAGIC.to_bytes(4, "little")
            + (len(block) + 4).to_bytes(2, "little")
            + (len(block) + 4).to_bytes(2, "little")
            + BCMEDIA_ADPCM_DATA_MAGIC.to_bytes(2, "little")
            + ((len(block) - 4) // 2).to_bytes(2, "little")
            + block + bytes(padding))


def parse_talk_ability(xml_text: str) -> TalkAudioFormat:
    """Pick the camera's ADPCM configuration, bounded and ASCII-checked."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as error:
        raise BaichuanError("camera sent an unreadable TalkAbility") from error
    ability = root.find(".//TalkAbility")
    if ability is None:
        raise TalkUnsupported("camera reported no TalkAbility")
    duplex = _text(ability.find("./duplexList/duplex")) or "FDX"
    mode = _text(ability.find("./audioStreamModeList/audioStreamMode")) or "followVideoStream"
    if not _TOKEN.fullmatch(duplex) or not _TOKEN.fullmatch(mode):
        raise TalkUnsupported("camera talk mode is not a plain token")
    for config in ability.findall("./audioConfigList/audioConfig"):
        if _text(config.find("./audioType")) != "adpcm":
            continue
        sample_rate = _int(config.find("./sampleRate"))
        precision = _int(config.find("./samplePrecision"))
        length = _int(config.find("./lengthPerEncoder"))
        track = _text(config.find("./soundTrack")) or "mono"
        if (sample_rate not in (8000, 16000) or precision != 16 or track != "mono"
                or length is None or length % 2 or not 64 <= length <= 8192):
            continue
        return TalkAudioFormat(duplex=duplex, audio_stream_mode=mode,
                               sample_rate=sample_rate, sample_precision=precision,
                               length_per_encoder=length, sound_track=track)
    raise TalkUnsupported("camera offers no 16-bit mono ADPCM talk format")


def talk_config_xml(audio_format: TalkAudioFormat, channel: int) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" ?>\n'
        "<body>\n"
        '<TalkConfig version="1.1">\n'
        f"<channelId>{int(channel)}</channelId>\n"
        f"<duplex>{audio_format.duplex}</duplex>\n"
        f"<audioStreamMode>{audio_format.audio_stream_mode}</audioStreamMode>\n"
        "<audioConfig>\n"
        "<audioType>adpcm</audioType>\n"
        f"<sampleRate>{int(audio_format.sample_rate)}</sampleRate>\n"
        f"<samplePrecision>{int(audio_format.sample_precision)}</samplePrecision>\n"
        f"<lengthPerEncoder>{int(audio_format.length_per_encoder)}</lengthPerEncoder>\n"
        f"<soundTrack>{audio_format.sound_track}</soundTrack>\n"
        "</audioConfig>\n"
        "</TalkConfig>\n"
        "</body>\n"
    )


def extension_xml(channel: int, *, binary: bool = False) -> str:
    binary_line = "<binaryData>1</binaryData>\n" if binary else ""
    return (
        '<?xml version="1.0" encoding="UTF-8" ?>\n'
        '<Extension version="1.1">\n'
        f"{binary_line}"
        f"<channelId>{int(channel)}</channelId>\n"
        "</Extension>\n"
    )


def login_xml(user_hash: str, password_hash: str, *, net: bool) -> str:
    net_block = (
        '<LoginNet version="1.1">\n<type>LAN</type>\n<udpPort>0</udpPort>\n</LoginNet>\n'
        if net else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" ?>\n'
        "<body>\n"
        '<LoginUser version="1.1">\n'
        f"<userName>{user_hash}</userName>\n"
        f"<password>{password_hash}</password>\n"
        "<userVer>1</userVer>\n"
        "</LoginUser>\n"
        f"{net_block}"
        "</body>\n"
    )


class _Message:
    __slots__ = ("msg_id", "channel", "msg_num", "response_code", "message_class",
                 "extension", "payload")

    def __init__(self, msg_id, channel, msg_num, response_code, message_class,
                 extension, payload):
        self.msg_id = msg_id
        self.channel = channel
        self.msg_num = msg_num
        self.response_code = response_code
        self.message_class = message_class
        self.extension = extension
        self.payload = payload


class BaichuanClient:
    """One connection, one login, one talk session; then it is closed."""

    def __init__(self, host: str, username: str, password: str, *, port: int = DEFAULT_PORT,
                 channel: int = 0, timeout: float = DEFAULT_TIMEOUT_SECONDS,
                 connect=socket.create_connection):
        if not host or not username or not password:
            raise ValueError("camera host and credentials are required")
        self._host = host
        self._port = int(port)
        self._username = username
        self._password = password
        self._channel = int(channel)
        self._timeout = float(timeout)
        self._connect = connect
        self._socket = None
        self._buffer = bytearray()
        self._msg_num = 0
        self._cipher = None
        self._logged_in = False
        self._talk_msg_num = None
        self._ack_deficit = 0

    @property
    def logged_in(self) -> bool:
        return self._logged_in

    # -- connection -------------------------------------------------------

    def login(self) -> None:
        if self._logged_in:
            return
        try:
            self._socket = self._connect((self._host, self._port), timeout=self._timeout)
            self._socket.settimeout(self._timeout)
        except OSError as error:
            raise BaichuanUnreachable("camera did not answer on the talk port") from error
        try:
            msg_num = self._next_msg_num()
            self._send_raw(build_header(
                MSG_LOGIN, 0, self._channel, msg_num,
                response_code=ENCRYPTION_REQUEST_AES, message_class=CLASS_LEGACY,
            ))
            reply = self._wait_for(MSG_LOGIN, msg_num)
            nonce = self._nonce_from(reply)
            self._cipher = Aes128(aes_key(nonce, self._password))
            body = login_xml(md5_modern(self._username + nonce),
                             md5_modern(self._password + nonce), net=True)
            msg_num = self._next_msg_num()
            self._send_raw(build_header(MSG_LOGIN, len(body.encode("utf-8")), self._channel,
                                        msg_num, payload_offset=0)
                           + bc_xor(body.encode("utf-8"), self._channel))
            reply = self._wait_for(MSG_LOGIN, msg_num)
            if reply.response_code == 401:
                raise BaichuanAuthError("camera rejected the configured credentials")
            if reply.response_code != 200:
                raise BaichuanRefused(MSG_LOGIN, reply.response_code)
            if not reply.payload:
                raise BaichuanAuthError("camera answered the login with no device info")
            self._logged_in = True
        except BaichuanError:
            self.close()
            raise
        except (OSError, ValueError) as error:
            self.close()
            raise BaichuanUnreachable("camera dropped the login") from error

    def close(self) -> None:
        connection, self._socket = self._socket, None
        self._logged_in = False
        self._cipher = None
        self._buffer.clear()
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    def logout(self) -> None:
        """Best effort: the camera keeps a session slot until it hears this."""
        if not self._logged_in or self._socket is None:
            self.close()
            return
        try:
            msg_num = self._next_msg_num()
            body = self._encrypt(login_xml(self._username, self._password, net=False)
                                 .encode("utf-8"))
            self._send_raw(build_header(MSG_LOGOUT, len(body), self._channel, msg_num,
                                        payload_offset=0) + body)
            self._wait_for(MSG_LOGOUT, msg_num, timeout=min(self._timeout, 2.0))
        except (BaichuanError, OSError, ValueError):
            pass
        finally:
            self.close()

    # -- talk -------------------------------------------------------------

    def talk_ability(self) -> TalkAudioFormat:
        self._require_login()
        reply = self._request(MSG_TALK_ABILITY, extension_xml(self._channel))
        if reply.response_code != 200:
            raise BaichuanRefused(MSG_TALK_ABILITY, reply.response_code)
        return parse_talk_ability(self._decrypt_xml(reply.payload, reply))

    def talk_config(self, audio_format: TalkAudioFormat) -> None:
        """Open the channel; one reset-and-retry if another client holds it."""
        self._require_login()
        body = talk_config_xml(audio_format, self._channel)
        reply = self._request(MSG_TALK_CONFIG, extension_xml(self._channel), body)
        if reply.response_code == TALK_BUSY_STATUS:
            self.talk_reset()
            reply = self._request(MSG_TALK_CONFIG, extension_xml(self._channel), body)
            if reply.response_code == TALK_BUSY_STATUS:
                raise TalkBusy(MSG_TALK_CONFIG, reply.response_code)
        if reply.response_code != 200:
            raise BaichuanRefused(MSG_TALK_CONFIG, reply.response_code)
        self._talk_msg_num = self._next_msg_num()
        self._ack_deficit = 0

    def talk_send(self, block: bytes, *, ack_timeout: float = 1.0) -> None:
        """Send one ADPCM block as one ``Talk`` message and take its acknowledgement."""
        self._require_login()
        if self._talk_msg_num is None:
            raise BaichuanError("talk_config must succeed before audio is sent")
        extension = self._encrypt(extension_xml(self._channel, binary=True).encode("utf-8"))
        payload = bcmedia_adpcm(block)
        self._send_raw(build_header(MSG_TALK, len(extension) + len(payload), self._channel,
                                    self._talk_msg_num, payload_offset=len(extension))
                       + extension + payload)
        reply = self._receive(timeout=ack_timeout, want=(MSG_TALK, self._talk_msg_num))
        if reply is None:
            self._ack_deficit += 1
            if self._ack_deficit > 32:
                raise BaichuanError("camera stopped acknowledging talk audio")
            return
        self._ack_deficit = 0
        if reply.response_code != 200:
            raise BaichuanRefused(MSG_TALK, reply.response_code)

    def talk_reset(self) -> None:
        self._require_login()
        reply = self._request(MSG_TALK_RESET, extension_xml(self._channel))
        self._talk_msg_num = None
        if reply.response_code != 200:
            raise BaichuanRefused(MSG_TALK_RESET, reply.response_code)

    # -- framing ----------------------------------------------------------

    def _require_login(self) -> None:
        if not self._logged_in or self._socket is None:
            raise BaichuanError("not logged in")

    def _next_msg_num(self) -> int:
        self._msg_num = (self._msg_num + 1) & 0xFFFF or 1
        return self._msg_num

    def _request(self, msg_id: int, extension: str, body: str = "") -> _Message:
        msg_num = self._next_msg_num()
        extension_bytes = self._encrypt(extension.encode("utf-8"))
        body_bytes = self._encrypt(body.encode("utf-8")) if body else b""
        self._send_raw(build_header(msg_id, len(extension_bytes) + len(body_bytes),
                                    self._channel, msg_num, payload_offset=len(extension_bytes))
                       + extension_bytes + body_bytes)
        return self._wait_for(msg_id, msg_num)

    def _encrypt(self, data: bytes) -> bytes:
        if not data:
            return b""
        if self._cipher is None:
            return bc_xor(data, self._channel)
        return self._cipher.cfb_encrypt(AES_IV, data)

    def _decrypt_xml(self, data: bytes, message: _Message) -> str:
        """Decrypt an XML body, trying the negotiated cipher first and BC second."""
        if not data:
            return ""
        candidates = []
        if message.msg_id == MSG_LOGIN and (message.response_code >> 8) == 0xDD:
            candidates.append(data if message.response_code & 0xFF == 0
                              else bc_xor(data, message.channel))
        elif message.msg_id == MSG_LOGIN or self._cipher is None:
            candidates.append(bc_xor(data, message.channel))
        else:
            candidates.append(self._cipher.cfb_decrypt(AES_IV, data))
            candidates.append(bc_xor(data, message.channel))
        for candidate in candidates:
            if candidate.startswith(_XML_HEADER):
                try:
                    return candidate.decode("utf-8")
                except UnicodeDecodeError:
                    continue
        raise BaichuanError("camera reply could not be decrypted")

    def _nonce_from(self, reply: _Message) -> str:
        text = self._decrypt_xml(reply.payload or reply.extension, reply)
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError as error:
            raise BaichuanError("camera sent an unreadable encryption reply") from error
        nonce = _text(root.find(".//nonce"))
        if not nonce or len(nonce) > 64 or not nonce.isascii() or not nonce.isprintable():
            raise BaichuanError("camera sent no usable login nonce")
        return nonce

    def _send_raw(self, data: bytes) -> None:
        if self._socket is None:
            raise BaichuanError("not connected")
        try:
            self._socket.sendall(data)
        except OSError as error:
            raise BaichuanUnreachable("camera connection was lost") from error

    def _wait_for(self, msg_id: int, msg_num: int, *, timeout=None) -> _Message:
        deadline = time.monotonic() + (self._timeout if timeout is None else timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BaichuanUnreachable("camera did not answer in time")
            message = self._receive(timeout=remaining, want=(msg_id, msg_num))
            if message is not None:
                return message

    def _receive(self, *, timeout: float, want) -> "_Message | None":
        """Return the next message matching ``want``; unrelated ones are dropped."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            message = self._parse_buffered()
            if message is not None:
                if (message.msg_id, message.msg_num) == want:
                    return message
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if self._socket is None:
                raise BaichuanUnreachable("camera connection is closed")
            try:
                readable, _, _ = select.select([self._socket], [], [], remaining)
                if not readable:
                    return None
                chunk = self._socket.recv(65536)
            except OSError as error:
                raise BaichuanUnreachable("camera connection was lost") from error
            if not chunk:
                raise BaichuanUnreachable("camera closed the connection")
            self._buffer += chunk
            if len(self._buffer) > MAX_BODY_BYTES + 24:
                raise BaichuanError("camera sent more than this client will buffer")

    def _parse_buffered(self):
        if len(self._buffer) < 20:
            return None
        if self._buffer[:4] not in (MAGIC, MAGIC_REVERSED):
            raise BaichuanError("camera stream lost framing")
        msg_id = int.from_bytes(self._buffer[4:8], "little")
        body_len = int.from_bytes(self._buffer[8:12], "little")
        channel = self._buffer[12]
        msg_num = int.from_bytes(self._buffer[14:16], "little")
        response_code = int.from_bytes(self._buffer[16:18], "little")
        message_class = int.from_bytes(self._buffer[18:20], "little")
        header_len = 24 if has_payload_offset(message_class) else 20
        if body_len > MAX_BODY_BYTES:
            raise BaichuanError("camera message is larger than this client accepts")
        if len(self._buffer) < header_len + body_len:
            return None
        payload_offset = (int.from_bytes(self._buffer[20:24], "little")
                          if header_len == 24 else 0)
        body = bytes(self._buffer[header_len:header_len + body_len])
        del self._buffer[:header_len + body_len]
        if payload_offset > len(body):
            raise BaichuanError("camera message has an impossible payload offset")
        extension, payload = body[:payload_offset], body[payload_offset:]
        return _Message(msg_id, channel, msg_num, response_code, message_class,
                        extension, payload)


def _text(element):
    if element is None or element.text is None:
        return None
    return element.text.strip()


def _int(element):
    text = _text(element)
    if text is None or not text.isdigit() or len(text) > 6:
        return None
    return int(text)
