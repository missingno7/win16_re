"""The recovered LZSS decompressor (simant/recovered/lzss.py) — pure, VM-free.

These tests need no VM: they exercise `decompress` as a plain bytes->bytes
function, the form a native port uses.  A round-trip encoder pins the decoder
against arbitrary data, and the Okumura invariants (window init, 4KB wrap) are
checked directly.  The byte-exact-vs-the-real-game proof lives in
test_hooks.py; this file locks the algorithm itself.
"""
from simant.recovered import lzss


def _lzss_compress(data: bytes) -> bytes:
    """A minimal Okumura-LZSS encoder matching lzss.decompress' bit layout:
    8-bit flag groups (LSB first, 1=literal / 0=match), match = (12-bit offset,
    4-bit length-THRESHOLD) over a 4KB space-initialised window.  Greedy; only
    needs to be a VALID encoding the decoder inverts, not optimal."""
    N, F, THRESH = lzss.WINDOW_SIZE, lzss.MAX_MATCH, lzss.THRESHOLD
    win = bytearray([lzss.SPACE]) * N
    r = lzss.WINDOW_START
    out = bytearray()
    flag_pos = None
    nbits = 0
    i = 0
    while i < len(data):
        if nbits == 0:                              # start a new flag group
            flag_pos = len(out)
            out.append(0)
        # find the longest match (>= THRESH+1) ending in the window
        best_len, best_off = 0, 0
        for off in range(N):
            k = 0
            while (k < F and i + k < len(data)
                   and win[(off + k) & (N - 1)] == data[i + k]):
                k += 1
            if k > best_len:
                best_len, best_off = k, off
        if best_len >= THRESH + 1:
            n = best_len
            out.append(best_off & 0xFF)
            out.append(((best_off >> 4) & 0xF0) | ((n - THRESH - 1) & 0x0F))
            for k in range(n):
                win[r] = data[i + k]
                r = (r + 1) & (N - 1)
            i += n
        else:
            out[flag_pos] |= (1 << nbits)           # mark literal
            out.append(data[i])
            win[r] = data[i]
            r = (r + 1) & (N - 1)
            i += 1
        nbits = (nbits + 1) & 7
    return bytes(out)


def test_roundtrip_various():
    cases = [
        b"",
        b"A",
        b"the ants go marching one by one, hurrah, hurrah",
        b"AAAAAAAAAAAAAAAAAAAAAAAA",                 # long run -> matches
        b"abcabcabcabcabcabcabc",                    # periodic
        bytes(range(256)) * 4,                       # every byte value
        b"SimAnt" * 200,                             # highly repetitive
    ]
    for original in cases:
        comp = _lzss_compress(original)
        assert lzss.decompress(comp, len(original)) == original, original[:20]


def test_window_starts_with_spaces():
    # A match at the very start references the space-filled window: two literal
    # 'X' then a length-3 match at offset WINDOW_START yields the 'X' just
    # written then spaces (the classic Okumura behaviour).
    original = b"X" + b" " * 5
    comp = _lzss_compress(original)
    assert lzss.decompress(comp, len(original)) == original


def test_constants_are_the_okumura_fingerprint():
    assert lzss.WINDOW_SIZE == 4096
    assert lzss.MAX_MATCH == 18
    assert lzss.WINDOW_START == 4096 - 18           # 0x0FEE
    assert lzss.THRESHOLD == 2


def test_decode_chunk_reports_clean_done_at_budget():
    # Decoding fewer bytes than the stream holds stops CLEAN on a flag boundary
    # (a literal), the resume code the streaming game relies on.
    data = _lzss_compress(b"hello world")
    win = bytearray([lzss.SPACE]) * lzss.WINDOW_SIZE
    out = bytearray(5)
    st = lzss.decode_chunk(data, 0, win, out, 0, lzss.WINDOW_START, 0,
                           len(data), 5)
    assert bytes(out) == b"hello"
    assert st.code == lzss.CODE_DONE
    assert st.out_pos == 5
