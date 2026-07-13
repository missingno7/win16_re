"""win16.tick_demo — the win16 seam over dos_re.tick_demo.

Game-free: the engine itself (container/format/record/verify) is dos_re's and
tested there; here we assert the win16 re-exports resolve and that the win16
DRIVE (input_demo_drive) translates a demo-exhausted pump into "drive done".
"""
from __future__ import annotations

import pytest

from win16 import tick_demo
from win16.demo import DemoEnded


def test_reexports_are_the_dos_re_primitives():
    import dos_re.tick_demo as dt
    for name in ("TickDemo", "masked_digest", "record_ticks", "replay_to",
                 "verify_ticks"):
        assert getattr(tick_demo, name) is getattr(dt, name)


class _FakeCPU:
    """Runs `stop_after` chunks, then raises DemoEnded from run() — the shape a
    v4 pump raises when the input timeline is exhausted."""

    def __init__(self, stop_after):
        self.stop_after = stop_after
        self.calls = 0

    def run(self, n):
        self.calls += 1
        if self.calls > self.stop_after:
            raise DemoEnded("demo exhausted")
        return n


class _FakeMachine:
    def __init__(self, cpu):
        self.cpu = cpu


def test_input_demo_drive_runs_then_reports_done():
    m = _FakeMachine(_FakeCPU(stop_after=3))
    advance = tick_demo.input_demo_drive(m, chunk=1000)
    assert advance() is True                    # frame 1
    assert advance() is True                    # frame 2
    assert advance() is True                    # frame 3
    assert advance() is False                   # DemoEnded -> drive done
    assert m.cpu.calls == 4


def test_input_demo_drive_propagates_other_errors():
    class Boom(_FakeCPU):
        def run(self, n):
            raise ValueError("not a demo end")
    advance = tick_demo.input_demo_drive(_FakeMachine(Boom(0)))
    with pytest.raises(ValueError):
        advance()
