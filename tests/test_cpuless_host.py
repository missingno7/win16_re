"""win16.cpuless — the CPU-FREE Windows API path (DOS_RE 2.0 stage 3).

Game-free.  What is asserted here is the seam a CPUless port stands on:

* :meth:`ApiRegistry.invoke_values` (args in / result out) and the CPU-driven
  ``_invoke`` (args off the emulated stack / ``ret_far``) run the SAME handler
  and produce the SAME result — the refactor is behaviour-preserving, which is
  what keeps the VMless byte-exact gate valid;
* ``plat.farcall`` on :class:`Win16CpulessPlatform` resolves an import-thunk
  slot to its API, decodes the pascal frame the recovered body pushed, and
  services it with no CPU anywhere;
* the honest boundaries: a raw (-register) API, an unbound slot, a stale
  argbytes contract, and — the big one — a service that needs to EXECUTE guest
  code all raise instead of returning a plausible value;
* the module graph of the CPU-free host is importable behind the CPUless
  import wall (``dos_re.detachment_guard.install_import_guard``).
"""
from __future__ import annotations

import types

import pytest

from dos_re.memory import Memory
from dos_re.x86 import CPUState

from win16.api.core import ApiRegistry, read_pascal_args
from win16.cpuless import (CpuFreeCarrier, CpuFreeExecutionAttempt,
                           Win16CpulessPlatform)
from win16.machine import THUNK_SEG

SS, SP = 0x2000, 0x1000


def _registry():
    api = ApiRegistry()

    @api.register("KERNEL", 22, args="word")
    def _free(ctx):
        return (ctx.args[0] + 1) & 0xFFFF

    @api.register("KERNEL", 15, "GlobalAlloc", args="word long", ret="long")
    def _size(ctx):
        return (ctx.args[1] << 4) | ctx.args[0]

    @api.register_raw("KERNEL", 91, "InitTask")
    def _raw(ctx):                                     # owns its own contract
        ctx.cpu.s.ax = 1

    api.resolve_import("KERNEL", 22)
    api.resolve_import("KERNEL", 15)
    api.resolve_import("KERNEL", 91)
    return api


def _host(api):
    """A machine record with a CPU-FREE carrier — no interpreter anywhere."""
    mem = Memory(size=0x40000, sel_base={})
    carrier = CpuFreeCarrier(mem, CPUState(ss=SS, sp=SP))
    machine = types.SimpleNamespace(mem=mem, api=api, cpu=carrier,
                                    interrupt=lambda cpu, num: None)
    api.install(carrier, THUNK_SEG)
    return machine


def _push_frame(mem, args_hi_to_lo):
    """Lay down what a recovered body pushes before a ``call far``: the pascal
    args (left to right) then the far return address."""
    sp = SP + 2 * len(args_hi_to_lo) + 4
    for word in args_hi_to_lo:                     # pushed first = highest addr
        sp -= 2
        mem.ww(SS, sp, word & 0xFFFF)
    mem.ww(SS, sp - 4, 0xBEEF)                     # ret ip
    mem.ww(SS, sp - 2, 0xCAFE)                     # ret cs
    return sp - 4


def test_invoke_values_matches_the_cpu_driven_path():
    """The two dispatch paths agree, argument for argument and bit for bit."""
    api = _registry()
    machine = _host(api)
    entry = api.entries[("KERNEL", 15)]

    # CPU-free: explicit values in, (ax, dx) out.
    ax, dx = api.invoke_values(machine.cpu, entry, "KERNEL.15", (0x0007, 0x30))

    # CPU-driven: the same call read off an emulated stack.  A CPU8086 is
    # deliberately NOT built here (this suite must stay importable behind the
    # wall in the port); the carrier stands in for it, and the code under test
    # is exactly `read_pascal_args` + `invoke_values`, which is what `_invoke`
    # composes.
    mem = machine.mem
    sp = _push_frame(mem, [0x0007, 0x0000, 0x0030])   # word, long hi, long lo
    machine.cpu.s.sp = sp
    args = read_pascal_args(machine.cpu, entry.arg_sizes)
    assert args == (0x0007, 0x0030)
    ax2, dx2 = api.invoke_values(machine.cpu, entry, "KERNEL.15", args)
    assert (ax, dx) == (ax2, dx2)
    assert (ax, dx) == (0x0307, 0x0000)               # (0x30 << 4) | 7


def test_farcall_services_a_thunk_slot_with_no_cpu():
    api = _registry()
    machine = _host(api)
    plat = Win16CpulessPlatform(machine)
    slot = api.slots[("KERNEL", 22)]

    sp = _push_frame(machine.mem, [0x0041])
    regs = {"ax": 0, "bx": 0, "cx": 0, "dx": 0, "si": 0, "di": 0, "bp": 0,
            "ds": 0, "es": 0, "ss": SS, "sp": sp, "_flags_in": 2}
    out = plat.farcall(THUNK_SEG, slot, regs, 2, cost=17)

    assert out["ax"] == 0x0042                    # the handler's return, in AX
    assert out["cost"] == 1                       # one thunk step, never guessed
    assert plat.farcalls == ["KERNEL.22(65,)"]
    # The recovered body owns its own stack arithmetic: the host performed no
    # far return, so SP is untouched by the service.
    assert machine.cpu.s.sp == sp


def test_farcall_boundaries_are_loud():
    from win16.api.core import Win16ApiGap
    api = _registry()
    machine = _host(api)
    plat = Win16CpulessPlatform(machine)

    with pytest.raises(Win16ApiGap, match="outside the import thunk segment"):
        plat.farcall(0x1234, 0, {"ss": SS, "sp": SP}, 0, 0)
    with pytest.raises(Win16ApiGap, match="no import slot is bound"):
        plat.farcall(THUNK_SEG, 0xFF00, {"ss": SS, "sp": SP}, 0, 0)
    with pytest.raises(Win16ApiGap, match="raw .-register. API"):
        plat.farcall(THUNK_SEG, api.slots[("KERNEL", 91)],
                     {"ss": SS, "sp": SP}, 0, 0)
    with pytest.raises(Win16ApiGap, match="disagrees with the registry"):
        plat.farcall(THUNK_SEG, api.slots[("KERNEL", 22)],
                     {"ss": SS, "sp": SP}, 8, 0)


def test_carrier_refuses_to_execute():
    """The one thing a CPU-free host must never do quietly."""
    carrier = CpuFreeCarrier(Memory(size=0x1000, sel_base={}), CPUState())
    for name in ("step", "run", "call_far", "emulate_int"):
        with pytest.raises(CpuFreeExecutionAttempt, match="guest EXECUTION"):
            getattr(carrier, name)
    with pytest.raises(AttributeError):            # an ordinary typo stays one
        carrier.no_such_attribute


def test_invoke_values_refuses_a_raw_api():
    api = _registry()
    machine = _host(api)
    with pytest.raises(ValueError, match="args-in/result-out"):
        api.invoke_values(machine.cpu, api.entries[("KERNEL", 91)],
                          "KERNEL.91", ())


def test_the_cpu_free_host_imports_behind_the_wall():
    """The wall's own check: importing the CPU-free host (and everything it
    pulls) must not reach the interpreter, the CPU carrier, or the VM runtime.
    Re-imports from scratch with the guard armed, then restores builtins."""
    import builtins
    import importlib
    import sys

    from dos_re.detachment_guard import install_import_guard

    names = [m for m in sys.modules
             if m == "win16" or m.startswith(("win16.", "dos_re."))]
    saved = {m: sys.modules.pop(m) for m in names}
    real_import = builtins.__import__
    try:
        install_import_guard(extra_forbidden=("win16.loader",
                                              "win16.bootimage"))
        importlib.import_module("win16.cpuless")
    finally:
        builtins.__import__ = real_import
        sys.modules.update(saved)


# --- the callback seam: host -> guest with no interpreter -------------------

def _plat(machine):
    from win16.cpuless import Win16CpulessPlatform
    plat = Win16CpulessPlatform.__new__(Win16CpulessPlatform)
    plat.machine, plat.carrier, plat.api = machine, machine.cpu, machine.api
    plat.clock = plat._entry = 0
    plat._slot_owner = {}
    plat.farcalls, plat.callbacks = [], []
    plat.callback_resolver = None
    return plat


def _wndproc(mem, *, ss=0, sp=0):
    """A stand-in recovered body with a real WndProc shape: reads its four
    pascal words off the frame the host built, cleans up `retf 8`, returns in
    AX:DX.  Frame after `push bp; mov bp,sp` is bp = sp-2, so the LAST pascal
    argument sits at [bp+6] — pascal pushes left to right."""
    lp = mem.rw(ss, (sp + 4) & 0xFFFF)          # lParam lo, pushed last
    wparam = mem.rw(ss, (sp + 8) & 0xFFFF)
    msg = mem.rw(ss, (sp + 10) & 0xFFFF)
    hwnd = mem.rw(ss, (sp + 12) & 0xFFFF)       # pushed first = highest
    out = {"ax": (hwnd + msg + wparam + lp) & 0xFFFF, "dx": 0x1234,
           "sp": (sp + 4 + 10) & 0xFFFF}        # retf 10: ret addr + 5 words
    return out, {"flags": 0, "fmask": 0, "cost": 7}


def test_callback_dispatches_into_the_recovered_corpus_with_no_cpu():
    api = _registry()
    machine = _host(api)
    plat = _plat(machine)
    from win16.cpuless import install_callback_dispatch

    install_callback_dispatch(plat, lambda seg, off: _wndproc)
    args = [0x0042, 0x0010, 0x0007, 0x0000, 0x0300]     # hwnd msg wp lp_hi lp_lo
    ax, dx = plat.callback(0x1234, 0x5678, args)

    assert (ax, dx) == ((0x42 + 0x10 + 7 + 0x300) & 0xFFFF, 0x1234)
    assert machine.cpu.s.sp & 0xFFFF == SP      # the body's retf balanced it
    assert plat.callbacks == ["1234:5678(66, 16, 7, 0, 768)"]


def test_callback_frame_matches_the_cpu_paths_byte_for_byte():
    """The layout is not a new convention: the recovered body's [bp+N] operands
    are the original compiler's.  So the frame this host builds must equal the
    one `win16.callback.call_far` pushes for the interpreter, byte for byte."""
    from win16.callback import call_far

    args = [0x0042, 0x0010, 0x0007, 0x0000, 0x0300]
    depth = 2 * len(args) + 4

    # CPU-free path: capture the frame the platform lays down.
    machine = _host(_registry())
    plat = _plat(machine)
    from win16.cpuless import install_callback_dispatch
    seen = {}

    def _capture(mem, *, ss=0, sp=0):
        seen["sp"] = sp
        seen["bytes"] = bytes(mem.rb(ss, (sp + i) & 0xFFFF)
                              for i in range(depth))
        return {"sp": (sp + depth) & 0xFFFF}, {"flags": 0, "fmask": 0,
                                               "cost": 1}

    install_callback_dispatch(plat, lambda seg, off: _capture)
    plat.callback(0x1234, 0x5678, args)

    # CPU path: the same call through call_far, stopped the moment it would
    # start interpreting.  A CPU8086 is deliberately not built (this suite stays
    # importable behind the wall); what is under test is the frame, not the run.
    class _Stop(Exception):
        pass

    m2 = _host(_registry())
    cpu2 = m2.cpu

    def _run(_n):
        raise _Stop()

    object.__setattr__(cpu2, "run", _run)       # bypasses the executing-member guard
    cpu2.win16_callback_frames = []
    with pytest.raises(_Stop):
        call_far(cpu2, THUNK_SEG, 0x1234, 0x5678, args, max_steps=None)

    sp2 = cpu2.s.sp & 0xFFFF
    frame2 = bytes(m2.mem.rb(SS, (sp2 + i) & 0xFFFF) for i in range(depth))
    assert seen["sp"] == sp2
    assert seen["bytes"] == frame2


def test_a_callback_with_no_resolver_stays_loud():
    """The default is unchanged: a host that cannot service the callback says
    so, rather than fabricating a return value for a WndProc that never ran."""
    from win16.cpuless import CpuFreeExecutionAttempt

    plat = _plat(_host(_registry()))
    with pytest.raises(CpuFreeExecutionAttempt, match="no callback resolver"):
        plat.callback(0x1234, 0x5678, [0])


def test_a_callback_the_corpus_cannot_serve_names_the_address():
    from win16.cpuless import CpuFreeExecutionAttempt, install_callback_dispatch

    plat = _plat(_host(_registry()))
    install_callback_dispatch(plat, lambda seg, off: None)
    with pytest.raises(CpuFreeExecutionAttempt, match=r"1234:5678"):
        plat.callback(0x1234, 0x5678, [0])


def test_a_body_whose_cleanup_disagrees_is_a_defect_not_a_rounding_error():
    """A recovered body that pops the wrong number of argument words has a
    wrong contract.  Silently accepting it would corrupt every later frame."""
    from win16.cpuless import CpuFreeExecutionAttempt, install_callback_dispatch

    def _wrong(mem, *, ss=0, sp=0):
        return {"sp": (sp + 4) & 0xFFFF}, {"flags": 0, "fmask": 0, "cost": 1}

    plat = _plat(_host(_registry()))
    install_callback_dispatch(plat, lambda seg, off: _wrong)
    with pytest.raises(CpuFreeExecutionAttempt, match="pascal cleanup"):
        plat.callback(0x1234, 0x5678, [0x1111, 0x2222])
