#!/usr/bin/env python3
"""Check push-to-talk on the camera itself, from the Pi, with nobody at the gate.

Plays a short two-tone test through the camera's talk channel -- 1.5 s at
1000 Hz, a gap, 1.5 s at 1500 Hz -- with the service's own Baichuan client,
ADPCM encoder and BcMedia framing, while recording the camera's own microphone
from the media gateway's loopback `camera` path. Then it looks for each tone.
The gate speaker is loud in the gate microphone, so a tone that played is
unmistakable, at its pitch, and one that did not is simply absent.

    sudo python3 scripts/talk_tone_check.py                  # exit 0 if both tones are heard
    sudo python3 scripts/talk_tone_check.py --save test.wav  # keep the recording
    sudo python3 scripts/talk_tone_check.py --drain 0        # reset with the last block

It is audible at the gate for about four seconds. It needs root to read
/etc/gate-camera-control.env, and it prints only the negotiated format and the
analysis: no credential, address or camera payload. It does not go through
gate-camera-control, so it must not run while an operator is talking.
"""
from __future__ import annotations

import argparse
import math
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gate_camera_control.talk import TALK_DRAIN_SECONDS  # noqa: E402

CAMERA_ENV = Path("/etc/gate-camera-control.env")
CAMERA_RTSP = "rtsp://127.0.0.1:8554/camera"
RATE = 16000
RECORD_SECONDS = 16
LEAD_IN_SECONDS = 4.0
AMPLITUDE = 5000  # about -16 dBFS; the camera's own volume setting does the rest
PLAN = ((1000, 1.5), (0, 0.6), (1500, 1.5))
WINDOW_SECONDS = 0.05
TONE_SHARE = 0.4  # of a window's power at the tone's frequency
HEARD_SECONDS = 1.0  # of each 1.5 s tone


def load_env(path: Path) -> dict[str, str]:
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def tone_share(window, frequency: float, rate: int) -> float:
    """The share of a window's power at one frequency (Goertzel): about 1 for a pure tone."""
    energy = sum(sample * sample for sample in window)
    if energy == 0:
        return 0.0
    coefficient = 2 * math.cos(2 * math.pi * frequency / rate)
    previous = before = 0.0
    for sample in window:
        previous, before = sample + coefficient * previous - before, previous
    power = previous * previous + before * before - coefficient * previous * before
    return min(1.0, 2 * power / (len(window) * energy))


def analyse(samples, rate: int = RATE):
    """Per window: start time, level in dBFS, and each test tone's share."""
    window = int(rate * WINDOW_SECONDS)
    rows = []
    for start in range(0, len(samples) - window + 1, window):
        chunk = samples[start:start + window]
        rms = math.sqrt(sum(sample * sample for sample in chunk) / window) or 1e-9
        rows.append((start / rate, 20 * math.log10(rms / 32768),
                     *(tone_share(chunk, frequency, rate)
                       for frequency, _ in PLAN if frequency)))
    return rows


def heard_seconds(rows, column: int) -> float:
    """How long the tone in this column was carried, in seconds."""
    return sum(WINDOW_SECONDS for row in rows if row[column] > TONE_SHARE)


def play_tones(client, drain_seconds: float) -> int:
    from gate_camera_control.adpcm import ImaAdpcmEncoder

    audio_format = client.talk_ability()
    print(f"format {audio_format.describe()} duplex={audio_format.duplex} "
          f"block_bytes={audio_format.block_bytes}")
    encoder = ImaAdpcmEncoder(audio_format.block_bytes)
    per_block = encoder.samples_per_block
    client.talk_config(audio_format)
    sent, phase, schedule = 0, 0.0, time.monotonic()
    for frequency, seconds in PLAN:
        step = 2 * math.pi * frequency / audio_format.sample_rate
        for _ in range(max(1, round(seconds * audio_format.sample_rate / per_block))):
            block = []
            for _ in range(per_block):
                block.append(int(AMPLITUDE * math.sin(phase)) if frequency else 0)
                phase = (phase + step) % (2 * math.pi)
            client.talk_send(encoder.encode_block(block))
            sent += 1
            # Real time, as a live publisher would: the camera buffers about
            # half a second and no more.
            schedule += per_block / audio_format.sample_rate
            delay = schedule - time.monotonic()
            if delay > 0:
                time.sleep(delay)
    time.sleep(drain_seconds)
    return sent


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--drain", type=float, default=TALK_DRAIN_SECONDS,
                        help="seconds between the last block and TalkReset "
                             f"(default {TALK_DRAIN_SECONDS}, as the service)")
    parser.add_argument("--save", type=Path, help="write the recording here as a WAV")
    arguments = parser.parse_args(argv)

    from gate_camera_control.baichuan import BaichuanClient

    env = load_env(CAMERA_ENV)
    with tempfile.TemporaryDirectory(prefix="talk-tone-check-") as directory:
        raw_path = Path(directory) / "microphone.raw"
        recorder = subprocess.Popen(
            ["/usr/bin/ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-rtsp_transport", "tcp", "-i", CAMERA_RTSP, "-vn", "-map", "0:a:0",
             "-ac", "1", "-ar", str(RATE), "-t", str(RECORD_SECONDS), "-f", "s16le",
             str(raw_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(LEAD_IN_SECONDS)
        client = BaichuanClient(env["GATE_CAMERA_HOST"], env["GATE_CAMERA_USERNAME"],
                                env["GATE_CAMERA_PASSWORD"])
        try:
            client.login()
            sent = play_tones(client, arguments.drain)
        finally:
            if client.logged_in:
                try:
                    client.talk_reset()
                except Exception as error:  # noqa: BLE001 - the type only, never a payload
                    print(f"talk_reset failed: {type(error).__name__}")
            client.logout()
        print(f"sent {sent} blocks; TalkReset {arguments.drain:.2f} s after the last")
        try:
            recorder.wait(timeout=RECORD_SECONDS + 15)
        except subprocess.TimeoutExpired:
            recorder.kill()
        data = raw_path.read_bytes() if raw_path.exists() else b""

    samples = struct.unpack(f"<{len(data) // 2}h", data[:len(data) // 2 * 2])
    print(f"recorded {len(samples) / RATE:.1f} s of the camera's microphone")
    rows = analyse(samples)
    results = [(frequency, seconds, heard_seconds(rows, column))
               for column, (frequency, seconds) in enumerate(
                   ((f, s) for f, s in PLAN if f), start=2)]
    for frequency, seconds, heard in results:
        print(f"{frequency} Hz: sent {seconds:.2f} s, heard {heard:.2f} s")
    if arguments.save:
        with wave.open(str(arguments.save), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(RATE)
            output.writeframes(data)
        print(f"recording saved to {arguments.save}")
    heard = all(seconds >= HEARD_SECONDS for _, _, seconds in results)
    print("PASS: both tones played through the camera" if heard
          else "FAIL: the tones were not heard on the camera's microphone")
    return 0 if heard else 1


if __name__ == "__main__":
    sys.exit(main())
