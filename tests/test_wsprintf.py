"""The Win16 wsprintf format engine.

The specifier set under test is the one that actually appears in a real
program's format strings (SimAnt's DGROUP yields %d %i %u %x %X %c %s %p
%ld %lu %#x %#lx %03x %6u %8ld %-ld %%), plus the loud-failure paths.
"""
from __future__ import annotations

import pytest

from win16.wsprintf import FormatGap, format_win16

STRINGS = {(0x1234, 0x0010): b"hello", (0x4000, 0x0002): b"world"}
NEAR_SEG = 0x4000


def render(fmt: bytes, *words: int) -> str:
    """Feed `words` as the raw 16-bit argument list, little-endian order."""
    cursor = [0]

    def next_word() -> int:
        v = words[cursor[0]]
        cursor[0] += 1
        return v

    def next_dword() -> int:
        lo, hi = next_word(), next_word()
        return lo | (hi << 16)

    def read_far_string(seg: int, off: int) -> bytes:
        return STRINGS[(seg, off)]

    return format_win16(fmt, next_word, next_dword, read_far_string,
                        NEAR_SEG).decode("latin-1")


def test_plain_text_and_literal_percent():
    assert render(b"no args") == "no args"
    assert render(b"100%%") == "100%"
    assert render(b"%d%%", 50) == "50%"          # DGROUP: '%d%%'


def test_signed_and_unsigned_words():
    assert render(b"(%d)", 42) == "(42)"
    assert render(b"(%d)", 0xFFFF) == "(-1)"     # word is signed for %d
    assert render(b"(%i)", 0x8000) == "(-32768)"
    assert render(b"(%u)", 0xFFFF) == "(65535)"


def test_long_arguments_consume_two_words():
    assert render(b"%lu", 0x0000, 0x0001) == "65536"
    assert render(b"%ld", 0xFFFF, 0xFFFF) == "-1"
    assert render(b"Ave Length: %lu Speed: %lu", 5, 0, 7, 0) == \
        "Ave Length: 5 Speed: 7"


def test_hex_forms_and_alt_flag():
    assert render(b"%x", 0x1F) == "1f"
    assert render(b"%X", 0x1F) == "1F"
    assert render(b"%#x", 0x1F) == "0x1f"
    assert render(b"%#x", 0) == "0"             # C: '#' adds nothing to zero
    assert render(b"%#lx", 0x5678, 0x1234) == "0x12345678"


def test_width_padding_and_flags():
    assert render(b"%6u", 42) == "    42"
    assert render(b"%03x", 0x1F) == "01f"
    assert render(b"%-ld", 42, 0) == "42"
    assert render(b"%-6u|", 42) == "42    |"
    assert render(b"%06d", 0xFFFF) == "-00001"   # zero pad after the sign
    assert render(b"%#06x", 0x1F) == "0x001f"    # ...and after the 0x prefix


def test_char_and_far_string():
    assert render(b"%c%c", 0x41, 0x142) == "AB"  # low byte only
    assert render(b"[%s]", 0x0010, 0x1234) == "[hello]"   # off, then seg
    assert render(b"%s%s", 0x0010, 0x1234, 0x0002, 0x4000) == "helloworld"


def test_near_string_uses_the_callers_data_segment():
    assert render(b"%hs", 0x0002) == "world"     # near ptr, DS = NEAR_SEG


def test_far_pointer():
    assert render(b"handle=%p", 0x0010, 0x1234) == "handle=1234:0010"


def test_unimplemented_specifiers_fail_loud():
    with pytest.raises(FormatGap, match="precision"):
        render(b"%.3s", 0x0010, 0x1234)
    with pytest.raises(FormatGap, match="conversion"):
        render(b"%f", 0, 0)
    with pytest.raises(FormatGap, match="truncated"):
        render(b"trailing %")
