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


@dataclass
class CallContext:
    """What a handler sees: the CPU plus decoded pascal arguments."""
    cpu: object                     # dos_re CPU8086
    registry: "ApiRegistry"
    module: str
    ordinal: int
    name: str
    args: tuple[int, ...] = ()

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


@dataclass
class ApiEntry:
    module: str
    ordinal: int
    name: str
    handler: Callable[[CallContext], int | None] | None
    arg_sizes: list[int] | None     # None => raw handler owns the whole contract
    ret: str = "word"               # "word" (AX), "long" (DX:AX), "void"
    raw: bool = False


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
                 args: str = "", ret: str = "word"):
        """Decorator: register a pascal-convention handler.

        `args` is the Wine-spec argument list, e.g. "word str long".  The
        wrapper reads the args, calls the handler with a CallContext, then
        far-returns popping the argument bytes.  `ret` declares the return
        contract: "word" puts the handler's int in AX (DX untouched), "long"
        puts it in DX:AX (DX set even when the high word is 0), "void" leaves
        both registers alone.
        """
        module = module.upper()
        known = ORDINAL_NAMES.get(module, {}).get(ordinal)
        if name is None:
            name = known
        if known is not None and name != known:
            raise ValueError(f"{module}.{ordinal} is {known}, not {name}")
        if ret not in ("word", "long", "void"):
            raise ValueError(f"bad ret spec {ret!r}")
        sizes = [ARG_SIZES[a] for a in args.split()] if args else []

        def deco(fn):
            key = (module, ordinal)
            if key in self.entries:
                raise ValueError(f"duplicate registration for {api_name(module, ordinal)}")
            self.entries[key] = ApiEntry(module, ordinal, name or f"#{ordinal}",
                                         fn, sizes, ret=ret)
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

    def _invoke(self, cpu, entry: ApiEntry, label: str) -> None:
        """Read pascal args, run the handler, and far-return with its result —
        shared by static-ordinal and by-name (GetProcAddress) dispatch."""
        if entry.raw:
            self.call_log.append(label)
            entry.handler(CallContext(cpu, self, entry.module, entry.ordinal, entry.name))
            return
        args = read_pascal_args(cpu, entry.arg_sizes or [])
        ctx = CallContext(cpu, self, entry.module, entry.ordinal, entry.name, args)
        self.call_log.append(f"{label}{args!r}")
        # Publish this API frame for call_far's resumable-callback record
        # (win16/callback.py): a callback dispatched by this handler needs the
        # API's name + argbytes to complete the call if a snapshot parks inside
        # it.  Saved/restored so nested dispatches stack.
        prev_api = getattr(cpu, "win16_current_api", ("?", 0))
        cpu.win16_current_api = (entry.name, sum(entry.arg_sizes or []))
        try:
            result = entry.handler(ctx)
        finally:
            cpu.win16_current_api = prev_api
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
