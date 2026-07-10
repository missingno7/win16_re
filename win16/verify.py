"""Differential verification of replacement hooks on a Win16 machine.

dos_re's `HookVerifier` proves a Python hook byte-exact by cloning the runtime,
re-interpreting the ORIGINAL ASM from the same pre-state to the hook's own
continuation, and diffing full CPU state + memory.  It clones a DOS VM
(CPU + Memory + DOSMachine).  A Win16 machine carries more: **the operating
system is a Python object graph** (windows, DCs, menus, the selector heap),
and an API call made by the ASM-oracle side must mutate the CLONE's OS, never
the live machine's.  This module supplies that cloner and wires it in
(`HookVerifierConfig.clone_runtime`, dos_re >= 3c403a6).

    from win16.verify import install_lift_verifier
    verifier = install_lift_verifier(machine, create_machine, hooks={(cs, ip)})
    machine.cpu.run(...)          # every call of a hooked function is verified
    verifier.counts[(cs, ip)]     # how many calls were proven byte-exact

Scope and honesty:

* **What is compared** is what dos_re compares: registers, flags, and the full
  4 MB VM memory image.  Divergence in the *Python* OS graph (a window the hook
  forgot to invalidate) is NOT compared — no more than dos_re compares DOSMachine
  state.  Memory is the contract; the OS graph is verified by the demo/snapshot
  digest baseline instead.
* **A clone is expensive** (fresh loader + a copy of memory + a pickle round-trip
  of the OS graph), and each verified call needs two.  Sample a handful of calls
  per function, exactly as `dos_re/tools/liftverify.py` does — this is a
  per-slice tool, not a whole-census sweep.
* **The drive matters.**  Free-running a Win16 snapshot is not a terminating
  workload (the message loop waits for input, and a modal wndproc loop waits
  forever).  Verify over a **demo replay** — the deterministic baseline the
  ports already record.

This layer never learns a game: `machine_factory` is the game adapter's
`create_machine`, passed in by the caller.
"""
from __future__ import annotations

import pickle
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from dos_re.cpu import CPUState
from dos_re.verification import HookVerifierConfig, install_hook_verifier

#: Host wiring that must never be cloned (bound methods of the interactive
#: driver, which owns an unpicklable threading lock).  Same list vmsnap detaches.
_HOST_ATTRS = ("machine", "message_source", "input_drainer", "yield_check")


@dataclass
class _Program:
    """The `.program.memory` shape dos_re's verifier expects of a runtime."""
    memory: Any


@dataclass
class Win16Runtime:
    """Duck-typed dos_re `Runtime` over a `Win16Machine`.

    The verifier only ever touches `.cpu` and `.program.memory`; `.machine` is
    ours, so the cloner can reach the OS graph.
    """
    program: _Program
    cpu: Any
    machine: Any

    @classmethod
    def of(cls, machine) -> "Win16Runtime":
        return cls(_Program(machine.mem), machine.cpu, machine)


def clone_machine(machine, machine_factory: Callable[[], Any]):
    """Return a detached copy of `machine`: VM memory, CPU state, and the OS
    object graph — with every API service re-bound to the clone.

    Mirrors `vmsnap.load_snapshot` without going through the filesystem.  The
    fresh machine from `machine_factory()` supplies a correctly wired loader,
    INT dispatch and API registry; we then overlay the live state onto it.
    """
    from win16.callback import _install_return_hook
    from win16.hugeheap import descriptor
    from win16.loader import THUNK_SEG

    src_sys = machine.api.services["system"]
    clone = machine_factory()

    clone.mem.data[:] = machine.mem.data
    clone.cpu.s = CPUState(**{f: getattr(machine.cpu.s, f)
                              for f in machine.cpu.s.__slots__})
    clone.cpu.instruction_count = machine.cpu.instruction_count
    clone.cpu.halted = machine.cpu.halted
    clone.cpu.trace_enabled = False
    clone.free_para = machine.free_para

    # Callback frames the clone still owes (a hook may be running inside a
    # WndProc/TimerProc dispatched from the API layer).
    clone.cpu.win16_orphan_frames = (
        list(getattr(machine.cpu, "win16_orphan_frames", []))
        + list(getattr(machine.cpu, "win16_callback_frames", [])))
    _install_return_hook(clone.cpu, THUNK_SEG)

    # The OS object graph.  Detach host wiring exactly as save_snapshot does,
    # deep-copy by pickle round-trip, then re-bind to the CLONE.
    saved = {a: getattr(src_sys, a, None) for a in _HOST_ATTRS}
    for a in _HOST_ATTRS:
        setattr(src_sys, a, None)
    try:
        clone_sys = pickle.loads(pickle.dumps(src_sys))
    finally:
        for a, v in saved.items():
            setattr(src_sys, a, v)
    clone_sys.machine = clone
    clone.api.services["system"] = clone_sys

    # An ASM-oracle re-run is headless: it must never block on a message source
    # or spin in a GetTickCount busy-wait waiting for a driver that isn't there.
    clone_sys.interactive = False
    clone_sys.clock_floor_anchor = (machine.cpu.instruction_count, clone_sys.clock_ms)

    # Re-wire the selector heap: VM Memory must consult the CLONE's map (the
    # pickle copied it; the fresh boot's dict knows nothing of the allocations).
    if clone_sys.huge_heap is not None:
        hh = clone_sys.huge_heap
        rekeyed = {descriptor(k): v for k, v in hh.sel_base.items()}
        hh.sel_base.clear()
        hh.sel_base.update(rekeyed)
        clone.mem.sel_base = hh.sel_base
        clone.mem.sel_min = hh.first_selector & 0xFFFC

    for key in ("async_keys", "async_keys_tapped"):
        if key in machine.api.services:
            clone.api.services[key] = set(machine.api.services[key])

    # Hook tables.  GAME-CODE hooks (islands, lifts) are pure functions of a CPU
    # and port over.  API/callback hooks in the thunk segment must NOT: each
    # closes over the machine that owns it, so copying the live ones would make
    # the clone's Windows calls read and mutate the LIVE window graph.  The
    # clone's own — installed by `machine_factory()` and bound to the clone —
    # stay exactly where they are.
    for key, hook in machine.cpu.replacement_hooks.items():
        if key[0] != THUNK_SEG:
            clone.cpu.replacement_hooks[key] = hook
    for key, name in machine.cpu.hook_names.items():
        if key[0] != THUNK_SEG:
            clone.cpu.hook_names[key] = name
    clone.cpu.hook_verifier_passthrough = set(
        getattr(machine.cpu, "hook_verifier_passthrough", ()))
    clone.cpu.hook_verifier_live_passthrough_overrides = dict(
        getattr(machine.cpu, "hook_verifier_live_passthrough_overrides", {}))
    clone.cpu.hook_verifier_verify_nested_calls = getattr(
        machine.cpu, "hook_verifier_verify_nested_calls", True)
    clone.cpu.hook_verifier = None
    return clone


def install_lift_verifier(machine, machine_factory: Callable[[], Any], *,
                          hooks: Iterable[tuple[int, int]],
                          asm_wall_timeout_s: float = 20.0,
                          asm_max_steps: int = 1_000_000):
    """Attach dos_re's strict differential verifier to a Win16 machine.

    `hooks` are the (cs, ip) entries to verify; everything else installed on the
    machine keeps running unverified.  Returns the `HookVerifier` (its `.counts`
    is the per-hook proof tally).

    The API thunks are registered as **passthrough**: they are the operating
    system, not a Python stand-in for game ASM.  Nothing behind them but an INT3
    tripwire, so they must neither be verified (there is no oracle) nor cleared
    from the ASM side (the oracle would execute the tripwire).
    """
    from win16.loader import THUNK_SEG

    rt = Win16Runtime.of(machine)

    def clone_runtime(src: Win16Runtime) -> Win16Runtime:
        return Win16Runtime.of(clone_machine(src.machine, machine_factory))

    machine.cpu.hook_verifier_passthrough = {
        key for key in machine.cpu.replacement_hooks if key[0] == THUNK_SEG}

    config = HookVerifierConfig.strict(
        hooks=set(hooks),
        asm_max_steps=asm_max_steps,
        asm_wall_timeout_s=asm_wall_timeout_s,
        clone_runtime=clone_runtime,
        asm_keeps_passthrough_hooks=True,
    )
    return install_hook_verifier(rt, config, stops={})
