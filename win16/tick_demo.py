"""Game-tick demos for win16 — the endgame's mode-independent equivalence proof.

This is a thin win16 seam over :mod:`dos_re.tick_demo` (which owns the container,
on-disk format, and the record / replay / verify loops).  A win16 machine's
``.cpu`` **is** the dos_re CPU8086, so ``record_ticks(machine, ...)`` drives it
directly — nothing here re-implements the engine.

Why a tick demo (vs the v4 instruction-keyed input demo in :mod:`win16.demo`):
the v4 anchor is the INSTRUCTION COUNT, which is hook-config-dependent — a lifted
island runs far fewer emulated instructions than the ASM it replaces, so one v4
recording only replays faithfully under the exact hook set it was recorded with
(cross-config replay silently desyncs).  A tick demo keys to the GAME TICK
instead — per tick it stores the input the game consumed plus a masked digest of
the gameplay state — so ONE recording replays identically in every mode
(pure-ASM / islands / hybrid) and drives a VM-less native core tick-for-tick.

The win16 mapping the adapter (a game-port project) supplies to ``record_ticks``:

* **tick** — the game's main-loop iteration.  For a message-pump game that is the
  sim WM_TIMER TimerProc firing (SimAnt's ~59fps sim tick); ``seed_ip`` /
  ``commit_ip`` are that proc's entry / a stable end-of-tick site.
* **consumption points** (``observe``) — where the tick READS input: the
  GetMessage / PeekMessage return sites and the GetAsyncKeyState / GetCursorPos /
  GetKeyState polls, captured at the read, not at arrival (the "refine" pattern).
* **sidebands** — GetTickCount (and anything else derived from the clock/
  instruction count) has no VM-less equivalent, so record it per tick and inject
  it before each native tick.
* **digest boundary** — :func:`masked_digest` over the gameplay-owned DGROUP
  region, excluding render/input-plumbing/audio bytes (the same ownership mask
  the forward lockstep oracle proves byte-exact).

Everything game-specific — the seam addresses, the key-cell list, the exclusion
mask, the native tick function — is the adapter's.  This module owns only the
win16 DRIVE: replaying an existing v4 input demo as ``advance_one_frame``.
"""
from __future__ import annotations

from dos_re.tick_demo import (TickDemo, masked_digest, record_ticks, replay_to,
                              verify_ticks)

from .demo import DemoEnded

__all__ = ["TickDemo", "masked_digest", "record_ticks", "replay_to",
           "verify_ticks", "input_demo_drive"]


def input_demo_drive(machine, *, chunk: int = 4096):
    """An ``advance_one_frame`` callable for :func:`record_ticks` that drives a
    win16 ``machine`` by replaying an already-installed v4 input demo.

    This is the win16 analogue of DOS's input-demo drive: the tick boundaries are
    found by ``record_ticks``'s seam-watching ``cpu.step`` wrapper, so a "frame"
    here is just a bounded run of ``chunk`` instructions.  Each call runs one
    chunk and returns True; when the input demo is exhausted the pump raises
    :class:`~win16.demo.DemoEnded`, which we translate to False (drive done).

    The caller installs the ``DemoDriver`` on the system object first (so the
    machine's own message pump replays the recorded input) — see
    ``win16.demo.DemoDriver.install``.
    """
    cpu = machine.cpu

    def advance() -> bool:
        try:
            cpu.run(chunk)
            return True
        except DemoEnded:
            return False

    return advance
