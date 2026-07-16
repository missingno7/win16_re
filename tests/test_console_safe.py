"""Fail-loud reports must land readably on a codepage console.

The regression this pins: forcing UTF-8 bytes at a Windows codepage console
prints mojibake (an em dash arrives as three bytes and draws as three
characters), so the console's OWN encoding is kept and only what it cannot
encode is transliterated to ASCII.
"""
import io

from win16.console import ASCII_FALLBACK, ERROR_NAME, make_console_safe


def _write(text: str, encoding: str) -> str:
    """Encode `text` to a stream of `encoding` using the fallback handler."""
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding=encoding, errors=ERROR_NAME,
                              newline="")
    stream.write(text)
    stream.flush()
    return raw.getvalue().decode(encoding)


def test_em_dash_degrades_to_ascii_on_a_codepage_without_it():
    # cp437/cp852 have no em dash: it must become "--", never mojibake.
    assert _write("VMless wall — HOLDS", "cp437") == "VMless wall -- HOLDS"


def test_em_dash_is_kept_when_the_codepage_has_one():
    # cp1250 encodes U+2014 natively (0x97) — no transliteration needed.
    assert _write("wall — HOLDS", "cp1250") == "wall — HOLDS"


def test_arrow_and_ellipsis_degrade_to_their_ascii_ancestors():
    assert _write("a → b …", "cp437") == "a -> b ..."


def test_unmapped_character_becomes_a_question_mark_not_an_exception():
    # The one failure mode a crash reporter must not have is crashing.
    assert _write("ant 字 nest", "cp437") == "ant ? nest"


def test_ascii_text_is_untouched():
    assert _write("VM STOPPED - GDI.136 not implemented", "cp437") == \
        "VM STOPPED - GDI.136 not implemented"


def test_make_console_safe_is_idempotent_and_never_raises():
    make_console_safe()
    make_console_safe()


def test_fallback_table_maps_to_pure_ascii():
    for src, dst in ASCII_FALLBACK.items():
        dst.encode("ascii")   # raises if a "fallback" is not actually ASCII
