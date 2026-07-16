"""Interactive boundary parks (win16/interactive.py + dos_re boundary_heads).

A fact-declared wait loop in the lifted graph (e.g. a splash timeout spinning
on GetTickCount) polls the WALL clock — but interactively that clock only
advances between cpu.run chunks, so inside one lifted invocation the spin can
never exit and dies at the module's MAX_ITERATIONS runaway guard.  The armed
observer parks each pass: re-points CS:IP at the head's RESUME entry, raises
BoundaryParked, and the CPU worker handles it as a YIELD (clock advance +
short sleep), letting the spin exit by its own original condition.

Per-head PARK COSTS (dos_re's frame_gate/pacing_spin vocabulary, priced by
the host): a pacing spin pays a small fixed sleep; a FRAME GATE — the game's
own frame driver, one pass = one frame — pays the whole frame quota
(wait_for_work: block until the next timer is due or input arrives), so the
cadence matches the original busy-spin without burning a core.
"""
import time
import types

import pytest

from win16.interactive import (FRAME_GATE, PACING_SPIN, BoundaryParked,
                               InteractiveDriver)


def _driver(timer_due=None):
    sysobj = types.SimpleNamespace(clock_ms=0, message_source=None,
                                   msg_queue=[], windows=[], quit_posted=None,
                                   timer_due=timer_due or {})
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


# -- per-head park costs (frame_gate vs pacing_spin) -------------------------

def test_unknown_park_kind_is_rejected():
    drv, _sys = _driver()
    with pytest.raises(ValueError):
        drv.arm_boundary_parks(_cpu(), head_kinds={(0x0100, 0x25B6): "later"})


def test_pacing_spin_is_the_default_and_sleeps():
    """An unpriced head is a pacing spin: it pays a small fixed cost so a
    wait that makes no progress cannot burn a host core."""
    drv, _sys = _driver()
    cpu = _cpu()
    # Well clear of the host clock's granularity (Windows' sleep/monotonic
    # quantise around ~15 ms — a 20 ms sleep can measure as 14.999).
    drv.arm_boundary_parks(cpu, spin_sleep=0.05)
    t0 = time.monotonic()
    with pytest.raises(BoundaryParked):
        cpu.boundary_hook(cpu, 0x0E99, 0x07E6, 0x07E8)
    assert time.monotonic() - t0 >= 0.02


def test_frame_gate_waits_until_the_next_tick_is_due():
    """The frame gate's cost: block until the game's next timer is due — the
    cadence the original busy-spin had — not a fixed sleep.

    Tests wait_for_work directly with an explicit cap: a deadline close
    enough to fit under the default 10 ms cap would race the host scheduler
    (the tick can fall due before the call even starts), which is a flaky
    test, not a real contract.
    """
    drv, sysobj = _driver()
    sysobj.timer_due = {(0x10, 1): drv.now_ms() + 40}
    t0 = time.monotonic()
    drv.wait_for_work(cap_ms=200.0)
    waited = time.monotonic() - t0
    assert 0.02 <= waited <= 0.2, f"frame gate waited {waited:.3f}s"
    # ...and the clock the frame loop watches moved with it.
    assert sysobj.clock_ms >= 20


def test_frame_gate_park_is_capped_so_a_non_timer_wait_stays_responsive():
    """A frame gate whose loop watches something other than a timer (a flag
    its own callback sets) must not park indefinitely: the wait is capped and
    the loop re-polls — bounded, never a silent hang."""
    drv, sysobj = _driver()
    cpu = _cpu()
    sysobj.timer_due = {(0x10, 1): drv.now_ms() + 60_000}   # a minute out
    drv.arm_boundary_parks(cpu, head_kinds={(0x0100, 0x25B6): FRAME_GATE})
    t0 = time.monotonic()
    with pytest.raises(BoundaryParked):
        cpu.boundary_hook(cpu, 0x0100, 0x25B6, 0x25BA)
    assert time.monotonic() - t0 <= 0.2, "the frame-gate park was not capped"


def test_frame_gate_park_returns_at_once_when_a_tick_is_already_due():
    """The frame gate must never delay a frame that is ALREADY due — the
    game is behind, not waiting."""
    drv, sysobj = _driver()
    cpu = _cpu()
    sysobj.timer_due = {(0x10, 1): 0}          # due since forever
    drv.arm_boundary_parks(cpu, head_kinds={(0x0100, 0x25B6): FRAME_GATE})
    t0 = time.monotonic()
    with pytest.raises(BoundaryParked):
        cpu.boundary_hook(cpu, 0x0100, 0x25B6, 0x25BA)
    assert time.monotonic() - t0 < 0.02


def test_frame_gate_park_wakes_on_input():
    """Input must break a frame-gate park immediately: SimAnt's frame loop
    exits on GetAsyncKeyState(VK_LBUTTON), so a click cannot wait for the
    next tick."""
    import threading
    drv, sysobj = _driver()
    cpu = _cpu()
    sysobj.timer_due = {(0x10, 1): drv.now_ms() + 5_000}   # far future
    drv.arm_boundary_parks(cpu, head_kinds={(0x0100, 0x25B6): FRAME_GATE})
    threading.Timer(0.03, drv.post_input, (0x10, 0x0201, 1, 0)).start()
    t0 = time.monotonic()
    with pytest.raises(BoundaryParked):
        cpu.boundary_hook(cpu, 0x0100, 0x25B6, 0x25BA)
    assert time.monotonic() - t0 < 1.0, "a click waited for the next tick"


def test_kinds_price_the_park_only_never_the_control_flow():
    """Both kinds park identically — same resume point, same exception; the
    kind is a pacing policy, not semantics."""
    for kind in (FRAME_GATE, PACING_SPIN):
        drv, sysobj = _driver()
        cpu = _cpu()
        sysobj.timer_due = {(0x10, 1): 0}      # due: no frame-gate wait
        drv.arm_boundary_parks(cpu, head_kinds={(0x0100, 0x25B6): kind},
                               spin_sleep=0.0)
        with pytest.raises(BoundaryParked) as exc:
            cpu.boundary_hook(cpu, 0x0100, 0x25B6, 0x25BA)
        assert (cpu.s.cs, cpu.s.ip) == (0x0100, 0x25BA)
        assert exc.value.head == (0x0100, 0x25B6)
