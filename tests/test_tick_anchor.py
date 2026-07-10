"""Win16System.tick_count: the anchored headless clock floor.

A resumed snapshot re-bases the instruction floor to the saved clock
(vmsnap.load_snapshot / verify.clone_machine set clock_floor_anchor).  Without
the anchor, a snapshot whose wall clock ran far ahead of the instruction pace
freezes GetTickCount on resume until the raw floor catches up.  Pure arithmetic,
so tested on the unbound method with a minimal fake — no game, no VM boot.
"""
from __future__ import annotations

import types

from win16.api.system import INSTR_PER_MS, Win16System


def _sys(*, clock_ms, instr, anchor=None, interactive=False):
    m = types.SimpleNamespace(cpu=types.SimpleNamespace(instruction_count=instr))
    ns = types.SimpleNamespace(clock_ms=clock_ms, interactive=interactive, machine=m)
    if anchor is not None:
        ns.clock_floor_anchor = anchor
    return ns


def test_interactive_uses_the_wall_clock():
    s = _sys(clock_ms=5000, instr=10**9, interactive=True)
    assert Win16System.tick_count(s) == 5000


def test_headless_floor_advances_from_the_anchor_not_from_zero():
    # Snapshot saved at 100 s of wall clock and 2 s of instruction pace.
    anchor = (2000 * INSTR_PER_MS, 100_000)
    # Right after resume: no instructions past the anchor -> clock == saved, not 2 s.
    s = _sys(clock_ms=100_000, instr=2000 * INSTR_PER_MS, anchor=anchor)
    assert Win16System.tick_count(s) == 100_000
    # 500 ms of instructions later, it has advanced 500 ms from the saved clock.
    s = _sys(clock_ms=100_000, instr=(2000 + 500) * INSTR_PER_MS, anchor=anchor)
    assert Win16System.tick_count(s) == 100_500


def test_unanchored_floor_would_freeze_a_resumed_ahead_clock():
    """The bug the anchor fixes: raw floor = instr/INSTR_PER_MS ignores the saved
    clock, so an ahead-of-pace snapshot sees a frozen GetTickCount."""
    # Same state, but no anchor (the pre-fix behaviour, via the (0,0) default).
    s = _sys(clock_ms=100_000, instr=2000 * INSTR_PER_MS)
    # Raw floor is only 2000 ms; max(100_000, 2000) pins to the saved clock and
    # then does NOT move until the raw floor climbs past 100_000 ms — frozen.
    assert Win16System.tick_count(s) == 100_000
    s = _sys(clock_ms=100_000, instr=(2000 + 500) * INSTR_PER_MS)
    assert Win16System.tick_count(s) == 100_000        # still frozen without the anchor
