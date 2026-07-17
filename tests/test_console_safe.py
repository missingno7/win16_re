"""Fail-loud reports must land readably on a codepage console.

The regression this pins: forcing UTF-8 bytes at a Windows codepage console
prints mojibake (an em dash arrives as three bytes and draws as three
characters), so the console's OWN encoding is kept and only what it cannot
encode is transliterated to ASCII.
"""
import io

from win16.console import (ASCII_FALLBACK, ERROR_NAME, console_encoding,
                           make_console_safe)


class _FakeConsole(io.TextIOWrapper):
    """A stdout that CLAIMS utf-8 while sitting in front of a codepage console.

    Not a hypothetical: PyPy reports `utf-8` for a console stdout on a cp852
    Windows box, which is exactly how mojibake survived the first fix.
    """

    def __init__(self, encoding="utf-8"):
        self.reconfigured_to = None
        super().__init__(io.BytesIO(), encoding=encoding, newline="")

    def isatty(self):
        return True

    def reconfigure(self, *, encoding=None, errors=None):
        if encoding is not None:
            self.reconfigured_to = encoding


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


def test_console_codepage_beats_the_streams_own_claim(monkeypatch):
    # THE regression: the stream says utf-8, the console draws cp852.  Trusting
    # the stream leaves the encoding alone, the em dash encodes cleanly, the
    # fallback never fires, and the console draws "ÔÇö".
    monkeypatch.setattr("os.name", "nt")
    stream = _FakeConsole(encoding="utf-8")
    assert console_encoding(stream, output_cp=852) == "cp852"


def test_a_utf8_console_is_left_on_utf8():
    stream = _FakeConsole(encoding="utf-8")
    assert console_encoding(stream, output_cp=65001) in (None, "utf-8")


def test_redirected_output_keeps_its_own_encoding(monkeypatch):
    # A pipe/file is not a console: UTF-8 is correct there, and transliterating
    # would corrupt a consumer that asked for it.
    monkeypatch.setattr("os.name", "nt")
    raw = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="")
    assert console_encoding(raw, output_cp=852) is None


def test_fallback_table_maps_to_pure_ascii():
    for src, dst in ASCII_FALLBACK.items():
        dst.encode("ascii")   # raises if a "fallback" is not actually ASCII
