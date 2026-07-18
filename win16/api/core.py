"""API registry + Win16 pascal calling-convention mechanics.

Every imported (module, ordinal) resolves to a thunk slot; a replacement hook
at the slot's CS:IP dispatches to a registered Python handler.  Handlers own
the full call contract: read pascal args from the caller's stack, perform the
service, and far-return popping the callee-cleaned argument bytes.

Fail-loud rule: an import with no registered handler still gets a thunk slot,
but calling it raises `Win16ApiGap` naming the exact API and call site.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .ordinals import ORDINAL_NAMES, api_name

# Pascal argument sizes in bytes, keyed by the Wine-spec type names.
ARG_SIZES = {
    "word": 2, "s_word": 2,
    "long": 4, "segptr": 4, "segstr": 4, "ptr": 4, "str": 4,
}


class Win16ApiGap(RuntimeError):
    """The program called a Win16 API that has not been implemented yet."""


class StackCursor:
    """A sequential reader over the caller's argument block.

    The pascal convention pushes left-to-right, so a fixed argument list can be
    decoded positionally by :func:`read_pascal_args`.  A **cdecl** Win16 API
    (``USER.420 wsprintf``) cannot: its arguments are pushed right-to-left and
    there is no declared list at all past the format string, so the only way to
    read them is to walk UP from the first one, pulling each as the format
    directs.  That walk is the entire difference, and this is it.

    It reads out of ``mem`` at ``ss:off`` and holds no CPU, so the same cursor
    serves the interpreter and the CPU-free host — both publish the caller's
    ``ss:sp`` on the object the handler sees as ``ctx.cpu``."""

    def __init__(self, mem, ss: int, base: int) -> None:
        self.mem = mem
        self.ss = ss & 0xFFFF
        self.off = base & 0xFFFF

    def word(self) -> int:
        v = self.mem.rw(self.ss, self.off)
        self.off = (self.off + 2) & 0xFFFF
        return v

    def dword(self) -> int:
        lo = self.word()
        return lo | (self.word() << 16)


@dataclass
class CallContext:
    """What a handler sees: the CPU plus decoded pascal arguments."""
    cpu: object                     # dos_re CPU8086
    registry: "ApiRegistry"
    module: str
    ordinal: int
    name: str
    args: tuple[int, ...] = ()
    #: for a ``varargs`` (cdecl) API only: a :class:`StackCursor` positioned at
    #: the caller's first argument.  ``None`` for every other API.
    varargs: "StackCursor | None" = None

    @property
    def mem(self):
        return self.cpu.mem

    def read_string(self, segptr: int, *, limit: int = 4096) -> bytes:
        """Read a zero-terminated string at a seg:off packed far pointer."""
        seg, off = (segptr >> 16) & 0xFFFF, segptr & 0xFFFF
        out = bytearray()
        for i in range(limit):
            b = self.mem.rb(seg, (off + i) & 0xFFFF)
            if b == 0:
                return bytes(out)
            out.append(b)
        raise ValueError(f"unterminated string at {seg:04X}:{off:04X}")


def read_pascal_args(cpu, sizes: list[int]) -> tuple[int, ...]:
    """Decode pascal args (pushed left-to-right) above the far return address.

    Returns values in declaration order.  32-bit args are read as one
    little-endian dword (push hi; push lo leaves lo at the lower address).
    """
    ss, sp = cpu.s.ss & 0xFFFF, cpu.s.sp & 0xFFFF
    total = sum(sizes)
    args: list[int] = []
    consumed = 0
    for size in sizes:
        consumed += size
        off = (sp + 4 + total - consumed) & 0xFFFF
        if size == 2:
            args.append(cpu.mem.rw(ss, off))
        else:
            args.append(cpu.mem.rw(ss, off) | (cpu.mem.rw(ss, (off + 2) & 0xFFFF) << 16))
    return tuple(args)


def ret_far(cpu, pop_bytes: int, ax: int | None = None, dx: int | None = None) -> None:
    """Perform RETF pop_bytes with an optional AX / DX:AX return value."""
    s = cpu.s
    ip = cpu.mem.rw(s.ss, s.sp)
    cs = cpu.mem.rw(s.ss, (s.sp + 2) & 0xFFFF)
    s.sp = (s.sp + 4 + pop_bytes) & 0xFFFF
    s.cs, s.ip = cs & 0xFFFF, ip & 0xFFFF
    if ax is not None:
        s.ax = ax & 0xFFFF
    if dx is not None:
        s.dx = dx & 0xFFFF


#: register names an API may deliver a result in, in the order a delta is
#: reported.  A ``ret="regs"`` API writes some subset of these directly.
#:
#: ``flags`` is in the list because for ``KERNEL.102 DOS3Call`` it is a genuine
#: RESULT register: the INT 21h convention signals failure by setting carry,
#: and a delta that dropped it would report a failed DOS call as a successful
#: one on the CPU-free path.
REGS_RESULT = ("ax", "bx", "cx", "dx", "si", "di", "bp", "ds", "es", "flags")


@dataclass
class ApiEntry:
    module: str
    ordinal: int
    name: str
    handler: Callable[[CallContext], int | None] | None
    arg_sizes: list[int] | None     # None => raw handler owns the whole contract
    ret: str = "word"               # "word" (AX), "long" (DX:AX), "void", "regs"
    raw: bool = False
    #: cdecl: the CALLER pops the arguments, so the callee-cleanup byte count is
    #: genuinely 0 rather than undeclared.  Recorded because ``argbytes == 0``
    #: alone cannot distinguish "cdecl" from "no arguments" (see
    #: :func:`win16.lift.entry_argbytes`); it does not change the number.
    caller_cleanup: bool = False
    #: the handler reads further arguments itself, through ``ctx.varargs``.
    varargs: bool = False


class ApiRegistry:
    """(module, ordinal) -> handler table + thunk slot allocation."""

    SLOT_STRIDE = 4                 # bytes per thunk slot (marker bytes only)

    def __init__(self) -> None:
        self.entries: dict[tuple[str, int], ApiEntry] = {}
        self.equates: dict[tuple[str, int], int] = {}
        self.slots: dict[tuple[str, int], int] = {}      # -> thunk offset
        self.call_log: list[str] = []
        self.services: dict[str, object] = {}            # named backend objects
        # DLLs we PROVIDE (as a Python API surface, e.g. MMSYSTEM).  A provided
        # DLL both LoadLibrary's successfully AND reports as an existing file
        # (games probe for the .dll before loading it — SimAnt's music engine
        # _access()es mmsystem.dll first).  Canonical names, e.g. "MMSYSTEM.DLL".
        self.provided_dlls: set[str] = set()
        # By-NAME procs a program resolves at runtime via GetProcAddress
        # (dynamically-loaded DLLs — e.g. SimAnt's mmsystem MIDI).  Each is
        # handed back as a freshly-minted callable thunk (mint_proc_thunk).
        self.named_procs: dict[tuple[str, str], ApiEntry] = {}
        self._proc_thunks: dict[tuple[str, str], int] = {}
        self._cpu = None
        self._thunk_seg = 0
        self._next_proc_off = 0

    # -- registration -----------------------------------------------------
    def register(self, module: str, ordinal: int, name: str | None = None,
                 args: str = "", ret: str = "word", *,
                 caller_cleanup: bool = False, varargs: bool = False):
        """Decorator: register a pascal-convention handler.

        `args` is the Wine-spec argument list, e.g. "word str long".  The
        wrapper reads the args, calls the handler with a CallContext, then
        far-returns popping the argument bytes.  `ret` declares the return
        contract: "word" puts the handler's int in AX (DX untouched), "long"
        puts it in DX:AX (DX set even when the high word is 0), "void" leaves
        both registers alone, and "regs" is the MULTI-register form — the
        handler writes its result registers directly and the delta is the
        result (:meth:`invoke_regs`).

        `caller_cleanup` marks a cdecl API (the caller pops); `varargs` gives
        the handler a :class:`StackCursor` at ``ctx.varargs`` to read arguments
        the declared list does not cover.  Neither invents an argument count: a
        cdecl API still declares the pascal argument list it cleans, which is
        the empty one.
        """
        module = module.upper()
        known = ORDINAL_NAMES.get(module, {}).get(ordinal)
        if name is None:
            name = known
        if known is not None and name != known:
            raise ValueError(f"{module}.{ordinal} is {known}, not {name}")
        if ret not in ("word", "long", "void", "regs"):
            raise ValueError(f"bad ret spec {ret!r}")
        sizes = [ARG_SIZES[a] for a in args.split()] if args else []

        def deco(fn):
            key = (module, ordinal)
            if key in self.entries:
                raise ValueError(f"duplicate registration for {api_name(module, ordinal)}")
            self.entries[key] = ApiEntry(module, ordinal, name or f"#{ordinal}",
                                         fn, sizes, ret=ret,
                                         caller_cleanup=caller_cleanup,
                                         varargs=varargs)
            return fn
        return deco

    def register_raw(self, module: str, ordinal: int, name: str | None = None):
        """Decorator: register a -register handler owning the full contract
        (register-based args, its own return mechanics)."""
        module = module.upper()

        def deco(fn):
            key = (module, ordinal)
            if key in self.entries:
                raise ValueError(f"duplicate registration for {api_name(module, ordinal)}")
            self.entries[key] = ApiEntry(module, ordinal,
                                         name or ORDINAL_NAMES.get(module, {}).get(ordinal, f"#{ordinal}"),
                                         fn, None, raw=True)
            return fn
        return deco

    def register_equate(self, module: str, ordinal: int, value: int) -> None:
        self.equates[(module.upper(), ordinal)] = value & 0xFFFF

    def register_proc(self, module: str, name: str, args: str = "", ret: str = "word"):
        """Decorator: register a proc resolvable by NAME at runtime (via
        GetProcAddress on a dynamically-loaded DLL).  Same pascal-arg contract
        as register(); the handler is reached through a minted callable thunk."""
        module = module.upper()
        sizes = [ARG_SIZES[a] for a in args.split()] if args else []

        def deco(fn):
            self.named_procs[(module, name)] = ApiEntry(module, 0, name, fn, sizes, ret=ret)
            return fn
        return deco

    def mint_proc_thunk(self, module: str, name: str) -> int:
        """A far pointer (seg<<16 | off) to a thunk that dispatches to the named
        proc, or 0 if we don't implement it (GetProcAddress returns NULL, and a
        well-behaved program falls back).  Idempotent per (module, name)."""
        key = (module.upper(), name)
        if key not in self.named_procs:
            return 0
        off = self._proc_thunks.get(key)
        if off is None:
            off = self._next_proc_off
            self._next_proc_off += self.SLOT_STRIDE
            for i in range(self.SLOT_STRIDE):
                self._cpu.mem.wb(self._thunk_seg, off + i, 0xCC)   # INT3 tripwire
            self._cpu.replacement_hooks[(self._thunk_seg, off)] = \
                self._make_named_dispatch(key)
            self._cpu.hook_names[(self._thunk_seg, off)] = f"proc:{key[0]}.{name}"
            self._proc_thunks[key] = off
        return (self._thunk_seg << 16) | off

    # -- import resolution (loader-facing) ---------------------------------
    def resolve_import(self, module: str, ordinal: int):
        """-> ("equate", value) or ("thunk", slot_offset)."""
        key = (module.upper(), ordinal)
        if key in self.equates:
            return ("equate", self.equates[key])
        if key not in self.slots:
            self.slots[key] = len(self.slots) * self.SLOT_STRIDE
        return ("thunk", self.slots[key])

    # -- runtime dispatch ---------------------------------------------------
    def install(self, cpu, thunk_seg: int) -> None:
        """Register a replacement hook at every allocated thunk slot."""
        self._cpu = cpu
        self._thunk_seg = thunk_seg
        # Runtime-minted GetProcAddress thunks live past the static import slots.
        self._next_proc_off = len(self.slots) * self.SLOT_STRIDE
        for (module, ordinal), offset in self.slots.items():
            label = api_name(module, ordinal)
            cpu.replacement_hooks[(thunk_seg, offset)] = self._make_dispatch(module, ordinal)
            cpu.hook_names[(thunk_seg, offset)] = f"api:{label}"

    def _gap(self, cpu, label: str):
        ret_ip = cpu.mem.rw(cpu.s.ss, cpu.s.sp)
        ret_cs = cpu.mem.rw(cpu.s.ss, (cpu.s.sp + 2) & 0xFFFF)
        raise Win16ApiGap(
            f"{label} called from {ret_cs:04X}:{ret_ip:04X} — not implemented")

    def invoke_values(self, carrier, entry: ApiEntry, label: str,
                      args: tuple[int, ...]) -> tuple[int | None, int | None]:
        """The CPU-FREE invocation path: **explicit args in, explicit result
        out**, as ``(ax, dx)`` (either may be ``None`` for a ``void`` API).

        Reads nothing off an emulated stack and performs no return mechanics,
        so a caller that has no CPU — the CPUless standalone runtime, where a
        recovered body reaches Win16 through ``plat.farcall`` — can service an
        API by handing over the argument VALUES.  ``carrier`` is whatever
        object the handlers see as ``ctx.cpu``: under the VM the real CPU8086,
        under the CPUless runtime a memory + register carrier that executes
        nothing (:class:`win16.cpuless.CpuFreeCarrier`).

        ``_invoke`` below is the thin CPU shim over this: it decodes the
        pascal args off ``ss:sp`` and applies ``ret_far`` to the result, so
        both paths run the SAME handler with the same contract."""
        if entry.ret == "regs":
            raise ValueError(
                f"{label}: a 'regs' API returns a MULTI-register bundle, which "
                f"does not fit (ax, dx) — call invoke_regs instead")
        result = self._run_handler(carrier, entry, label, args)
        ax = dx = None
        if entry.ret == "void":
            if result is not None:
                raise ValueError(f"{label}: void API returned {result!r}")
        elif result is None:
            raise ValueError(f"{label}: {entry.ret} API returned None")
        elif entry.ret == "word":
            ax = result & 0xFFFF
        else:  # long
            ax, dx = result & 0xFFFF, (result >> 16) & 0xFFFF
        return ax, dx

    def invoke_regs(self, carrier, entry: ApiEntry, label: str,
                    args: tuple[int, ...]) -> dict[str, int]:
        """The CPU-FREE invocation path for a MULTI-register API.

        A few Win16 APIs deliver their result in more registers than ``AX`` or
        ``DX:AX``: ``KERNEL.91 InitTask`` returns a whole bundle (AX/BX/CX/DX/
        SI/DI/ES), and ``KERNEL.102 DOS3Call`` is an INT 21h passthrough whose
        result is whatever registers the DOS service wrote.  Neither fits
        :meth:`invoke_values`' ``(ax, dx)``, and neither should be forced to —
        so they get their own primitive rather than a widened one.  Every
        existing API's ``(ax, dx)`` contract is therefore exactly what it was.

        The handler writes the result registers on ``ctx.cpu.s`` directly, as it
        always has; what is returned is the DELTA — ``{reg: value}`` for each of
        :data:`REGS_RESULT` the handler changed.  Under the interpreter the
        carrier IS the CPU so the writes have already landed and the delta is
        redundant (applying it is a no-op); under a CPU-free host the delta is
        the entire result, and the same handler produces both.

        Return mechanics stay with the caller, exactly as in
        :meth:`invoke_values`: nothing here performs a far return."""
        if entry.ret != "regs":
            raise ValueError(
                f"{label}: not a 'regs' API (ret={entry.ret!r}) — its result "
                f"is a value, so call invoke_values")
        s = carrier.s
        before = {r: getattr(s, r) & 0xFFFF for r in REGS_RESULT}
        result = self._run_handler(carrier, entry, label, args)
        if result is not None:
            raise ValueError(
                f"{label}: a 'regs' API writes its result registers and "
                f"returns None, but it returned {result!r}")
        return {r: getattr(s, r) & 0xFFFF for r in REGS_RESULT
                if getattr(s, r) & 0xFFFF != before[r]}

    def _run_handler(self, carrier, entry: ApiEntry, label: str,
                     args: tuple[int, ...]):
        """Build the CallContext and run the handler — the part every
        invocation form shares.  Reads no stack and performs no return."""
        if entry.raw:
            raise ValueError(
                f"{label}: a raw (-register) API owns its own register/stack "
                f"return contract and has no args-in/result-out form")
        cursor = None
        if entry.varargs:
            # The caller's argument block starts above the far return address,
            # past whatever the declared pascal list already consumed.  Both
            # invocation paths publish the caller's ss:sp on the carrier, so
            # this is the same block in both.
            cursor = StackCursor(carrier.mem, carrier.s.ss & 0xFFFF,
                                 (carrier.s.sp + 4 + sum(entry.arg_sizes or []))
                                 & 0xFFFF)
        ctx = CallContext(carrier, self, entry.module, entry.ordinal,
                          entry.name, args, varargs=cursor)
        self.call_log.append(f"{label}{args!r}")
        # Publish this API frame for call_far's resumable-callback record
        # (win16/callback.py): a callback dispatched by this handler needs the
        # API's name + argbytes to complete the call if a snapshot parks inside
        # it.  Saved/restored so nested dispatches stack.
        prev_api = getattr(carrier, "win16_current_api", ("?", 0))
        carrier.win16_current_api = (entry.name, sum(entry.arg_sizes or []))
        try:
            return entry.handler(ctx)
        finally:
            carrier.win16_current_api = prev_api

    def _invoke(self, cpu, entry: ApiEntry, label: str) -> None:
        """Read pascal args, run the handler, and far-return with its result —
        shared by static-ordinal and by-name (GetProcAddress) dispatch.  The
        CPU shim over :meth:`invoke_values` / :meth:`invoke_regs`."""
        if entry.raw:
            self.call_log.append(label)
            entry.handler(CallContext(cpu, self, entry.module, entry.ordinal, entry.name))
            return
        args = read_pascal_args(cpu, entry.arg_sizes or [])
        if entry.ret == "regs":
            # The handler wrote the result registers on cpu.s itself, so
            # applying the delta here is a no-op — it is applied anyway rather
            # than skipped, so this path and the CPU-free one stay the same
            # sequence of steps and cannot drift apart.
            for reg, val in self.invoke_regs(cpu, entry, label, args).items():
                setattr(cpu.s, reg, val & 0xFFFF)
            ret_far(cpu, sum(entry.arg_sizes or []))
            return
        ax, dx = self.invoke_values(cpu, entry, label, args)
        ret_far(cpu, sum(entry.arg_sizes or []), ax=ax, dx=dx)

    def _make_dispatch(self, module: str, ordinal: int):
        def dispatch(cpu) -> None:
            entry = self.entries.get((module, ordinal))
            label = api_name(module, ordinal)
            if entry is None or entry.handler is None:
                self._gap(cpu, label)
            self._invoke(cpu, entry, label)
        return dispatch

    def _make_named_dispatch(self, key: tuple[str, str]):
        def dispatch(cpu) -> None:
            entry = self.named_procs.get(key)
            label = f"{key[0]}.{key[1]}"
            if entry is None or entry.handler is None:
                self._gap(cpu, label)
            self._invoke(cpu, entry, label)
        return dispatch
