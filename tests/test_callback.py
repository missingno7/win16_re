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
    assert cpu.win16_callback_frames == []       # clean return pops our frame


def test_boundary_park_inside_callback_is_a_yield_not_a_stop():
    """A fact-declared wait loop that parks INSIDE a WndProc/TimerProc
    callback (win16.interactive boundary parks, lifted graph) must be caught
    by call_far's chunk loop as a yield — the callback keeps stepping (the
    next step resumes at the RESUME entry) and still far-returns cleanly.
    Found live: SimAnt's _WaitHundredths pacing delay reached from
    MAINWNDPROC during a scenario start."""
    from win16.callback import call_far
    from win16.interactive import BoundaryParked

    yields = []

    class _ParkingCPU(_FakeCPU):
        def __init__(self):
            super().__init__(return_after=30_000)
            self._parks = 3

        def run(self, n):
            if self._parks:
                self._parks -= 1
                raise BoundaryParked(0x0E99, 0x4A0D, 0x4A10)
            return super().run(n)

    cpu = _ParkingCPU()
    ax, dx = call_far(cpu, 0x60, 0x0100, 0x2930, [], max_steps=None,
                      yield_check=lambda: yields.append(1))
    assert (ax, dx) == (0x1234, 0x5678)          # callback completed normally
    assert cpu.win16_callback_frames == []       # clean far-return
    assert len(yields) >= 3                      # each park refreshed the clock


def test_call_wndproc_honours_system_callback_budget(monkeypatch):
    """EVERY callback dispatch site must honour the SYSTEM's budget policy
    (callback_max_steps: None under an interactive driver, capped headless) —
    not call_far's hard default.  Found live: a title screen polling
    GetAsyncKeyState/GetCursorPos for a click INSIDE its wndproc was killed
    at 20M steps as a 'runaway' (CallbackOverrun) although the interactive
    driver had already declared the wait legitimate (callback_max_steps=None,
    exactly as the TimerProc branch honoured all along)."""
    import win16.callback as callback_mod
    from win16.api.system import Win16System

    seen = {}

    def fake_call_far(cpu, thunk_seg, seg, off, args, *, max_steps="MISSING",
                      yield_check=None):
        seen["max_steps"] = max_steps
        seen["yield_check"] = yield_check
        return 0x1111, 0x2222

    monkeypatch.setattr(callback_mod, "call_far", fake_call_far)

    def _yield():
        pass

    fake_sys = SimpleNamespace(machine=SimpleNamespace(cpu=object()),
                               callback_max_steps=None,     # interactive policy
                               yield_check=_yield)
    window = SimpleNamespace(handle=0x30,
                             wndclass=SimpleNamespace(wndproc=(0x0100, 0x2930)))
    result = Win16System.call_wndproc(fake_sys, window, 0x0201, 1, 0x00640064)
    assert seen["max_steps"] is None, \
        "call_wndproc ignored the system's callback budget policy"
    assert seen["yield_check"] is _yield
    assert result == (0x2222 << 16) | 0x1111


def test_dialog_proc_honours_system_callback_budget(monkeypatch):
    """The dialog-proc dispatcher is a callback site like any other: same
    policy seam (a dialog proc may legitimately wait on the user)."""
    import win16.callback as callback_mod
    from win16.api import dialogs as dialogs_mod

    seen = {}

    def fake_call_far(cpu, thunk_seg, seg, off, args, *, max_steps="MISSING",
                      yield_check=None):
        seen["max_steps"] = max_steps
        return 1, 0

    monkeypatch.setattr(callback_mod, "call_far", fake_call_far)

    fake_sys = SimpleNamespace(machine=SimpleNamespace(cpu=object()),
                               callback_max_steps=None,
                               yield_check=None)
    monkeypatch.setattr(dialogs_mod, "_sys", lambda ctx: fake_sys)
    dlg = SimpleNamespace(handle=0x40, proc=(0x0100, 0x1000))
    assert dialogs_mod._call_proc(object(), dlg, 0x0110, 0, 0) == 1
    assert seen["max_steps"] is None, \
        "_call_proc ignored the system's callback budget policy"


def test_frame_preserved_when_vm_stops_mid_callback():
    # If the VM stops mid-callback (a gap/halt propagating), call_far must LEAVE
    # the frame on win16_callback_frames so a crash snapshot records the in-flight
    # callback and can resume it (else its later far-return orphans -> the
    # OrphanReturnError seen when resuming a mid-callback crash snapshot).
    from win16.callback import call_far

    class _Stop(Exception):
        pass

    class _StoppingCPU(_FakeCPU):
        def run(self, n):
            raise _Stop()                        # VM stops immediately, mid-callback

    cpu = _StoppingCPU(return_after=1)
    with pytest.raises(_Stop):
        call_far(cpu, 0x60, 0x0100, 0x2440, [], max_steps=None)
    assert len(cpu.win16_callback_frames) == 1   # frame kept for the crash snapshot


def test_yield_check_progress_rearms_the_budget():
    """The cap is a NO-PROGRESS detector, not a length limit.  A modal loop
    that runs its own message pump resides for an unbounded number of steps
    while still servicing input; a host whose `yield_check` reports progress
    (returns truthy) must not have such a callback aborted.  Found on a
    recorded SimAnt session: a dialog the player sat in for 20M+ instructions,
    steadily consuming recorded mouse input, tripped the fixed budget 11
    instructions before the next recorded arrival."""
    from win16.callback import call_far
    cpu = _FakeCPU(return_after=50_000)          # far beyond the 10k budget

    def yield_check():
        return True                              # progress every chunk

    ax, dx = call_far(cpu, 0x60, 0x0100, 0x2440, [], max_steps=10_000,
                      yield_check=yield_check)
    assert (ax, dx) == (0x1234, 0x5678)


def test_yield_check_without_progress_still_overruns():
    """The detector must keep firing for the case worth catching: a callback
    burning the whole budget with no external progress at all."""
    from win16.callback import CallbackOverrun, call_far
    cpu = _FakeCPU(return_after=50_000)
    calls = []

    def yield_check():
        calls.append(1)
        return False                             # no progress, ever

    with pytest.raises(CallbackOverrun, match="made no progress"):
        call_far(cpu, 0x60, 0x0100, 0x2440, [], max_steps=10_000,
                 yield_check=yield_check)
    assert calls                                 # the hook really ran


def test_demo_driver_yield_reports_arrival_progress(tmp_path):
    """DemoDriver._on_yield is the replay's progress signal: truthy exactly
    when the recorded timeline advanced (an arrival was injected)."""
    import json
    from win16.demo import DemoDriver

    path = tmp_path / "d.jsonl"
    recs = [{"kind": "win16-demo", "version": 4, "exe": "X.EXE",
             "snapshot": None, "instruction": 0},
            {"t": "i", "i": 100, "v": [1, 0x0200, 0, 0, 5, 0]},
            {"t": "i", "i": 900, "v": [1, 0x0200, 0, 0, 9, 0]}]
    path.write_text("\n".join(json.dumps(r) for r in recs), encoding="ascii")

    driver = DemoDriver(path)
    noted = []
    cpu = SimpleNamespace(instruction_count=0)
    driver.sys = SimpleNamespace(machine=SimpleNamespace(cpu=cpu),
                                 msg_queue=[], _note_input=noted.append,
                                 quit_posted=None)

    assert driver._on_yield() is False           # nothing due yet
    cpu.instruction_count = 500
    assert driver._on_yield() is True            # first arrival injected
    assert driver._on_yield() is False           # nothing new due
    cpu.instruction_count = 1000
    assert driver._on_yield() is True            # second arrival injected
