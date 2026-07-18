"""Far-pointer PROVENANCE — what a callable FARPROC in guest memory denotes.

A Win16 program does not only reach the OS through static ``call far
THUNK_SEG:slot``.  It also does *dynamic linking*: ``GetProcAddress`` and
``MakeProcInstance`` hand it a far pointer, it stores that pointer in a
variable, and later it calls **through** the variable — ``call far [bp-4]``,
``call far es:[disp16]``, ``call far [DGROUP:0048]``.  A CPUless promoter looks
at such a site, cannot name the target statically, and refuses.  That refusal
is correct on the evidence it has; it is wrong on the evidence that EXISTS.

Because the pointer was not conjured by the program.  **Windows minted it**, and
in this framework "Windows" is :class:`win16.api.core.ApiRegistry`.  The
registry knows every far pointer it ever handed out, and for the overwhelming
majority it knows something much stronger than the address: it knows the
pointer is an import thunk that ``plat.farcall`` already services statically,
with a pascal cleanup :func:`win16.lift.entry_argbytes` can derive.  That fact
simply never reached the promoter.  This module is the channel.

**The split, and which half lives here.**  There are two halves to the
question "what does this indirect call site call?":

1. *what does far pointer V denote?* — a Windows fact.  This module answers it,
   through :meth:`FarPointerLog.describe`, off the registry's own tables.
2. *which far pointer values reach call site S?* — a RUNTIME fact about one
   program's execution.  Nothing here can know it: no far pointer carries the
   address of the site that will eventually call it, and win16 never sees the
   indirect call at all (the interpreter dispatches straight to the thunk hook).
   The consuming game-port project captures it with a probe over a replay, and
   feeds the captures back in as data.

:func:`indirect_farcall_document` is where the two halves meet: sites in,
denotations attached, out comes the evidence file dos_re's consumer side reads.
See that function's docstring for exactly what the probe must record.

**Cost.**  Nothing here is installed by default, and the decisive case needs no
instrumentation at all: :meth:`FarPointerLog.describe` reads ``registry.slots``
and ``registry._proc_thunks``, which the registry maintains as ordinary state
whether or not anyone is watching, so a target that is any kind of thunk is
resolvable *after the fact* from a registry that was never armed.  Arming
(:meth:`FarPointerLog.arm`) adds only the one thing no table retains: the
ORIGIN of a pointer that leaves the thunk segment — ``MakeProcInstance``
handing back a pointer into the program's own code.  It wraps two callables on
the registry INSTANCE (``mint_proc_thunk`` and the ``KERNEL.51`` handler), each
wrapper returning its inner result unchanged, and touches neither
``invoke_values`` nor any dispatch path.  The byte-exact production gate cannot
see it, armed or not.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .lift import API_DISPATCH_COST, entry_argbytes

__all__ = ["FarPointer", "Mint", "FarPointerLog", "indirect_farcall_document"]

#: :attr:`FarPointer.kind` — the far pointer is NULL (0).  ``GetProcAddress``
#: returns this for a proc we do not implement, and a program that stores it
#: and calls through it anyway is calling address zero.
NULL = "null"
#: a STATIC import-thunk slot: ``(module, ordinal)`` from the NE import table.
#: A call through it is exactly the call a static ``call far`` would have made,
#: so it composes as ``plat.farcall`` with the same derived cleanup.
API_THUNK = "api-thunk"
#: a thunk minted at RUN TIME for a by-name proc (``GetProcAddress`` on a
#: dynamically-loaded DLL).  Same dispatch, same contract; it just was not in
#: the import table, so no static analysis could have found it.
PROC_THUNK = "proc-thunk"
#: an address in the thunk segment with nothing bound at it.  A frontier
#: witness, never a guess: something produced a far pointer into the boundary
#: segment that this registry did not mint.
UNBOUND_THUNK = "unbound-thunk"
#: an address OUTSIDE the thunk segment: the program's own code.  A callback it
#: registered with Windows and then also calls itself, typically via
#: ``MakeProcInstance``.  There is no Windows contract to derive — the target is
#: a guest function, and promoting the call means promoting that function.
GUEST = "guest"


@dataclass(frozen=True)
class FarPointer:
    """What a 32-bit far pointer DENOTES, as the Win16 layer understands it."""

    value: int                      #: packed ``seg << 16 | off``
    seg: int
    off: int
    kind: str                       #: one of the module constants above
    name: str | None = None         #: ``"USER.1"`` / ``"MMSYSTEM.mciSendCommand"``
    #: the pascal callee-cleanup for a call through this pointer, or ``None``
    #: when there is no derivable contract (a raw API, an unbound thunk, guest
    #: code).  ``None`` is a refusal and must stay one — see
    #: :func:`win16.lift.entry_argbytes`.
    argbytes: int | None = None
    #: how the program came to hold it, when that was observed: ``"static-import"``,
    #: ``"GetProcAddress"``, ``"MakeProcInstance"``.  ``None`` = never seen minted
    #: (which is normal for a static import slot, whose provenance is the import
    #: table itself rather than any runtime event).
    origin: str | None = None

    @property
    def key(self) -> str:
        """The ``"SEG:OFF"`` spelling dos_re's evidence channel keys on."""
        return f"{self.seg & 0xFFFF:04X}:{self.off & 0xFFFF:04X}"

    @property
    def refusal(self) -> str | None:
        """Why a call through this pointer still has no contract, or ``None``
        when it has one.  The vocabulary is :class:`win16.lift.SkippedSlot`'s,
        because it is the same frontier reached from the dynamic side: a target
        that IS an API but declares no argument list is ``"raw-api"`` here for
        exactly the reason it is ``"raw-api"`` there.  Reporting the *kind*
        instead would say what the target is and leave out what is missing."""
        if self.callable_by_platform:
            return None
        if self.kind in (API_THUNK, PROC_THUNK):
            return "raw-api"
        return self.kind

    @property
    def callable_by_platform(self) -> bool:
        """True when a call through this pointer is a PLATFORM effect with a
        known contract — i.e. dos_re can compose it as ``plat.farcall`` instead
        of refusing the site."""
        return self.kind in (API_THUNK, PROC_THUNK) and self.argbytes is not None

    def as_dict(self) -> dict:
        d = {"target": self.key, "kind": self.kind}
        if self.name is not None:
            d["name"] = self.name
        if self.argbytes is not None:
            d["argbytes"] = self.argbytes
            d["cost"] = API_DISPATCH_COST
        if self.origin is not None:
            d["origin"] = self.origin
        return d


@dataclass(frozen=True)
class Mint:
    """One recorded event of a callable far pointer LEAVING the Win16 layer."""

    value: int                      #: the packed far pointer handed to guest code
    origin: str                     #: the API that handed it over
    detail: str                     #: what was asked for, e.g. ``"MMSYSTEM.mciSendCommand"``


class FarPointerLog:
    """The provenance recorder + resolver for one :class:`ApiRegistry`.

    Construct it around a registry and call :meth:`describe` on any far pointer
    a probe captured.  :meth:`arm` is optional and only adds guest-pointer
    origins (see the module docstring).
    """

    #: the APIs that hand a callable far pointer to guest code.  This list is
    #: the whole claim the module rests on, so it is written out rather than
    #: discovered: if a new API starts returning a FARPROC, it belongs here and
    #: nothing else needs to change.
    #:
    #: ``KERNEL.50 GetProcAddress`` is deliberately ABSENT: its return value is
    #: ``mint_proc_thunk``'s return value unchanged, so wrapping the mint
    #: catches it at the single funnel every by-name proc passes through —
    #: including any future API that mints one.
    HANDOUT_APIS = (("KERNEL", 51, "MakeProcInstance"),)

    def __init__(self, registry) -> None:
        self.registry = registry
        #: every handout observed while armed, in order
        self.mints: list[Mint] = []
        #: packed value -> the origin that handed it over (first handout wins;
        #: a repeated ``MakeProcInstance`` of the same proc is the same fact)
        self.origins: dict[int, tuple[str, str]] = {}
        self._armed: list = []

    # -- recording ---------------------------------------------------------

    def record(self, value: int, origin: str, detail: str) -> None:
        """Note that ``value`` was handed to guest code by ``origin``.

        A NULL handout is recorded too — ``GetProcAddress`` returning 0 is the
        reason a program later far-calls address zero, and losing that would
        turn a diagnosable event into a mystery."""
        value = int(value) & 0xFFFFFFFF
        self.mints.append(Mint(value, origin, detail))
        self.origins.setdefault(value, (origin, detail))

    def arm(self) -> "FarPointerLog":
        """Start recording handouts.  Idempotent; returns self so it chains."""
        if self._armed:
            return self
        reg = self.registry

        inner_mint = reg.mint_proc_thunk

        def mint_proc_thunk(module: str, name: str) -> int:
            value = inner_mint(module, name)
            self.record(value, "GetProcAddress", f"{str(module).upper()}.{name}")
            return value

        # Restore by REMOVING the instance attribute rather than assigning the
        # bound method back: assigning would leave a permanent shadow over the
        # class method, so a disarmed registry would not be the object it was.
        had_own = "mint_proc_thunk" in reg.__dict__
        reg.mint_proc_thunk = mint_proc_thunk
        self._armed.append(
            (lambda: setattr(reg, "mint_proc_thunk", inner_mint)) if had_own
            else (lambda: reg.__dict__.pop("mint_proc_thunk", None)))

        for module, ordinal, origin in self.HANDOUT_APIS:
            entry = reg.entries.get((module, ordinal))
            if entry is None or entry.handler is None:
                continue                        # API not installed in this surface
            self._armed.append(_wrap_handler(self, entry, origin))
        return self

    def disarm(self) -> None:
        """Undo :meth:`arm`, restoring the registry exactly as it was."""
        while self._armed:
            self._armed.pop()()

    def __enter__(self) -> "FarPointerLog":
        return self.arm()

    def __exit__(self, *_exc) -> None:
        self.disarm()

    # -- resolution --------------------------------------------------------

    def describe(self, value: int) -> FarPointer:
        """Resolve a captured far pointer to what it denotes.

        Works on an UNARMED log for every thunk target, because the tables it
        reads are the registry's own state rather than anything this module
        installed."""
        value = int(value) & 0xFFFFFFFF
        seg, off = (value >> 16) & 0xFFFF, value & 0xFFFF
        origin = self.origins.get(value)
        origin_name = origin[0] if origin else None

        if value == 0:
            return FarPointer(0, 0, 0, NULL, origin=origin_name)

        thunk_seg = getattr(self.registry, "_thunk_seg", 0) & 0xFFFF
        if seg != thunk_seg or not thunk_seg:
            # No name and no argbytes, deliberately: a guest address denotes a
            # function in the PROGRAM, which this repo does not get to know.
            # The origin is the whole Windows-side fact about it.
            return FarPointer(value, seg, off, GUEST, origin=origin_name)

        for (module, ordinal), slot in self.registry.slots.items():
            if slot == off:
                entry = self.registry.entries.get((module, ordinal))
                return FarPointer(
                    value, seg, off, API_THUNK, name=f"{module}.{ordinal}",
                    argbytes=entry_argbytes(entry),
                    origin=origin_name or "static-import")

        for (module, name), slot in getattr(
                self.registry, "_proc_thunks", {}).items():
            if slot == off:
                entry = self.registry.named_procs.get((module, name))
                return FarPointer(
                    value, seg, off, PROC_THUNK, name=f"{module}.{name}",
                    argbytes=entry_argbytes(entry),
                    origin=origin_name or "GetProcAddress")

        return FarPointer(value, seg, off, UNBOUND_THUNK, origin=origin_name)


def _wrap_handler(log: FarPointerLog, entry, origin: str):
    """Wrap one registered handler so its RETURN VALUE is recorded, and return
    the undo.  The wrapper is transparent: same arguments, same return value,
    no state of its own — the handler's own effect is untouched."""
    inner = entry.handler

    def handler(ctx):
        value = inner(ctx)
        if value is not None:
            log.record(value, origin, f"{entry.module}.{entry.ordinal}")
        return value

    entry.handler = handler
    return lambda: setattr(entry, "handler", inner)


_DOCUMENT_NOTICE = (
    "GENERATED by win16.farptr.indirect_farcall_document: per-site indirect "
    "far-call targets (captured by the game project's runtime probe) resolved "
    "against the Win16 API registry's own far-pointer provenance. Disposable; "
    "regenerate, do not hand-edit.")


def _iter_sites(sites):
    """Accept both spellings of a probe capture and yield ``(site, targets)``
    with ``targets`` a ``{key: count}`` mapping.

    * the serialized ``indirect_sites.json`` form — a LIST of
      ``{"site": "CS:IP", "targets": {"SEG:OFF": count}}``;
    * the in-process form — a MAPPING ``{"CS:IP": {"SEG:OFF": count}}``, or
      ``{"CS:IP": ["SEG:OFF", ...]}`` when the probe kept no counts.

    Neither is a fallback for the other; they are the same capture written two
    ways, and a caller should not have to reshape one into the other.
    """
    if isinstance(sites, Mapping):
        items = sites.items()
    else:
        items = ((rec["site"], rec["targets"]) for rec in sites)
    for site, targets in items:
        if isinstance(targets, Mapping):
            yield str(site).upper(), {str(k).upper(): int(v)
                                      for k, v in targets.items()}
        else:
            yield str(site).upper(), {str(k).upper(): 0 for k in targets}


def _packed(key: str) -> int:
    seg, off = key.split(":")
    return ((int(seg, 16) & 0xFFFF) << 16) | (int(off, 16) & 0xFFFF)


def indirect_farcall_document(log: FarPointerLog, sites, *,
                              demo: str | None = None) -> dict:
    """Join a runtime site capture to far-pointer provenance -> the evidence file.

    **What the game project's probe must record.**  For every INDIRECT far call
    the interpreter executes (``call far`` with a memory or register operand —
    ``FF /3`` and ``FF /5``, not the static ``9A``), one row:

    * ``site`` — the CS:IP **of the call instruction itself**, formatted
      ``"%04X:%04X"``.  Not the return address, and not the target: the site is
      the key dos_re's promoter looks the evidence up by, and it must be the
      address the refusal names.
    * ``targets`` — the far pointer value the site actually branched to, as
      ``"SEG:OFF"``, counted per distinct value.  Read the operand AFTER the
      effective address is resolved, i.e. the ``CS:IP`` the CPU is about to
      transfer to; a site that is called with several different pointers over a
      run must accumulate all of them, because a promoted body has to handle
      every value the site can see, not the first one.

    Capture it over the SAME deterministic replay the rest of the evidence
    comes from, and capture it with the interpreter unhooked, or a lifted island
    standing in for a caller will hide the very sites this exists to find.

    The document carries three views of the same join, because the consumer
    needs different ones at different stages:

    * ``"sites"`` — the full annotated join: every site, every target, with its
      denotation.  This is what a human reads to see the frontier close.
    * ``"dyn_evidence"`` — ``{"CS:IP": ["SEG:OFF", ...]}``, dos_re's existing
      closure-walk input shape, unchanged, so ``cpuless_closure --dyn-evidence``
      consumes it as-is.
    * ``"contracts"`` — the ``plat.farcall`` contracts for every target that
      HAS one, in the identical ``{"SEG:OFF": {argbytes, cost, name}}`` shape
      :func:`win16.lift.plat_farcalls_document` produces for static call sites.
      A target with no derivable contract is simply absent from this map, and a
      promoter that finds it absent must keep refusing — the whole point is to
      supply the contracts that EXIST, never to invent the ones that do not.

    ``"unresolved"`` lists the sites that still have no fully-contracted target,
    with the reason per target, so the remaining frontier is reported rather
    than silently shrunk.
    """
    out_sites: dict[str, dict] = {}
    dyn: dict[str, list[str]] = {}
    contracts: dict[str, dict] = {}
    unresolved: dict[str, dict[str, str]] = {}

    for site, targets in _iter_sites(sites):
        rows = []
        for key, count in sorted(targets.items()):
            fp = log.describe(_packed(key))
            row = fp.as_dict()
            if count:
                row["count"] = count
            rows.append(row)
            if fp.callable_by_platform:
                contracts[fp.key] = {"argbytes": fp.argbytes,
                                     "cost": API_DISPATCH_COST,
                                     "name": fp.name}
            else:
                unresolved.setdefault(site, {})[fp.key] = fp.refusal
        out_sites[site] = {"targets": rows}
        dyn[site] = [r["target"] for r in rows]

    doc = {
        "_notice": _DOCUMENT_NOTICE,
        "thunk_seg": f"{getattr(log.registry, '_thunk_seg', 0) & 0xFFFF:04X}",
        "sites": dict(sorted(out_sites.items())),
        "dyn_evidence": dict(sorted(dyn.items())),
        "contracts": dict(sorted(contracts.items())),
        "unresolved": dict(sorted(unresolved.items())),
    }
    if demo is not None:
        doc["demo"] = demo
    return doc
