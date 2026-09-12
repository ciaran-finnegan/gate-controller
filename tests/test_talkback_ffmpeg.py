"""Prove the ADPCM the forwarder sends is what a stock decoder plays.

The camera's decoder is not available on a workstation; ffmpeg's
``adpcm_ima_wav`` decoder implements the same DVI/IMA block layout, so a WAV
container built around the forwarder's own blocks must decode back to the tone
that went in. Skipped where ffmpeg is not installed, exactly as
``test_clear_stream_ffmpeg.py`` is.
"""

import math
import shutil
import struct
import subprocess
import unittest

from gate_camera_control.adpcm import ImaAdpcmEncoder, pcm16_to_samples


FFMPEG = shutil.which("ffmpeg")
RATE = 16000
BLOCK_BYTES = 516


def ima_wav(blocks, *, rate, block_bytes, samples_per_block, total_samples) -> bytes:
    data = b"".join(blocks)
    fmt = struct.pack(
        "<HHIIHHHH", 0x0011, 1, rate, rate * block_bytes // samples_per_block,
        block_bytes, 4, 2, samples_per_block,
    )
    fact = struct.pack("<I", total_samples)
    body = (b"WAVE"
            + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"fact" + struct.pack("<I", len(fact)) + fact
            + b"data" + struct.pack("<I", len(data)) + data)
    return b"RIFF" + struct.pack("<I", len(body)) + body


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class AdpcmAgainstFfmpegTests(unittest.TestCase):
    def test_ffmpeg_decodes_the_forwarders_blocks_back_to_the_tone(self):
        encoder = ImaAdpcmEncoder(BLOCK_BYTES)
        per_block = encoder.samples_per_block
        count = 40
        reference = [int(9000 * math.sin(2 * math.pi * 440 * index / RATE))
                     for index in range(per_block * count)]
        blocks = [encoder.encode_block(reference[index * per_block:(index + 1) * per_block])
                  for index in range(count)]
        wav = ima_wav(blocks, rate=RATE, block_bytes=BLOCK_BYTES,
                      samples_per_block=per_block, total_samples=len(reference))
        completed = subprocess.run(
            [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "wav", "-i", "pipe:0",
             "-f", "s16le", "-c:a", "pcm_s16le", "pipe:1"],
            input=wav, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=30,
        )
        self.assertEqual(0, completed.returncode, completed.stderr.decode("utf-8", "replace"))
        decoded = pcm16_to_samples(completed.stdout)
        self.assertEqual(len(reference), len(decoded))
        pairs = list(zip(reference, decoded))
        noise = sum((left - right) ** 2 for left, right in pairs) / len(pairs)
        power = sum(left * left for left, _ in pairs) / len(pairs)
        self.assertGreater(10 * math.log10(power / noise), 25.0)


if __name__ == "__main__":
    unittest.main()
