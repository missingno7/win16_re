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


def make_console_safe() -> None:
    """Install the fallback on stdout/stderr, keeping their native encoding.

    Idempotent and never fatal: a stream that cannot be reconfigured (already
    wrapped, replaced by a test harness, not a TextIO) is left as it is.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors=ERROR_NAME)
        except (AttributeError, ValueError, OSError):
            pass
