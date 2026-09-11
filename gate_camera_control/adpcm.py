"""IMA ADPCM block encoder in the DVI/WAV layout the camera's talk channel plays.

The camera's ``TalkAbility`` names exactly this format: ``adpcm``, 16-bit,
mono, with ``lengthPerEncoder`` samples per block. Each block opens with a
four-byte header -- the first sample as a little-endian int16, the step index
carried over from the previous block, and a zero -- followed by one nibble per
remaining sample, low nibble first. That is the block gstreamer's
``adpcmenc layout=dvi`` and ffmpeg's ``adpcm_ima_wav`` produce, and it is what
neolink sends the same cameras. The reconstruction mirrors the decoder step for
step, so the encoder's idea of what the camera heard never drifts from what it
actually heard.
"""

from array import array


HEADER_BYTES = 4
STEP_TABLE = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
    253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
    1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024,
    3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493,
    10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623,
    27086, 29794, 32767,
)
INDEX_TABLE = (-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8)


def samples_per_block(block_bytes: int) -> int:
    """Samples one block carries: one in the header, two per data byte."""
    if block_bytes < HEADER_BYTES + 1:
        raise ValueError("an ADPCM block needs a header and at least one data byte")
    return (block_bytes - HEADER_BYTES) * 2 + 1


def pcm16_to_samples(pcm: bytes) -> list:
    """Little-endian signed 16-bit PCM to a list of ints."""
    if len(pcm) % 2:
        raise ValueError("PCM16 must be an even number of bytes")
    samples = array("h")
    samples.frombytes(bytes(pcm))
    if _big_endian_host():
        samples.byteswap()
    return samples.tolist()


def _big_endian_host() -> bool:
    return array("h", [1]).tobytes() != b"\x01\x00"


def _clamp16(value: int) -> int:
    return -32768 if value < -32768 else 32767 if value > 32767 else value


class ImaAdpcmEncoder:
    """Encodes fixed-size blocks; the step index carries from block to block."""

    def __init__(self, block_bytes: int):
        self.block_bytes = int(block_bytes)
        self.samples_per_block = samples_per_block(self.block_bytes)
        self.step_index = 0

    def encode_block(self, samples) -> bytes:
        if len(samples) != self.samples_per_block:
            raise ValueError("block has the wrong number of samples")
        predictor = _clamp16(int(samples[0]))
        step_index = self.step_index
        block = bytearray(self.block_bytes)
        block[0:2] = (predictor & 0xFFFF).to_bytes(2, "little")
        block[2] = step_index
        block[3] = 0
        position = HEADER_BYTES
        low = True
        for sample in samples[1:]:
            step = STEP_TABLE[step_index]
            difference = int(sample) - predictor
            nibble = 0
            if difference < 0:
                nibble = 8
                difference = -difference
            reconstructed = step >> 3
            if difference >= step:
                nibble |= 4
                difference -= step
                reconstructed += step
            step >>= 1
            if difference >= step:
                nibble |= 2
                difference -= step
                reconstructed += step
            step >>= 1
            if difference >= step:
                nibble |= 1
                reconstructed += step
            predictor = _clamp16(predictor - reconstructed if nibble & 8
                                 else predictor + reconstructed)
            step_index += INDEX_TABLE[nibble]
            step_index = 0 if step_index < 0 else 88 if step_index > 88 else step_index
            if low:
                block[position] = nibble
            else:
                block[position] |= nibble << 4
                position += 1
            low = not low
        self.step_index = step_index
        return bytes(block)


def decode_block(block: bytes) -> list:
    """The matching decoder, kept beside the encoder so tests can prove them equal."""
    block = bytes(block)
    if len(block) < HEADER_BYTES + 1:
        raise ValueError("ADPCM block is too short")
    predictor = int.from_bytes(block[0:2], "little", signed=True)
    step_index = block[2]
    if step_index > 88:
        raise ValueError("ADPCM step index out of range")
    samples = [predictor]
    for byte in block[HEADER_BYTES:]:
        for nibble in (byte & 0x0F, byte >> 4):
            step = STEP_TABLE[step_index]
            reconstructed = step >> 3
            if nibble & 4:
                reconstructed += step
            if nibble & 2:
                reconstructed += step >> 1
            if nibble & 1:
                reconstructed += step >> 2
            predictor = _clamp16(predictor - reconstructed if nibble & 8
                                 else predictor + reconstructed)
            step_index += INDEX_TABLE[nibble]
            step_index = 0 if step_index < 0 else 88 if step_index > 88 else step_index
            samples.append(predictor)
    return samples
