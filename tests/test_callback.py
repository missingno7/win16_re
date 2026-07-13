"""call_far's runaway cap (win16/callback.py).

A bounded callback that never returns must raise CallbackOverrun; but an
INTERACTIVE sim-tick TimerProc legitimately busy-waits on the real clock and on
input, so the interactive driver runs it uncapped (max_steps=None) — the cap
would kill a paused game or a "press a key" wait mid-callback.
"""
from types import SimpleNamespace

import pytest


class _FakeCPU:
    """Drives call_far without a real VM: run() counts steps and, once past
    `return_after`, simulates the callback's clean far-return to the sentinel."""

    def __init__(self, return_after: int):
        self.s = SimpleNamespace(cs=0x0100, ip=0x0000, sp=0xFF00, ss=0x0200,
                                 ax=0x1234, dx=0x5678)
        self._clean_sp = 0xFF00
        self.mem = SimpleNamespace(ww=lambda seg, off, v: None)
        self.halted = False
        self.replacement_hooks: dict = {}
        self.hook_names: dict = {}
        self._steps = 0
        self._return_after = return_after

    def run(self, n: int) -> int:
        from win16.callback import _CallbackReturn
        self._steps += n
        if self._steps >= self._return_after:
            self.s.sp = self._clean_sp          # the callback's retf restores SP
            raise _CallbackReturn()
        return n


def test_bounded_callback_overruns():
    from win16.callback import CallbackOverrun, call_far
    cpu = _FakeCPU(return_after=50_000)          # returns later than the cap
    with pytest.raises(CallbackOverrun):
        call_far(cpu, 0x60, 0x0100, 0x2440, [], max_steps=10_000)


def test_unbounded_callback_runs_to_completion():
    from win16.callback import call_far
    cpu = _FakeCPU(return_after=50_000)          # would trip any finite cap
    ax, dx = call_far(cpu, 0x60, 0x0100, 0x2440, [], max_steps=None)
    assert (ax, dx) == (0x1234, 0x5678)          # the callback's result passes through
