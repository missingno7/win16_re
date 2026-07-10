"""The Win16 wsprintf/wvsprintf format engine (USER.420/421).

Windows 3.x shipped its own printf-alike so applications need not link the C
runtime's.  Its grammar is a subset of C's:

    %[-][#][0][width][l|h]<type>          type in: d i u x X c s p %

with the 16-bit realities that make it *not* the host's `%` operator:

- an int argument is a **word** (2 bytes); ``l`` promotes it to a dword.
- ``%s`` takes a **far** pointer (4 bytes, the Win16 LPSTR); ``%hs`` takes a
  near pointer (2 bytes, relative to the caller's data segment).
- ``%p`` prints a far pointer as ``SSSS:OOOO``.
- arguments are read sequentially from an ``lpArglist`` buffer (what the
  caller's varargs left on its stack), not from a Python call.

Deliberately NOT implemented: precision (``%.3s``), ``+``/space flags, ``e``,
``f``, ``g``, ``n``.  No SimAnt format string uses them; a program that does
gets a loud, precise error rather than a plausibly-wrong string.  Add them
when a real call site proves the need.

The engine is pure: it pulls arguments through callbacks, so it is testable
without a VM and reusable by both wvsprintf (pascal, arglist pointer) and a
future wsprintf (cdecl, args inline on the stack).
"""
from __future__ import annotations

from typing import Callable

_FLAGS = "-#0"
_DIGITS = "0123456789"


class FormatGap(NotImplementedError):
    """A format specifier no observed call site needed — implement, don't guess."""


def _int_str(value: int, base: int, upper: bool, alt: bool) -> str:
    if base == 10:
        return str(value)
    text = f"{value:x}"
    if upper:
        text = text.upper()
    if alt and value != 0:
        text = ("0X" if upper else "0x") + text
    return text


def _pad(text: str, width: int, left: bool, zero: bool) -> str:
    if len(text) >= width:
        return text
    if left:                                     # '-' beats '0'
        return text + " " * (width - len(text))
    if not zero:
        return " " * (width - len(text)) + text
    # Zero padding goes *after* any 0x/0X prefix or '-' sign, as in C.
    prefix = ""
    if text[:1] == "-":
        prefix, text = "-", text[1:]
    elif text[:2] in ("0x", "0X"):
        prefix, text = text[:2], text[2:]
    return prefix + "0" * (width - len(prefix) - len(text)) + text


def format_win16(fmt: bytes,
                 next_word: Callable[[], int],
                 next_dword: Callable[[], int],
                 read_far_string: Callable[[int, int], bytes],
                 near_seg: int) -> bytes:
    """Render `fmt`, pulling each argument in turn from the callbacks.

    `next_word`/`next_dword` consume the next 2/4 bytes of the argument list;
    `read_far_string(seg, off)` reads a NUL-terminated string.
    """
    out = bytearray()
    i, n = 0, len(fmt)
    while i < n:
        ch = fmt[i]
        if ch != 0x25:                           # '%'
            out.append(ch)
            i += 1
            continue
        i += 1
        left = alt = zero = False
        while i < n and chr(fmt[i]) in _FLAGS:
            f = chr(fmt[i])
            left, alt, zero = left or f == "-", alt or f == "#", zero or f == "0"
            i += 1
        width = 0
        while i < n and chr(fmt[i]) in _DIGITS:
            width = width * 10 + (fmt[i] - 0x30)
            i += 1
        if i < n and fmt[i] == 0x2E:             # '.'
            raise FormatGap(
                f"wsprintf precision in {fmt!r} — no observed call site uses "
                "it; implement it against a real call site rather than guess")
        size = ""
        if i < n and chr(fmt[i]) in "lh":
            size = chr(fmt[i])
            i += 1
        if i >= n:
            raise FormatGap(f"truncated format specifier in {fmt!r}")
        conv = chr(fmt[i])
        i += 1

        if conv == "%":
            out.append(0x25)
            continue
        if conv == "c":
            out.append(next_word() & 0xFF)
            continue
        if conv == "s":
            if size == "h":
                text = read_far_string(near_seg, next_word())
            else:
                off = next_word()
                text = read_far_string(next_word(), off)
            out += _pad(text.decode("latin-1"), width, left, False).encode("latin-1")
            continue
        if conv == "p":
            off, seg = next_word(), next_word()
            out += _pad(f"{seg:04X}:{off:04X}", width, left, zero).encode("latin-1")
            continue
        if conv in "diuxX":
            raw = next_dword() if size == "l" else next_word()
            if conv in "di":                     # signed: sign-extend the word
                bits = 32 if size == "l" else 16
                if raw & (1 << (bits - 1)):
                    raw -= 1 << bits
            base, upper = (16, conv == "X") if conv in "xX" else (10, False)
            text = _int_str(raw, base, upper, alt)
            out += _pad(text, width, left, zero).encode("latin-1")
            continue
        raise FormatGap(
            f"wsprintf conversion %{size}{conv} in {fmt!r} — not implemented; "
            "add it from its real call site")
    return bytes(out)
