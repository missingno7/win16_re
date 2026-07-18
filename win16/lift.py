"""Win16 LIFT-TIME facts — the OS/ABI properties a CPUless promotion needs.

The three-bucket split this module exists to hold up.  When a lifting question
comes up, ask: *would a DOS binary from a different compiler plausibly contain
this?*

* **yes** — it is a generic code-shape mechanism (a frame pointer carried at a
  constant bias, a stack-probe idiom, a dispatch arm).  It belongs in
  ``dos_re``, which learns no operating system;
* **no, it is a property of the OS/ABI** — the pascal callee-cleanup of a
  KERNEL/USER/GDI import, the far-entry calling convention, the shape of the
  import-thunk table.  It belongs **here**;
* **no, it is this EXE** — a symbol table, a hand fact, an override.  It
  belongs in the consuming game-port project.

The hard rule that keeps the split from becoming a fork: **shared code never
branches on platform identity.**  There is no ``if win16:`` inside ``dos_re``.
The platform enters dos_re's promoter purely as *data* — the boundary segment
number and a per-slot contract table — and this module is what produces that
data from the Windows side.

Nothing here knows a game.  The thunk segment, the slot table and the API
registry all arrive as arguments; what this module owns is the single Win16
fact that turns them into a contract: **a Win16 API is pascal-convention and
cleans its own arguments**, so the callee-cleanup byte count for a thunk slot
is the sum of the declared argument sizes of the API behind it — exactly the
number :func:`win16.api.core.ret_far` pops when the interpreter services the
same call.

See ``tests/test_win16_lifting_conformance.py`` for the fence: synthetic
Win16-shaped bodies (the far-entry prologue, ``__loadds``, a boundary far call)
lifted and diffed against the interpreter, with no game checked out.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

# from .machine, not .loader: the constant is the same, but loader builds a
# CPU8086 at import time and this is a build-time module with no VM in it.
from .machine import THUNK_SEG  # noqa: F401  (re-exported: boundary segment)

__all__ = ["THUNK_SEG", "SkippedSlot", "plat_farcall_contracts",
           "plat_farcalls_document"]

#: Virtual-instruction cost the interpreter charges for one Win16 API
#: dispatch.  The API surface installs a plain replacement hook per thunk slot
#: (``win16/api/core.py``), and a plain hook — one that does not declare
#: ``owns_time`` — is charged exactly one instruction.  A recovered body adds
#: this back through ``plat.farcall``'s reported cost, so a promoted caller's
#: virtual clock stays identical to the interpreter's.
API_DISPATCH_COST = 1


@dataclass(frozen=True)
class SkippedSlot:
    """A thunk slot that gets NO far-call contract, and why.

    Never a guess: a far call to such a slot refuses
    ``platform-farcall-contract-unknown`` in dos_re's promoter, which is the
    honest frontier item.  ``reason`` is one of:

    * ``"raw-api"`` — the API is a raw handler (``arg_sizes is None``); it owns
      its own return contract, so there is no declared pascal argument list to
      sum.  Giving it one means giving it an args-in/result-out values form.
    * ``"unimplemented"`` — the registry has no entry for that
      ``(module, ordinal)`` at all.  That is an API gap, not a lifting gap.
    """
    key: str            #: ``"MODULE.ordinal"``
    off: int            #: thunk-segment offset of the slot
    reason: str


def _normalize(key) -> tuple[str, int]:
    """Accept both slot-table spellings: the serialized manifest form
    ``"USER.1"`` and the in-process :attr:`ApiRegistry.slots` form
    ``("USER", 1)``.  Both name the same thing; neither is a fallback for the
    other."""
    if isinstance(key, tuple):
        mod, ordinal = key
        return str(mod).upper(), int(ordinal)
    mod, ordinal = str(key).rsplit(".", 1)
    return mod.upper(), int(ordinal)


def plat_farcall_contracts(
        thunk_seg: int,
        api_slots: Mapping[str | tuple[str, int], int],
        registry,
        *,
        cost: int = API_DISPATCH_COST,
) -> tuple[dict[str, dict], list[SkippedSlot]]:
    """Derive the CPUless PLATFORM far-call contracts for an import-thunk table.

    A Win16 program reaches every KERNEL / USER / GDI / ... API through a static
    ``call far THUNK_SEG:slot`` into the import-thunk segment.  dos_re composes
    such a call as a ``plat.farcall`` platform effect rather than refusing it as
    an uncomposed call — but only when it is handed the pascal callee-cleanup
    ``argbytes`` for the target slot, which it never guesses.  That number is a
    *Windows* fact, and this is where it is produced.

    :param thunk_seg: the import-thunk segment (:data:`THUNK_SEG` for a machine
        built by ``win16.loader``; passed explicitly so a caller reading a boot
        manifest uses the value that image was actually built with).
    :param api_slots: the slot table — ``{"USER.1": 0x0004, ...}`` (manifest
        spelling) or ``{("USER", 1): 0x0004, ...}``
        (:attr:`ApiRegistry.slots` spelling) — mapping each imported API to its
        offset within ``thunk_seg``.
    :param registry: an :class:`win16.api.core.ApiRegistry` (from
        ``win16.api.surface.build_registry``); only ``.entries`` is read.
    :param cost: virtual instructions charged per dispatch
        (:data:`API_DISPATCH_COST`).

    :returns: ``(contracts, skipped)``.  ``contracts`` is dos_re's
        ``--plat-farcalls`` map, ``{"SSSS:OOOO": {"argbytes", "cost", "name"}}``
        with uppercase 4-hex keys.  ``skipped`` lists the slots that get no
        contract, each with its reason (see :class:`SkippedSlot`).
    """
    contracts: dict[str, dict] = {}
    skipped: list[SkippedSlot] = []
    for raw_key, off in api_slots.items():
        mod, ordinal = _normalize(raw_key)
        key = f"{mod}.{ordinal}"
        entry = registry.entries.get((mod, ordinal))
        if entry is None:
            skipped.append(SkippedSlot(key, int(off), "unimplemented"))
            continue
        if entry.arg_sizes is None:
            skipped.append(SkippedSlot(key, int(off), "raw-api"))
            continue
        contracts[f"{thunk_seg & 0xFFFF:04X}:{int(off) & 0xFFFF:04X}"] = {
            "argbytes": sum(entry.arg_sizes),
            "cost": cost,
            "name": key,
        }
    return contracts, skipped


_DOCUMENT_NOTICE = (
    "GENERATED by win16.lift.plat_farcalls_document from the Win16 "
    "import-thunk table + the API registry's declared pascal argument sizes. "
    "Disposable; regenerate, do not hand-edit.")


def plat_farcalls_document(
        thunk_seg: int,
        api_slots: Mapping[str | tuple[str, int], int],
        registry,
        *,
        cost: int = API_DISPATCH_COST,
) -> tuple[dict, list[SkippedSlot]]:
    """:func:`plat_farcall_contracts` wrapped in the JSON document dos_re's
    ``cpuless_promote --plat-farcalls @FILE`` reads (a ``"contracts"`` map plus
    metadata).  ``json.dumps`` it and hand over the path; the skipped list is
    returned alongside for the caller's own frontier reporting, deliberately
    NOT written into the document."""
    contracts, skipped = plat_farcall_contracts(
        thunk_seg, api_slots, registry, cost=cost)
    doc = {
        "_notice": _DOCUMENT_NOTICE,
        "thunk_seg": f"{thunk_seg & 0xFFFF:04X}",
        "contracts": contracts,
    }
    return doc, skipped
