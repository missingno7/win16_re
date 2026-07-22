"""The CPU-FREE Win16 host: boot a data-only image and service the Windows API
for a **CPUless recovered corpus** (implementations with the ``generated-cpuless``
recovery level, run without any CPU carrier).

Under dos_re 3.0 the import wall lives in ``dos_re.detachment_guard`` and a
reached-but-unimplemented target raises ``dos_re.runtime_miss
.RuntimeExecutionFrontier``.  What dos_re cannot give a *Win16* port is the
platform itself: a promoted body reaches Windows through
``plat.farcall(seg, off, regs, argbytes, cost)``, and win16's API layer
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

* :meth:`Win16CpulessPlatform.callback` + :func:`install_callback_dispatch` —
  the OTHER direction.  A Win16 API that calls back into guest code (a window
  proc, a dialog proc, a timer proc) goes through ``win16.callback.call_far``,
  which under the VM pushes a frame and runs the interpreter to a sentinel.  A
  CPUless host has nothing to run, so it dispatches into the *recovered corpus*
  instead: same pascal frame in memory (the body's ``[bp+6]`` is the original
  compiler's and cannot be renegotiated), same ``(AX, DX)`` back.  Which CS:IP
  maps to which recovered body is game knowledge, so it arrives as an injected
  resolver rather than living here.

  This is what lets anything ABOVE the message pump run without a CPU.  It is
  OPT-IN: with no resolver installed, those APIs still raise
  :class:`CpuFreeExecutionAttempt` naming the target, because a host that
  quietly skipped a WndProc would be fabricating the game's behaviour instead
  of reproducing it.
"""
from __future__ import annotations

import hashlib
import json
import pickle
from pathlib import Path

from dos_re.lift.platform import UnsupportedPlatformEffect
from dos_re.runtime_miss import RuntimeExecutionFrontier
from dos_re.x86 import CPUState

from .api.core import Win16ApiGap, api_name
from .machine import (BOOT_MANIFEST_SCHEMA, CALLBACK_RET_IP, THUNK_SEG,
                      Win16Machine)


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
    from dos_re.independence import GeneratedGraphBootstrapError
    from dos_re.memory import Memory
    from .vmsnap import restore_machine_state

    boot = Path(boot_dir)
    manifest = json.loads((boot / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != BOOT_MANIFEST_SCHEMA:
        raise GeneratedGraphBootstrapError(
            f"unrecognized boot image schema in {boot} "
            f"(want {BOOT_MANIFEST_SCHEMA!r}, got {manifest.get('schema')!r})")
    meta = json.loads((boot / "state.json").read_text(encoding="utf-8"))

    image = (boot / manifest["artifacts"]["memory"]).read_bytes()
    got = hashlib.sha256(image).hexdigest()
    if got != manifest["memory_sha256"]:
        raise GeneratedGraphBootstrapError(
            f"boot image memory hash mismatch: {got[:16]} != "
            f"{manifest['memory_sha256'][:16]} — image corrupted or stale")

    program = pickle.loads((boot / manifest["artifacts"]["program"]).read_bytes())
    if getattr(program, "raw", b""):
        raise GeneratedGraphBootstrapError(
            "boot image program identity carries raw executable bytes — "
            "not a data-only image")

    api = registry_factory()
    if api.slots:
        raise GeneratedGraphBootstrapError(
            "registry_factory returned a registry with import slots already "
            "assigned — slots must come from the manifest alone")
    for key, val in manifest["api_equates"].items():
        mod, ordn = key.rsplit(".", 1)
        have = api.equates.get((mod, int(ordn)))
        if have != val:
            raise GeneratedGraphBootstrapError(
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


class Win16CpulessPlatform:
    """The ``plat`` contract for a CPUless Win16 corpus.

    Carries its own honest defaults for ``inp``/``outp``/``ivec`` (a Win16
    program reaches hardware through the OS, so a corpus that hits one has
    found something real — it raises ``UnsupportedPlatformEffect`` with a
    witness, never a silent no-op) and implements the effects a Win16 corpus
    actually reaches: the import-thunk far-call and the raw INT surface."""

    def inp(self, port, width, cost):
        raise UnsupportedPlatformEffect(
            f"IN from port {port & 0xFFFF:#06x} with no host platform "
            f"implementation")

    def outp(self, port, value, width, cost):
        raise UnsupportedPlatformEffect(
            f"OUT to port {port & 0xFFFF:#06x} with no host platform "
            f"implementation")

    def ivec(self, key, cost, regs):
        # "Not mine": the caller raises its own frontier witness naming the
        # vector — an unmodelled external handler stays LOUD.
        return None

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
        #: (seg, off) -> recovered callable, or None if the corpus has no body
        #: for that address.  Game-supplied: which CS:IP maps to which recovered
        #: function is exactly the knowledge this layer must not hold.
        self.callback_resolver = None
        #: every guest callback dispatched this run, in order — the evidence
        #: that host->guest re-entry really ran with no interpreter.
        self.callbacks: list[str] = []

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

    # -- the callback seam: host -> guest, with no interpreter -------------

    def callback(self, seg: int, off: int, args: list[int]) -> tuple[int, int]:
        """Call GUEST code at ``seg:off`` with 16-bit pascal args — the CPU-free
        twin of :func:`win16.callback.call_far`.

        This is the mirror of :meth:`farcall`.  ``farcall`` is guest->host: the
        body built a pascal frame and the host reads it.  This is host->guest:
        the host builds the frame and the recovered body reads it.  Both keep
        the SAME memory layout, because the recovered body's ``[bp+6]`` operand
        is the original compiler's and cannot be renegotiated:

            ss:sp ->  far return address (4 bytes)   <- sp on entry
                      arg[n-1] .. arg[0]             <- pushed in pascal order

        The body owns its own stack arithmetic: its epilogue's ``retf N`` pops
        the return address *and* the arguments, so ``sp`` must come back to
        where it started.  That is checked, not assumed — a mismatch means the
        argument contract is wrong and is a defect, not a rounding error.

        The far return address is a SENTINEL that is never executed.  Under the
        CPU path it is the address the interpreter runs back to; here there is
        nothing to run, and control returns by the Python call itself.  It is
        pushed only so the frame offsets match.
        """
        resolve = self.callback_resolver
        if resolve is None:
            raise CpuFreeExecutionAttempt(
                f"callback into guest code at {seg:04X}:{off:04X}, but no "
                f"callback resolver is installed on this CPU-free host — "
                f"install one (install_callback_dispatch) so the call can "
                f"dispatch into the recovered corpus")
        fn = resolve(seg, off)
        if fn is None:
            raise CpuFreeExecutionAttempt(
                f"callback into guest code at {seg:04X}:{off:04X}: no recovered "
                f"body is promoted for that address — the corpus cannot service "
                f"this callback and there is no interpreter to fall back to")

        s = self.carrier.s
        mem = self.machine.mem
        saved = {r: getattr(s, r) & 0xFFFF for r in
                 ("cs", "ip", "sp", "ss", "bp", "ds", "es")}

        def push(word: int) -> None:
            s.sp = (s.sp - 2) & 0xFFFF
            mem.ww(s.ss, s.sp, word & 0xFFFF)

        for w in args:
            push(w)
        push(THUNK_SEG)                 # sentinel far return address, never run
        push(CALLBACK_RET_IP)

        regs = {r: getattr(s, r) & 0xFFFF for r in
                ("ax", "bx", "cx", "dx", "si", "di", "bp", "ds", "es", "ss",
                 "sp")}
        regs["cs"], regs["ip"] = seg & 0xFFFF, off & 0xFFFF
        out, _compat = self.call(fn, **self._accepted(fn, regs))
        self._apply(out, 0)

        if (s.sp & 0xFFFF) != saved["sp"]:
            raise CpuFreeExecutionAttempt(
                f"recovered callback {seg:04X}:{off:04X} returned with SP "
                f"{s.sp & 0xFFFF:04X} != {saved['sp']:04X} — the body's pascal "
                f"cleanup disagrees with the {len(args)} argument word(s) the "
                f"host pushed")
        ax, dx = s.ax & 0xFFFF, s.dx & 0xFFFF
        s.cs, s.ip = saved["cs"], saved["ip"]
        self.callbacks.append(f"{seg:04X}:{off:04X}{tuple(args)!r}")
        return ax, dx

    @staticmethod
    def _accepted(fn, regs: dict) -> dict:
        """Narrow a full register bundle to the parameters ``fn`` declares.

        Every recovered body takes exactly the registers its own contract reads
        (``dos_re``'s emitter derives that per function), so passing the whole
        bundle is a ``TypeError``.  Asking the callable itself keeps this free of
        any knowledge of how a particular corpus records its contracts."""
        import inspect
        params = inspect.signature(fn).parameters
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return dict(regs)
        return {k: v for k, v in regs.items() if k in params}

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


def install_callback_dispatch(platform: Win16CpulessPlatform, resolver) -> None:
    """Let the Win16 API layer re-enter GUEST code on a CPU-free host.

    ``resolver(seg, off)`` returns the recovered callable promoted for that
    address, or ``None`` if the corpus has none.  Every API that calls back into
    game code — ``DispatchMessage``/``SendMessage`` reaching a WndProc, a dialog
    proc, a ``TimerProc`` — goes through :func:`win16.callback.call_far`, which
    finds the dispatcher installed here and routes to
    :meth:`Win16CpulessPlatform.callback` instead of an interpreter.  Nothing
    else in the API layer changes: the call sites still see ``(AX, DX)``.

    Installing this is what lets anything ABOVE the message pump run without a
    CPU.  Without it those APIs raise ``CpuFreeExecutionAttempt`` — which stays
    the default, because a host that silently skipped a WndProc would be
    fabricating the game's behaviour rather than reproducing it."""
    platform.callback_resolver = resolver
    platform.carrier.win16_callback_dispatch = platform.callback


# --- TEMPORARY 3.0-migration bridge -----------------------------------------
# dos_re 3.0 deleted ``dos_re.lift.standalone`` (its selection/loading helpers
# became the declarative ImplementationCatalog).  The corpus-module naming and
# loading below is the port-side residue a Win16 host still needs until the
# catalog materializes callables at plan time (3.0 migration Phase 3, at which
# point this section is REMOVED with its callers).

def module_name(key: str) -> str:
    """Recovered module basename for a ``'CS:IP'`` key: ``'1010:5F61'`` ->
    ``'func_1010_5f61'`` (the dos_re emitter's naming convention)."""
    cs, ip = key.split(":")
    return f"func_{int(cs, 16):04x}_{int(ip, 16):04x}"


def load_recovered(package: str, key: str):
    """Import promoted recovered function ``key`` from corpus ``package``.

    A missing module is a reached execution frontier: raise the 3.0 witness
    (``RuntimeExecutionFrontier``) naming the target, never a fallback."""
    import importlib

    name = module_name(key)
    try:
        mod = importlib.import_module(f"{package}.{name}")
    except ModuleNotFoundError as exc:
        raise RuntimeExecutionFrontier(
            target_address=key,
            reason=f"no recovered module ({name}) in {package} — it (or a "
                   f"recovered callee) is on the CPUless frontier; promote it "
                   f"or bind an authored override") from exc
    return getattr(mod, name)


def run_deep(fn, *args, stack_bytes: int = 512 * 1024 * 1024,
             recursion: int = 200_000, **kwargs):
    """Run ``fn`` on a thread with a large stack + raised recursion limit so a
    BOUNDED tail-dispatch loop completes instead of dying on Python's frame
    limit.  Result and exception propagate unchanged.  (The big stack is the
    load-bearing half: raising the recursion limit alone lets CPython run past
    what the C stack holds, crashing the process instead of raising.)"""
    import sys
    import threading

    box: dict = {}

    def _target():
        sys.setrecursionlimit(recursion)
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as exc:            # noqa: BLE001 — propagated verbatim
            box["error"] = exc

    prev = threading.stack_size(stack_bytes)
    try:
        t = threading.Thread(target=_target)
        t.start()
        t.join()
    finally:
        threading.stack_size(prev)
    if "error" in box:
        raise box["error"]
    return box.get("value")
