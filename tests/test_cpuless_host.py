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
  import wall (``dos_re.lift.standalone.install_import_guard``).
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

    from dos_re.lift.standalone import install_import_guard

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
