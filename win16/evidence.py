"""Win16 replay execution evidence: observed entries + dynamic-dispatch
targets, identity-keyed for the Execution Atlas.

The observation half of dos_re 3.0's replay-evidence contract
(`ReplayExecutionEvidence` / `set_execution_evidence` / Atlas
`ingest-replay`), for a Win16 machine replaying under the INTERPRETER:

* every interpreted instruction is tested against the IR's function-entry
  set — a hit is a function VISIT (entry-only: exits are not tracked, so
  every visit is honestly ``incomplete`` with its true invocation count and
  first-entry replay point);
* an executed indirect-dispatch site (near ``call [..]``/``jmp [..]``, far
  ISR-chain ``jmp``, and FAR ``call [..]`` — Win16 dynamic linking) leaves a
  pending slot which the NEXT control-transfer observation binds: an
  interpreted instruction binds a guest target, a replacement-hook dispatch
  binds an import thunk WITH ITS API NAME (a thunk never reaches the
  interpreted path — the same both-halves rule the entry probe and
  ``win16.farptr`` established).

The probe is a ``cpu.coverage_telemetry`` and keeps the per-instruction cost
to set-membership tests and integer bookkeeping; identity strings and replay
points are constructed once, at :func:`finish`.  Observed evidence joins the
Atlas's static projection by construction: transfer sources are the SAME
``ExecutionPointIdentity`` keys the static Recovery-IR import gives
unresolved ``call_ind``/``jmp_ind`` sites, guest targets are
``FunctionIdentity`` keys (or execution points for non-entry targets), and
API-thunk targets are the ``platform-effect`` ``BoundaryIdentity`` keys the
static import mints from the IR's ``api:*`` effect tags.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from dos_re.identity import (BoundaryIdentity, ExecutionPointIdentity,
                             FunctionIdentity, ImageIdentity,
                             real_mode_address)
from dos_re.replay import (FunctionVisit, ObservedTransfer, ReplayEvent,
                           ReplayExecutionEvidence, ReplayExecutionIdentity,
                           ReplayPoint)


_PREFIXES = (0x26, 0x2E, 0x36, 0x3E, 0xF0, 0xF2, 0xF3, 0x66, 0x67)


def modrm_reg(hexbytes: str) -> int | None:
    """The ModRM reg/opcode-extension field of a decoded instruction, skipping
    prefix bytes.  For an 0xFF-group transfer this is the /digit separating
    NEAR (call /2, jmp /4) from FAR (call /3, jmp /5) indirect."""
    b = bytes.fromhex(hexbytes)
    j = 0
    while j < len(b) and b[j] in _PREFIXES:
        j += 1
    j += 1                                   # opcode
    if j < len(b):
        return (b[j] >> 3) & 7
    return None


def entry_set(ir: Mapping) -> frozenset[tuple[int, int]]:
    """Every IR function entry as a (cs, ip) pair."""
    out = set()
    for key in ir["functions"]:
        cs, ip = key.split(":")
        out.add((int(cs, 16), int(ip, 16)))
    return frozenset(out)


def dispatch_sites(ir: Mapping) -> dict[tuple[int, int], str]:
    """Every indirect-dispatch site with its IR edge kind: ``call [..]`` /2
    (near) and /3 (far — Win16 dynamic linking) as ``call_ind``, ``jmp [..]``
    /4 (near jump tables) and /5 (far ISR-chain tail) as ``jmp_ind`` — the
    same kinds the static Recovery-IR import gives their unresolved edges."""
    out: dict[tuple[int, int], str] = {}
    for key, fn in ir["functions"].items():
        cs = int(key.split(":")[0], 16)
        for blk in fn["blocks"]:
            for i in blk["instructions"]:
                kind = i.get("kind")
                if kind not in ("call_ind", "jmp_ind"):
                    continue
                reg = modrm_reg(i["bytes"])
                if (kind == "call_ind" and reg in (2, 3)) or \
                        (kind == "jmp_ind" and reg in (4, 5)):
                    out[(cs, int(i["ip"], 16))] = kind
    return out


class Win16EvidenceProbe:
    """Telemetry collector; install as ``cpu.coverage_telemetry``.

    ``ordinal`` is a zero-argument callable returning the replay input
    driver's current timeline position (events applied so far) — the replay
    point an observation is attributed to.
    """

    __slots__ = ("entries", "sites", "ordinal", "counts", "first_ord",
                 "dyn", "pending")

    def __init__(self, entries: frozenset[tuple[int, int]],
                 sites: frozenset[tuple[int, int]],
                 ordinal: Callable[[], int]):
        self.entries = entries
        self.sites = sites
        self.ordinal = ordinal
        #: function entry (cs, ip) -> true invocation count
        self.counts: dict[tuple[int, int], int] = {}
        #: function entry (cs, ip) -> ordinal at first observed entry
        self.first_ord: dict[tuple[int, int], int] = {}
        #: site (cs, ip) -> {target: [count, first_ordinal, last_ordinal]}
        #: where target is a guest (cs, ip) tuple or an API hook-name string.
        self.dyn: dict[tuple[int, int], dict[Any, list[int]]] = {}
        self.pending: tuple[int, int] | None = None

    def _bind(self, target) -> None:
        tgts = self.dyn.setdefault(self.pending, {})
        cur = self.ordinal()
        rec = tgts.get(target)
        if rec is None:
            tgts[target] = [1, cur, cur]
        else:
            rec[0] += 1
            rec[2] = cur
        self.pending = None

    def record_interpreted_instruction(self, addr) -> None:
        if self.pending is not None:
            self._bind(addr)
        if addr in self.entries:
            n = self.counts.get(addr)
            if n is None:
                self.counts[addr] = 1
                self.first_ord[addr] = self.ordinal()
            else:
                self.counts[addr] = n + 1
        if addr in self.sites:
            self.pending = addr

    # A replacement-hook dispatch: for a pending FAR-indirect site this IS the
    # resolved target (an import thunk is a Python hook and never appears as
    # an interpreted instruction) — bind it by NAME, the boundary identity.
    def record_hook_unverified(self, addr, name) -> None:  # noqa: D401
        if self.pending is not None:
            self._bind(str(name))

    # unused telemetry surface — present so the CPU never AttributeErrors.
    def record_hook_verified(self, *a, **k) -> None:
        pass


class EntryOnlyVisits:
    """Duck-typed visits index: honest entry-only records (``incomplete``)."""

    def __init__(self, visits: list[FunctionVisit]):
        self._visits = sorted(visits, key=lambda v: v.function_id)

    def records(self) -> tuple[FunctionVisit, ...]:
        return tuple(self._visits)

    def to_json(self) -> list[dict]:
        return [record.to_json() for record in self._visits]


def finish(probe: Win16EvidenceProbe, *, image: ImageIdentity,
           address_space: str, timeline_id: str,
           profile: ReplayExecutionIdentity,
           site_kinds: Mapping[tuple[int, int], str],
           provenance: Mapping[str, Any],
           ) -> tuple[ReplayExecutionEvidence, EntryOnlyVisits]:
    """Materialize the probe's raw observations as identity-keyed evidence."""
    program = image.program

    def fid(cs: int, ip: int) -> str:
        return FunctionIdentity(image, address_space,
                                real_mode_address(cs, ip)).key

    def pid(cs: int, ip: int) -> str:
        return ExecutionPointIdentity(image, address_space,
                                      real_mode_address(cs, ip)).key

    def point(ordinal: int) -> ReplayPoint:
        return ReplayPoint(ordinal, timeline_id)

    visits = [
        FunctionVisit(fid(cs, ip), invocation_count=n,
                      first_entry=point(probe.first_ord[(cs, ip)]),
                      last_exit=None, incomplete=True)
        for (cs, ip), n in probe.counts.items()
    ]

    transfers = []
    for site, targets in probe.dyn.items():
        source = pid(*site)
        kind = site_kinds.get(site, "call_ind")
        for target, (count, first, last) in targets.items():
            if isinstance(target, str):
                # An API import thunk, bound by hook name: the SAME
                # platform-effect boundary key the static IR import mints.
                target_id = BoundaryIdentity(
                    program, "platform-effect", target).key
            elif target in probe.entries:
                target_id = fid(*target)
            else:
                target_id = pid(*target)
            transfers.append(ObservedTransfer(
                source, target_id, kind, count, point(first), point(last)))

    evidence = ReplayExecutionEvidence(
        profile.identity_digest, tuple(transfers),
        provenance=dict(provenance))
    return evidence, EntryOnlyVisits(visits)
