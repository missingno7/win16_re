"""The CPU-FREE Win16 host: boot a data-only image and service the Windows API
for a **CPUless recovered corpus** (DOS_RE 2.0 stage 3, the standalone wall).

``dos_re.lift.standalone`` gives every port the same four things (the import
wall, ``run_recovered``, ``FailLoudPlatform``, ``run_deep``).  What it cannot
give a *Win16* port is the platform itself: a promoted body reaches Windows
through ``plat.farcall(seg, off, regs, argbytes, cost)``, and win16's API layer
was CPU-COUPLED — :meth:`ApiRegistry._invoke` read the pascal arguments off the
emulated stack *via ``cpu``* and finished with ``ret_far(cpu, ...)``.  A CPUless
host has no ``cpu``.  This module closes that, generically:

* :class:`CpuFreeCarrier` — what the API handlers see as ``ctx.cpu``: memory, a
  register record, the hook tables, a virtual clock.  It executes NOTHING.  Any
  attempt to *run guest code* through it (``step``/``run``/a callback
  dispatch) raises :class:`CpuFreeExecutionAttempt` — the honest boundary,
  never a synthesised return value.
* :func:`load_cpuless_image` — the EXE-free boot, loader-free: it reconstructs
  the machine record + API registry + OS object graph from the boot image
  without importing ``win16.loader`` (which builds a ``CPU8086`` at module
  level and is therefore forbidden behind the wall).
* :class:`Win16CpulessPlatform` — the ``plat`` contract over that host.
  ``farcall`` resolves a thunk slot to its ``(module, ordinal)``, decodes the
  pascal arguments the recovered body already pushed (from ``mem`` at
  ``ss:sp`` — values, no CPU), and services it through the CPU-free
  :meth:`ApiRegistry.invoke_values`.  ``intr`` applies the register bundle to
  the carrier and runs the machine's own INT surface.

WHAT IS NOT HERE, deliberately.  A Win16 API that CALLS BACK into guest code —
a window proc, a dialog proc, an enum proc, a timer proc, all of which the VM
path services with ``win16.callback.call_far`` — cannot be serviced by a host
that owns no execution engine.  Under the CPUless wall those callbacks must
dispatch into the *recovered corpus* instead.  That seam is a real, named
frontier item: reaching one raises :class:`CpuFreeExecutionAttempt` with the
API and the callback target, so it shows up as a work item rather than as a
silent wrong answer.
"""
from __future__ import annotations

import hashlib
import json
import pickle
from pathlib import Path

from dos_re.lift.standalone import FailLoudPlatform
from dos_re.x86 import CPUState

from .api.core import Win16ApiGap, api_name
from .machine import BOOT_MANIFEST_SCHEMA, THUNK_SEG, Win16Machine


class CpuFreeExecutionAttempt(RuntimeError):
    """The CPU-free host was asked to EXECUTE guest code — the one thing it
    cannot do.  A structured frontier witness (which API, which target), never
    a fallback to an interpreter and never a faked result."""


class CpuFreeCarrier:
    """Memory + registers + hook tables, with no execution engine.

    The API handlers are written against a duck-typed ``cpu``: they read and
    write ``.s`` (registers) and ``.mem``, log through ``.instruction_count``,
    and mint thunks into ``.replacement_hooks`` / ``.hook_names``.  All of that
    is *state*, and state is exactly what a CPUless host legitimately owns (see
    ``lint_cpuless``: ``CPUState`` is a VALUE record; what must never be held
    is something that RUNS).  The executing members are therefore absent by
    construction: naming one raises."""

    #: attributes that would MEAN execution — a CPU8086 has them, this has not
    _EXECUTING = ("step", "run", "interpret_current_instruction",
                  "interpret_current_instruction_without_hook", "call_far",
                  "emulate_call", "emulate_int")

    def __init__(self, mem, state: CPUState):
        self.mem = mem
        self.s = state
        self.instruction_count = 0
        self.replacement_hooks: dict = {}
        self.hook_names: dict = {}
        self.halted = False
        self.code_poisoned = True
        self.win16_current_api = ("?", 0)
        self.win16_orphan_frames: list = []
        self.trace_enabled = False

    def __getattr__(self, name):
        if name in CpuFreeCarrier._EXECUTING:
            raise CpuFreeExecutionAttempt(
                f"the CPU-free Win16 host was asked for {name!r}: servicing "
                f"this API needs guest EXECUTION (a callback into game code). "
                f"Under the CPUless wall that call must dispatch into the "
                f"recovered corpus; there is no interpreter to fall back to.")
        raise AttributeError(name)


def load_cpuless_image(boot_dir: str | Path, registry_factory, *,
                       game_root: str | Path | None = None):
    """Reconstruct a Win16 machine from a data-only boot image with a CPU-FREE
    carrier in place of the interpreter.  Returns ``(machine, manifest)``.

    The loader-free twin of :func:`win16.bootimage.load_boot_image`: same image,
    same integrity gates (schema, memory hash, EXE-free program identity, the
    registry-drift equate cross-check), but it imports neither ``win16.loader``
    nor ``dos_re.cpu``, so it runs behind the CPUless import wall."""
    from dos_re.independence import VMlessViolation
    from dos_re.memory import Memory
    from .vmsnap import restore_machine_state

    boot = Path(boot_dir)
    manifest = json.loads((boot / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != BOOT_MANIFEST_SCHEMA:
        raise VMlessViolation(
            f"unrecognized boot image schema in {boot} "
            f"(want {BOOT_MANIFEST_SCHEMA!r}, got {manifest.get('schema')!r})")
    meta = json.loads((boot / "state.json").read_text(encoding="utf-8"))

    image = (boot / manifest["artifacts"]["memory"]).read_bytes()
    got = hashlib.sha256(image).hexdigest()
    if got != manifest["memory_sha256"]:
        raise VMlessViolation(
            f"boot image memory hash mismatch: {got[:16]} != "
            f"{manifest['memory_sha256'][:16]} — image corrupted or stale")

    program = pickle.loads((boot / manifest["artifacts"]["program"]).read_bytes())
    if getattr(program, "raw", b""):
        raise VMlessViolation(
            "boot image program identity carries raw executable bytes — "
            "not a data-only image")

    api = registry_factory()
    if api.slots:
        raise VMlessViolation(
            "registry_factory returned a registry with import slots already "
            "assigned — slots must come from the manifest alone")
    for key, val in manifest["api_equates"].items():
        mod, ordn = key.rsplit(".", 1)
        have = api.equates.get((mod, int(ordn)))
        if have != val:
            raise VMlessViolation(
                f"API equate {key} mismatch: registry {have!r} != "
                f"manifest {val!r} — the registry factory drifted from the "
                f"one the image was built with")
    for key, off in manifest["api_slots"].items():
        mod, ordn = key.rsplit(".", 1)
        api.slots[(mod, int(ordn))] = off

    mem = Memory(size=manifest["memory_size"], sel_base={})
    machine = Win16Machine(exe=program, cpu=None, mem=mem, api=api,
                           seg_bases=list(manifest["seg_bases"]),
                           free_para=meta["free_para"])
    machine.cpu = CpuFreeCarrier(mem, CPUState(**meta["cpu"]))
    api.install(machine.cpu, manifest["thunk_seg"])
    restore_machine_state(machine, boot, meta)
    if game_root is not None:
        machine.api.services["system"].file_root = Path(game_root)
    return machine, manifest


class Win16CpulessPlatform(FailLoudPlatform):
    """The ``plat`` contract for a CPUless Win16 corpus.

    Inherits ``FailLoudPlatform``'s honest defaults for ``inp``/``outp`` (a
    Win16 program reaches hardware through the OS, so a port that hits one has
    found something real) and implements the two effects SimAnt's corpus
    actually reaches: the import-thunk far-call and the raw INT surface."""

    def __init__(self, machine):
        self.machine = machine
        self.carrier = machine.cpu
        self.api = machine.api
        self.clock = 0
        self._entry = 0
        self._slot_owner: dict[int, tuple[str, int]] = {}
        for (mod, ordn), off in self.api.slots.items():
            self._slot_owner[off] = (mod, ordn)
        #: every farcall serviced this run, in order: (label, args) — the
        #: runner's evidence that the CPU-free API path really ran.
        self.farcalls: list[str] = []

    # -- helpers ----------------------------------------------------------

    def _read_pascal_args(self, ss: int, sp: int, sizes) -> tuple[int, ...]:
        """Decode the pascal args the recovered body already pushed, straight
        out of memory at ``ss:sp`` — the CPU-free twin of
        :func:`win16.api.core.read_pascal_args` (same layout, no ``cpu``)."""
        mem = self.machine.mem
        total = sum(sizes)
        args: list[int] = []
        consumed = 0
        for size in sizes:
            consumed += size
            off = (sp + 4 + total - consumed) & 0xFFFF
            if size == 2:
                args.append(mem.rw(ss, off))
            else:
                args.append(mem.rw(ss, off)
                            | (mem.rw(ss, (off + 2) & 0xFFFF) << 16))
        return tuple(args)

    def _apply(self, regs: dict, cost: int) -> None:
        """Publish the recovered body's register bundle + virtual time on the
        carrier, so a handler that reads ``ctx.cpu.s`` sees the caller's state."""
        s = self.carrier.s
        for r in ("ax", "bx", "cx", "dx", "si", "di", "bp", "ds", "es",
                  "ss", "sp"):
            if r in regs:
                setattr(s, r, regs[r] & 0xFFFF)
        s.flags = regs.get("_flags", regs.get("_flags_in", 2)) & 0xFFFF
        self.carrier.instruction_count = self._entry + cost

    def _bundle(self, regs: dict) -> dict:
        s = self.carrier.s
        out = {r: getattr(s, r) & 0xFFFF for r in
               ("ax", "bx", "cx", "dx", "si", "di", "bp", "ds", "es")}
        out["flags"] = s.flags & 0xFFFF
        out["halted"] = bool(self.carrier.halted)
        return out

    # -- the plat contract -------------------------------------------------

    def farcall(self, seg: int, off: int, regs: dict, argbytes: int,
                cost: int) -> dict:
        """A ``call far`` into the import-thunk segment: service it through the
        CPU-FREE API path (args-in / result-out).

        The recovered body owns its own stack arithmetic (it pops ``4 +
        argbytes`` itself), so nothing here performs a far return: the args are
        READ from the frame it built, the handler runs on values, and only the
        result registers come back."""
        seg &= 0xFFFF
        off &= 0xFFFF
        thunk_seg = getattr(self.api, "_thunk_seg", THUNK_SEG)
        if seg != thunk_seg:
            raise Win16ApiGap(
                f"platform far-call {seg:04X}:{off:04X} is outside the import "
                f"thunk segment {thunk_seg:04X} — no Win16 service lives there")
        owner = self._slot_owner.get(off)
        if owner is None:
            raise Win16ApiGap(
                f"platform far-call {seg:04X}:{off:04X}: no import slot is "
                f"bound at this thunk offset")
        module, ordinal = owner
        label = api_name(module, ordinal)
        entry = self.api.entries.get((module, ordinal))
        if entry is None or entry.handler is None:
            raise Win16ApiGap(f"{label} — not implemented (CPUless host)")
        if entry.raw:
            raise Win16ApiGap(
                f"{label} is a raw (-register) API: it owns its own "
                f"register/stack return contract and has no args-in/"
                f"result-out form — give it one before the CPUless corpus "
                f"can reach it")
        want = sum(entry.arg_sizes or [])
        if want != argbytes:
            raise Win16ApiGap(
                f"{label}: the recovered body's pascal cleanup ({argbytes} "
                f"bytes) disagrees with the registry's argument list ({want} "
                f"bytes) — the far-call contract is stale")
        self._apply(regs, cost)
        args = self._read_pascal_args(self.carrier.s.ss, self.carrier.s.sp,
                                      entry.arg_sizes or [])
        ax, dx = self.api.invoke_values(self.carrier, entry, label, args)
        self.farcalls.append(f"{label}{args!r}")
        if ax is not None:
            self.carrier.s.ax = ax & 0xFFFF
        if dx is not None:
            self.carrier.s.dx = dx & 0xFFFF
        out = self._bundle(regs)
        # Virtual time: the thunk dispatch itself.  A Win16 API that re-enters
        # guest code would add the callback's own instructions — under this
        # host that path raises instead (CpuFreeExecutionAttempt), so a
        # serviced call is exactly one step and the cost is never guessed.
        out["cost"] = 1
        return out

    def intr(self, num: int, regs: dict, cost: int) -> dict:
        """The raw INT surface (INT 21h file/DOS services, INT 2Fh multiplex):
        apply the bundle to the carrier and run the machine's own handler —
        the SAME service the VM path uses, which reads ``cpu.s``/``cpu.mem``
        only and therefore needs no interpreter."""
        self._apply(regs, cost)
        self.carrier.s.cs = regs.get("cs", 0) & 0xFFFF
        self.carrier.s.ip = regs.get("ip", 0) & 0xFFFF
        self.machine.interrupt(self.carrier, num & 0xFF)
        return self._bundle(regs)

    # -- recovered-root invocation ----------------------------------------

    def call(self, recovered_fn, **regs):
        """Invoke a recovered ROOT against this host and advance the virtual
        clock by the body's own reported cost (the CPUless timing contract)."""
        import inspect
        self._entry = self.clock
        # A PURE-COMPUTE body's contract is ``(mem, *regs)``: it takes no
        # ``plat`` because it reaches no platform effect.  Passing one anyway is
        # a TypeError, so the host asks the body what it takes (the same rule
        # dos_re's CPUlessPlatformRuntime.call follows).
        if "plat" in inspect.signature(recovered_fn).parameters:
            out, compat = recovered_fn(self.machine.mem, self, **regs)
        else:
            out, compat = recovered_fn(self.machine.mem, **regs)
        self.clock = self._entry + compat["cost"]
        return out, compat
