"""Make fail-loud console output survive ANY console codepage.

This framework's whole diagnostic model is "the report goes to the console"
(AGENTS.md): a VM stop, an API gap, a wall violation.  That report must be
readable on the console the operator actually has — which on Windows is a
codepage console (cp437/cp850/cp852/cp1250/...), not UTF-8.

The trap this closes: `sys.stdout.reconfigure(encoding="utf-8")` looks like it
"adds Unicode support", but it does the opposite.  A Windows console renders
its own codepage, so forcing UTF-8 BYTES at it prints mojibake — an em dash
arrives as three bytes and the console draws three characters ("ÔÇö").  Left
alone, Python talks UTF-16 to a real console and the codepage encodes what it
can natively (cp1250 has an em dash; cp852 does not).

So: keep the console's own encoding, and transliterate only the characters it
cannot encode.  Typographic punctuation degrades to its ASCII ancestor rather
than to noise or an exception — "--" for an em dash, "->" for an arrow — and
anything unmapped becomes "?" instead of raising UnicodeEncodeError (a crash
in the crash reporter is the one failure mode a fail-loud tool must not have).
"""
from __future__ import annotations

import codecs
import os
import sys

#: Typographic characters -> their ASCII ancestors.  Only what our own reports
#: emit; this is a console fallback, not a general Unicode transliterator.
ASCII_FALLBACK = {
    "—": "--",   # em dash
    "–": "-",    # en dash
    "→": "->",   # rightwards arrow
    "←": "<-",   # leftwards arrow
    "…": "...",  # ellipsis
    "‘": "'", "’": "'",
    "“": '"', "”": '"',
    "×": "x",    # multiplication sign
    "✓": "ok", "✗": "X",
}

ERROR_NAME = "win16_ascii_fallback"


def _handler(err: UnicodeError):
    text = err.object[err.start:err.end]
    return "".join(ASCII_FALLBACK.get(c, "?") for c in text), err.end


codecs.register_error(ERROR_NAME, _handler)


def console_encoding(stream, *, output_cp=None) -> str | None:
    """The encoding a Windows CONSOLE will actually render `stream` as, or None.

    The trap this closes: a stream's own `.encoding` is NOT what the console
    draws.  PyPy reports `utf-8` for a console stdout while the console renders
    codepage 852 — so encoding an em dash SUCCEEDS, the fallback below never
    fires, and three UTF-8 bytes are drawn as three cp852 characters.  The
    console's codepage is the only authority, and only the OS can be asked.

    None means "the stream's own encoding is authoritative": not Windows, not a
    console (redirected to a pipe/file, where UTF-8 is right and mojibake is the
    consumer's problem), or the codepage cannot be read.
    """
    if os.name != "nt":
        return None
    try:
        if not stream.isatty():
            return None
    except Exception:  # noqa: BLE001 — a stream without isatty is not a console
        return None
    if output_cp is None:
        try:
            import ctypes
            output_cp = ctypes.windll.kernel32.GetConsoleOutputCP()
        except Exception:  # noqa: BLE001 — no kernel32/console: leave the stream alone
            return None
    if not output_cp:
        return None
    return "utf-8" if output_cp == 65001 else f"cp{output_cp}"


def _same_codec(a: str | None, b: str | None) -> bool:
    try:
        return codecs.lookup(a).name == codecs.lookup(b).name
    except (LookupError, TypeError):
        return False


def make_console_safe() -> None:
    """Point stdout/stderr at the console's OWN codepage, with the fallback.

    Reconfiguring the ENCODING is the load-bearing half: the fallback can only
    fire on characters the target encoding cannot represent, so a stream left
    on UTF-8 in front of a codepage console degrades nothing and emits mojibake.

    Idempotent and never fatal: a stream that cannot be reconfigured (already
    wrapped, replaced by a test harness, not a TextIO) is left as it is.
    """
    for stream in (sys.stdout, sys.stderr):
        target = console_encoding(stream)
        try:
            if target and not _same_codec(target, getattr(stream, "encoding", None)):
                stream.reconfigure(encoding=target, errors=ERROR_NAME)
            else:
                stream.reconfigure(errors=ERROR_NAME)
        except (AttributeError, ValueError, OSError):
            pass
