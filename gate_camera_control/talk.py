"""Push-to-talk: forward one bounded talk session from the media gateway to the camera.

The browser publishes its microphone as Opus over WebRTC (WHIP) into the media
gateway's ``talk`` path. This controller, armed by the Worker for at most
``max_seconds``, pulls that path over loopback RTSP with ffmpeg, decodes it to
16-bit PCM at the camera's rate, encodes DVI ADPCM blocks, and sends them to the
camera over Baichuan. Everything about the session is bounded:

- one session at a time, armed explicitly, with a hard deadline enforced by
  killing ffmpeg -- not by a cooperative check that a stalled read could miss;
- a publisher that never arrives ends the session at ``publisher_wait``;
- the camera's talk channel is reset and the Baichuan session logged out at
  every exit, and the WHIP publisher is kicked from the gateway so the app sees
  the end rather than a silently dead channel.

No camera payload, address or credential reaches the journal or the snapshot.
"""

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

from .adpcm import ImaAdpcmEncoder, pcm16_to_samples
from .baichuan import (
    BaichuanAuthError, BaichuanError, BaichuanRefused, BaichuanUnreachable,
    TalkBusy, TalkUnsupported,
)


GATEWAY_API = "http://127.0.0.1:9997"
TALK_PATH = "talk"
TALK_RTSP_URL = f"rtsp://127.0.0.1:8554/{TALK_PATH}"
FFMPEG_BINARY = "/usr/bin/ffmpeg"
DEFAULT_MAX_SECONDS = 30
HARD_MAX_SECONDS = 60
MIN_SECONDS = 5
PUBLISHER_WAIT_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.25
PROBE_RETRY_SECONDS = 15 * 60.0
PROBE_REFRESH_SECONDS = 60 * 60.0
FFMPEG_STOP_GRACE_SECONDS = 2.0
_MAX_API_BYTES = 64 * 1024
_SESSION_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
REASONS = (
    "ready", "not_enabled", "not_probed", "ffmpeg_missing", "unsupported",
    "camera_auth", "camera_busy", "camera_unreachable", "camera_error",
)
END_REASONS = (
    "time_limit", "publisher_gone", "released", "no_publisher", "talk_busy",
    "camera_unreachable", "camera_error", "camera_auth", "unsupported", "ffmpeg_failed",
)


def ffmpeg_command(binary: str, rtsp_url: str, sample_rate: int) -> list:
    """The fixed decode pipeline: loopback RTSP in, PCM16 mono at the camera's rate out."""
    return [
        binary, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-fflags", "nobuffer", "-rtsp_transport", "tcp", "-i", rtsp_url,
        "-vn", "-map", "0:a:0", "-af", "aresample=async=1",
        "-ac", "1", "-ar", str(int(sample_rate)), "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1",
    ]


class TalkUnavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class TalkBusySession(Exception):
    """A session is already armed or streaming."""


class _Session:
    def __init__(self, session_id: str, armed_at: float, max_seconds: int):
        self.id = session_id
        self.armed_at = armed_at
        self.max_seconds = max_seconds
        self.deadline = armed_at + max_seconds
        self.state = "armed"
        self.ended_at = None
        self.outcome = None
        self.blocks = 0
        self.release = threading.Event()
        self.process = None
        self.lock = threading.Lock()


class TalkController:
    """Owns the one talk session and the camera's talk readiness."""

    def __init__(self, client_factory, *, enabled: bool = True,
                 max_seconds: int = DEFAULT_MAX_SECONDS, gateway_api: str = GATEWAY_API,
                 ffmpeg_binary: str = FFMPEG_BINARY, rtsp_url: str = TALK_RTSP_URL,
                 publisher_wait: float = PUBLISHER_WAIT_SECONDS,
                 poll_interval: float = POLL_INTERVAL_SECONDS, clock=time.time,
                 monotonic=time.monotonic, spawn=subprocess.Popen,
                 opener=urllib.request.urlopen, journal=None):
        self._client_factory = client_factory
        self._enabled = bool(enabled)
        self._max_seconds = max(MIN_SECONDS, min(HARD_MAX_SECONDS, int(max_seconds)))
        self._gateway_api = gateway_api.rstrip("/")
        self._ffmpeg = ffmpeg_binary
        self._rtsp_url = rtsp_url
        self._publisher_wait = float(publisher_wait)
        self._poll_interval = float(poll_interval)
        self._clock = clock
        self._monotonic = monotonic
        self._spawn = spawn
        self._opener = opener
        self._journal = journal or (lambda *_arguments, **_keywords: None)
        self._lock = threading.Lock()
        self._session = None
        self._thread = None
        self._last = None
        self._reason = "not_enabled" if not self._enabled else "not_probed"
        self._probed_at = None
        self._probe_ok = False
        self._format = None

    # -- readiness --------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def max_seconds(self) -> int:
        return self._max_seconds

    def available(self) -> bool:
        return self._enabled and self._reason == "ready"

    def probe(self) -> bool:
        """One login and ``TalkAbility``; the camera is never sent audio here."""
        if not self._enabled:
            return False
        with self._lock:
            if self._session is not None:
                return self._probe_ok
        self._probed_at = self._clock()
        if not os.access(self._ffmpeg, os.X_OK):
            self._set_reason("ffmpeg_missing", probe_ok=False)
            return False
        client = None
        try:
            client = self._client_factory()
            client.login()
            self._format = client.talk_ability()
        except BaichuanError as error:
            self._set_reason(_camera_reason(error), probe_ok=False)
            return False
        finally:
            if client is not None:
                client.logout()
        self._set_reason("ready", probe_ok=True)
        return True

    def refresh_if_due(self) -> bool:
        """Probe on a slow clock: quickly after a failure, rarely once it is ready."""
        if not self._enabled:
            return False
        if self._probed_at is None:
            return self.probe()
        elapsed = self._clock() - self._probed_at
        interval = PROBE_REFRESH_SECONDS if self._probe_ok else PROBE_RETRY_SECONDS
        if elapsed < interval:
            return False
        return self.probe()

    def _set_reason(self, reason: str, *, probe_ok: bool) -> None:
        self._reason = reason
        self._probe_ok = probe_ok
        self._journal("talk_probe", outcome=reason)

    # -- sessions ---------------------------------------------------------

    def arm(self, max_seconds=None) -> dict:
        if not self._enabled:
            raise TalkUnavailable("not_enabled")
        if self._reason != "ready" or self._format is None:
            raise TalkUnavailable(self._reason)
        seconds = self._max_seconds if max_seconds is None else int(max_seconds)
        if not MIN_SECONDS <= seconds <= self._max_seconds:
            raise ValueError("invalid_request")
        with self._lock:
            if self._session is not None:
                raise TalkBusySession()
            session = _Session(uuid.uuid4().hex, self._clock(), seconds)
            self._session = session
            self._thread = threading.Thread(
                target=self._run, args=(session,), name="camera-talk", daemon=True
            )
            self._thread.start()
        self._journal("talk_armed", max_seconds=seconds, session=session.id[:12])
        return self.snapshot()

    def release(self) -> dict:
        with self._lock:
            session = self._session
        if session is not None:
            session.release.set()
            self._stop_process(session)
        return self.snapshot()

    def stop(self) -> None:
        """Shutdown: end any session and wait briefly for the thread."""
        self.release()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=FFMPEG_STOP_GRACE_SECONDS + 3)

    def snapshot(self) -> dict:
        now = self._clock()
        with self._lock:
            session = self._session or self._last
            active = self._session is not None
        talk = {
            "available": self.available(),
            "reason": self._reason,
            "active": active,
            "max_seconds": self._max_seconds,
            "state": session.state if session is not None else "idle",
            "session_id": None,
            "armed_at": None,
            "expires_at": None,
            "seconds_remaining": None,
            "last_outcome": None,
            "last_ended_at": None,
        }
        if session is not None:
            talk["session_id"] = session.id
            talk["armed_at"] = _iso(session.armed_at)
            talk["expires_at"] = _iso(session.deadline)
            talk["seconds_remaining"] = (
                max(0, int(session.deadline - now)) if active else None
            )
            talk["last_outcome"] = session.outcome
            talk["last_ended_at"] = _iso(session.ended_at) if session.ended_at else None
        return talk

    def state_block(self) -> dict:
        """The nonsecret block published for the controller heartbeat."""
        with self._lock:
            active = self._session is not None
        return {"available": self.available(), "reason": self._reason, "active": active}

    # -- the session thread ----------------------------------------------

    def _run(self, session: _Session) -> None:
        client = None
        outcome = "camera_error"
        started = self._monotonic()
        try:
            client = self._client_factory()
            client.login()
            audio_format = client.talk_ability()
            session.state = "waiting_for_publisher"
            publisher = self._wait_for_publisher(session)
            if publisher is None:
                outcome = "released" if session.release.is_set() else "no_publisher"
                return
            session.state = "streaming"
            outcome = self._stream(session, client, audio_format)
        except TalkBusy:
            outcome = "talk_busy"
        except TalkUnsupported:
            outcome = "unsupported"
        except BaichuanAuthError:
            outcome = "camera_auth"
        except BaichuanUnreachable:
            outcome = "camera_unreachable"
        except (BaichuanError, OSError, ValueError):
            outcome = "camera_error"
        finally:
            self._stop_process(session)
            if client is not None:
                if client.logged_in:
                    try:
                        client.talk_reset()
                    except (BaichuanError, OSError):
                        pass
                client.logout()
            self._kick_publisher()
            session.state = "ended"
            session.outcome = outcome
            session.ended_at = self._clock()
            with self._lock:
                self._last = session
                self._session = None
            if outcome in ("camera_unreachable", "camera_auth", "unsupported"):
                self._set_reason(outcome, probe_ok=False)
            self._journal("talk_ended", reason=outcome, blocks=session.blocks,
                          seconds=round(self._monotonic() - started, 1),
                          session=session.id[:12])

    def _wait_for_publisher(self, session: _Session):
        wait_deadline = min(session.deadline, session.armed_at + self._publisher_wait)
        while not session.release.is_set():
            publisher = self._publisher_id()
            if publisher is not None:
                self._journal("talk_publisher", outcome="ready", session=session.id[:12])
                return publisher
            if self._clock() >= wait_deadline:
                self._journal("talk_publisher", outcome="absent", session=session.id[:12])
                return None
            session.release.wait(self._poll_interval)
        return None

    def _stream(self, session: _Session, client, audio_format) -> str:
        encoder = ImaAdpcmEncoder(audio_format.block_bytes)
        bytes_per_block = encoder.samples_per_block * 2
        remaining = session.deadline - self._clock()
        if remaining <= 0:
            return "time_limit"
        command = ffmpeg_command(self._ffmpeg, self._rtsp_url, audio_format.sample_rate)
        try:
            process = self._spawn(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env={"LANG": "C", "LC_ALL": "C"},
            )
        except OSError:
            self._journal("talk_stream", outcome="ffmpeg_failed", session=session.id[:12])
            return "ffmpeg_failed"
        with session.lock:
            session.process = process
        if session.release.is_set():
            return "released"
        # The hard limit is a kill, so a stalled pipe read can never outlive it.
        timer = threading.Timer(remaining, self._stop_process, args=(session,))
        timer.daemon = True
        timer.start()
        try:
            client.talk_config(audio_format)
            self._journal("talk_stream", outcome="started",
                          format=audio_format.describe(), session=session.id[:12])
            while True:
                pcm = process.stdout.read(bytes_per_block)
                if len(pcm) < bytes_per_block:
                    break
                block = encoder.encode_block(pcm16_to_samples(pcm))
                client.talk_send(block)
                session.blocks += 1
        except BaichuanRefused as error:
            if isinstance(error, TalkBusy):
                raise
            return "camera_error"
        finally:
            timer.cancel()
            self._stop_process(session)
        if session.release.is_set():
            return "released"
        if self._clock() >= session.deadline:
            return "time_limit"
        return "publisher_gone" if session.blocks else "ffmpeg_failed"

    def _stop_process(self, session: _Session) -> None:
        with session.lock:
            process = session.process
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            try:
                process.wait(timeout=FFMPEG_STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=FFMPEG_STOP_GRACE_SECONDS)
        except (OSError, subprocess.SubprocessError):
            pass

    # -- the media gateway's loopback API ---------------------------------

    def _publisher_id(self):
        """The WebRTC session publishing ``talk``, or None while nobody is."""
        try:
            with self._opener(f"{self._gateway_api}/v3/paths/get/{TALK_PATH}", timeout=1) as response:
                if response.status != 200:
                    return None
                body = response.read(_MAX_API_BYTES + 1)
        except (AttributeError, OSError, TypeError, ValueError, urllib.error.URLError):
            return None
        if len(body) > _MAX_API_BYTES:
            return None
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("ready") is not True:
            return None
        source = payload.get("source")
        if not isinstance(source, dict):
            return None
        identifier = source.get("id")
        if (source.get("type") != "webRTCSession" or not isinstance(identifier, str)
                or not 0 < len(identifier) <= 64
                or not set(identifier) <= _SESSION_ID_CHARS):
            return "unknown"
        return identifier

    def _kick_publisher(self) -> None:
        identifier = self._publisher_id()
        if identifier is None or identifier == "unknown":
            return
        request = urllib.request.Request(
            f"{self._gateway_api}/v3/webrtcsessions/kick/{identifier}", method="POST",
        )
        try:
            with self._opener(request, timeout=1) as response:
                response.read(1)
        except (AttributeError, OSError, TypeError, ValueError, urllib.error.URLError):
            pass


def _camera_reason(error: BaichuanError) -> str:
    if isinstance(error, TalkUnsupported):
        return "unsupported"
    if isinstance(error, BaichuanAuthError):
        return "camera_auth"
    if isinstance(error, BaichuanUnreachable):
        return "camera_unreachable"
    if isinstance(error, TalkBusy):
        return "camera_busy"
    return "camera_error"


def _iso(timestamp) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).replace(microsecond=0).isoformat()
