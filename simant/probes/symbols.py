"""Read SImAnt's shipped SIMANTW.SYM to name code addresses.

The linker symbol file is a MAPSYM table: runs of `[offset:2][len:1][name]`
records, grouped per segment and sorted by offset within a segment.  We don't
need the full MAPSYM structure for triage — a flat "nearest preceding symbol"
lookup over every record is enough to turn a hot `seg:offset` from the profiler
into a named routine to recover (it named `_StillDown`, `_DialogWaitInit`, ...
during the USER.186 bring-up).

Approximate by design: offsets repeat across SimAnt's six code segments, so the
resolver returns the globally nearest preceding symbol.  Because the tables are
dense (~tens of bytes apart) an exact or near-exact hit is almost always the
right routine; always cross-check against the raw `seg:offset`.
"""
from __future__ import annotations

import re
from bisect import bisect_right
from functools import lru_cache
from pathlib import Path

_SYM_PATH = Path(__file__).resolve().parent.parent.parent / "assets" / "ANTWIN" \
    / "SIMANTW.SYM"
_NAME_RE = re.compile(rb"[A-Za-z_][A-Za-z0-9_@.$]*")


@lru_cache(maxsize=1)
def _symbols() -> list[tuple[int, str]]:
    """All (offset, name) records, sorted and de-duplicated by offset."""
    if not _SYM_PATH.exists():
        return []
    data = _SYM_PATH.read_bytes()
    seen: dict[int, str] = {}
    for j in range(len(data) - 3):
        ln = data[j + 2]
        if not (3 <= ln <= 40):
            continue
        name = data[j + 3:j + 3 + ln]
        if _NAME_RE.fullmatch(name):
            off = data[j] | (data[j + 1] << 8)
            seen.setdefault(off, name.decode("latin-1"))
    return sorted(seen.items())


def nearest_symbol(seg: int, off: int) -> str:
    """The nearest preceding symbol to `off`, as 'name+0xNN' (seg is shown for
    context only — the lookup is offset-based, see the module docstring)."""
    syms = _symbols()
    if not syms:
        return "(no SIMANTW.SYM)"
    offs = [o for o, _ in syms]
    i = bisect_right(offs, off) - 1
    if i < 0:
        return "(before first symbol)"
    sym_off, name = syms[i]
    delta = off - sym_off
    return name if delta == 0 else f"{name}+0x{delta:X}"


def symbols_in_range(lo: int, hi: int) -> list[tuple[int, str]]:
    """Every (offset, name) with lo <= offset < hi (for disassembly context)."""
    return [(o, n) for o, n in _symbols() if lo <= o < hi]
