"""AES-128 in CFB-128 mode, in pure Python, for the camera's Baichuan channel.

Only the forward cipher is needed: CFB both encrypts and decrypts with it. What
passes through here is a few kilobytes of control XML per talk session -- the
audio itself travels unencrypted, as the camera expects -- so a table-driven
pure-Python cipher is fast enough by three orders of magnitude, and it keeps the
one process holding camera credentials free of any package outside the
interpreter. The vectors in ``tests/test_talkback.py`` are FIPS-197 C.1 and
NIST SP 800-38A F.3.13.
"""

BLOCK_BYTES = 16
_ROUNDS = 10


def _build_sbox() -> bytes:
    sbox = [0] * 256
    p = q = 1
    while True:
        # p walks the multiplicative group by repeated multiplication by 3;
        # q walks it the other way by division, so q is always p's inverse.
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        affine = q ^ (q << 1) ^ (q << 2) ^ (q << 3) ^ (q << 4)
        sbox[p] = (affine ^ (affine >> 8) ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    return bytes(sbox)


_SBOX = _build_sbox()
_XTIME = bytes(((value << 1) ^ 0x1B) & 0xFF if value & 0x80 else value << 1
               for value in range(256))
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)
_SHIFT_ROWS = tuple(((column + row) % 4) * 4 + row for column in range(4) for row in range(4))


def _expand_key(key: bytes) -> list:
    if len(key) != BLOCK_BYTES:
        raise ValueError("AES-128 needs a 16-byte key")
    words = [list(key[index:index + 4]) for index in range(0, BLOCK_BYTES, 4)]
    for index in range(4, 4 * (_ROUNDS + 1)):
        word = list(words[index - 1])
        if index % 4 == 0:
            word = word[1:] + word[:1]
            word = [_SBOX[value] for value in word]
            word[0] ^= _RCON[index // 4 - 1]
        words.append([left ^ right for left, right in zip(words[index - 4], word)])
    return [bytes(sum(words[index:index + 4], [])) for index in range(0, len(words), 4)]


def _mix_columns(state: bytes) -> bytes:
    mixed = bytearray(BLOCK_BYTES)
    for column in range(0, BLOCK_BYTES, 4):
        a0, a1, a2, a3 = state[column:column + 4]
        total = a0 ^ a1 ^ a2 ^ a3
        mixed[column] = a0 ^ total ^ _XTIME[a0 ^ a1]
        mixed[column + 1] = a1 ^ total ^ _XTIME[a1 ^ a2]
        mixed[column + 2] = a2 ^ total ^ _XTIME[a2 ^ a3]
        mixed[column + 3] = a3 ^ total ^ _XTIME[a3 ^ a0]
    return bytes(mixed)


def _round(state: bytes, round_key: bytes, *, final: bool) -> bytes:
    substituted = bytes(_SBOX[value] for value in state)
    shifted = bytes(substituted[position] for position in _SHIFT_ROWS)
    if not final:
        shifted = _mix_columns(shifted)
    return bytes(left ^ right for left, right in zip(shifted, round_key))


class Aes128:
    """One expanded key; ``encrypt_block`` is the FIPS-197 forward cipher."""

    def __init__(self, key: bytes):
        self._round_keys = _expand_key(bytes(key))

    def encrypt_block(self, block: bytes) -> bytes:
        if len(block) != BLOCK_BYTES:
            raise ValueError("AES blocks are 16 bytes")
        state = bytes(left ^ right for left, right in zip(block, self._round_keys[0]))
        for index in range(1, _ROUNDS):
            state = _round(state, self._round_keys[index], final=False)
        return _round(state, self._round_keys[_ROUNDS], final=True)

    def cfb_encrypt(self, iv: bytes, data: bytes) -> bytes:
        return self._cfb(iv, data, decrypt=False)

    def cfb_decrypt(self, iv: bytes, data: bytes) -> bytes:
        return self._cfb(iv, data, decrypt=True)

    def _cfb(self, iv: bytes, data: bytes, *, decrypt: bool) -> bytes:
        """CFB-128 with a partial final segment, as the camera and its apps use it."""
        if len(iv) != BLOCK_BYTES:
            raise ValueError("CFB needs a 16-byte IV")
        data = bytes(data)
        output = bytearray()
        register = bytes(iv)
        for index in range(0, len(data), BLOCK_BYTES):
            segment = data[index:index + BLOCK_BYTES]
            keystream = self.encrypt_block(register)
            processed = bytes(left ^ right for left, right in zip(segment, keystream))
            output += processed
            register = segment if decrypt else processed
        return bytes(output)
