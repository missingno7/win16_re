"""Interactive boundary parks (win16/interactive.py + dos_re boundary_heads).

A fact-declared wait loop in the lifted graph (e.g. a splash timeout spinning
on GetTickCount) polls the WALL clock — but interactively that clock only
advances between cpu.run chunks, so inside one lifted invocation the spin can
never exit and dies at the module's MAX_ITERATIONS runaway guard.  The armed
observer parks each pass: re-points CS:IP at the head's RESUME entry, raises
BoundaryParked, and the CPU worker handles it as a YIELD (clock advance +
short sleep), letting the spin exit by its own original condition.
"""
import time
import types

import pytest

from win16.interactive import BoundaryParked, InteractiveDriver


def _driver():
    sysobj = types.SimpleNamespace(clock_ms=0, message_source=None,
                                   msg_queue=[], windows=[], quit_posted=None)
    return InteractiveDriver(sysobj), sysobj


def _cpu():
    return types.SimpleNamespace(s=types.SimpleNamespace(cs=0, ip=0),
                                 boundary_hook=None)


def test_armed_observer_repoints_at_resume_and_raises():
    drv, _sys = _driver()
    cpu = _cpu()
    drv.arm_boundary_parks(cpu)
    assert cpu.boundary_hook is not None
    with pytest.raises(BoundaryParked) as exc:
        # The emitted observer ABI: (cpu, head_cs, head_ip, resume_ip).
        cpu.boundary_hook(cpu, 0x0E99, 0x07E6, 0x07E8)
    # CS:IP re-pointed at the RESUME entry BEFORE the raise — the next
    # step() re-enters the lifted body there (zero interpreted instructions).
    assert (cpu.s.cs, cpu.s.ip) == (0x0E99, 0x07E8)
    assert exc.value.head == (0x0E99, 0x07E6)
    assert exc.value.resume_ip == 0x07E8


def test_boundary_yield_advances_wall_clock():
    drv, sysobj = _driver()
    cpu = _cpu()
    drv.arm_boundary_parks(cpu, spin_sleep=0.001)
    before = sysobj.clock_ms
    time.sleep(0.02)               # wall time passes while the game "waits"
    drv.boundary_yield()
    assert sysobj.clock_ms > before, \
        "boundary_yield must advance the clock the parked wait polls"


def test_wall_clock_spin_exits_through_parks():
    """The whole env-wait model end to end, without a VM: a 'lifted' spin
    whose exit condition is the wall clock parks each pass; the worker's
    park-yield loop advances the clock; the spin exits by its own condition
    — never a runaway-guard death, never a poked flag."""
    drv, sysobj = _driver()
    cpu = _cpu()
    drv.arm_boundary_parks(cpu, spin_sleep=0.001)
    deadline = drv.now_ms() + 30                 # a 30 ms "splash timeout"
    passes = [0]

    def lifted_spin():
        # One lifted invocation: iterations do NOT advance the clock (the
        # real graph reads GetTickCount = sysobj.clock_ms, frozen inside a
        # chunk); each pass hits the emitted observer.
        for _ in range(10):                      # tiny MAX_ITERATIONS stand-in
            passes[0] += 1
            if sysobj.clock_ms >= deadline:
                return True                      # the loop's own exit
            cpu.boundary_hook(cpu, 0x0E99, 0x07E6, 0x07E8)
        raise AssertionError("runaway guard would have killed the spin")

    for _ in range(10_000):                      # the CPU worker loop
        try:
            done = lifted_spin()
        except BoundaryParked:
            drv.boundary_yield()
            continue
        assert done
        break
    else:
        raise AssertionError("spin never exited")
    assert passes[0] > 1                         # it really parked and resumed
