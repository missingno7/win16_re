"""Win16 API coverage: join a recovery IR's ``api:*`` surface against an
``ApiRegistry``'s implemented surface plus optional runtime-dispatch counts.

Game-agnostic (this repo's rule): consumes any recovery-IR document produced
through ``win16.irgen`` (every instruction carries a ``platform_effect`` tag —
``api:<MODULE>.<ord>[:<Name>]`` for far transfers into the import-thunk
segment, ``int21_dos``/``int2f``/... for raw software interrupts), any
``win16.api.core.ApiRegistry`` (with or without allocated import slots), and
optional ``RuntimeCounts`` collected by ``instrument_machine`` over a demo
replay.  The consuming game-port project owns the wrapper binding these to a
concrete game (IR artifact + boot path + demo).

Per API target the report answers:

* **identity** — ``MODULE.ordinal`` plus a canonical name, resolved in
  priority order: the Wine-spec ordinal table (the same source
  ``cpu.hook_names`` labels come from), the registry entry's own name, the
  registered handler's ``__name__``, the IR tag's name part.  A target none
  of those name is reported ``unnamed`` honestly — never guessed.
* **static usage** — call-site count + calling symbols from the IR.  Sites
  are deduped by their global ``(CS, IP)``: dispatch-fact / closure entries
  overlap their containing function's byte range, so one call site can
  appear in several records; attribution prefers the record without a
  generated ``entry_origin``.
* **implemented** — registered handler (pascal or raw) vs equate vs tripwire
  (a thunk slot with no handler raises ``Win16ApiGap`` when called).
* **runtime exercise** — how many times the thunk actually dispatched during
  an instrumented run, plus the GetProcAddress-minted dynamic surface and a
  per-service interrupt summary (``int21:<AH>``).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .api.ordinals import ORDINAL_NAMES


# --------------------------------------------------------------------------
# api:* tag parsing
# --------------------------------------------------------------------------

def parse_api_tag(tag: str) -> tuple[str, int, str | None] | None:
    """``api:USER.109:PeekMessage`` -> ``("USER", 109, "PeekMessage")``;
    ``api:KERNEL.90`` -> ``("KERNEL", 90, None)``.  Returns None for tags
    that carry no (module, ordinal) identity (the ``api:slot_XXXX`` fallback
    of an unnamed thunk slot) — the caller reports those separately rather
    than guessing."""
    body = tag[4:]
    if body.startswith("slot_"):
        return None
    modord, _, name = body.partition(":")
    module, _, ordinal = modord.partition(".")
    if not ordinal.isdigit():
        return None
    return module.upper(), int(ordinal), (name or None)


# --------------------------------------------------------------------------
# static usage from the IR
# --------------------------------------------------------------------------

def static_usage(doc: dict) -> tuple[dict, dict, dict]:
    """-> ``(api_sites, int_sites, unresolved_tags)``.

    ``api_sites``: ``(module, ordinal) -> {"sites", "callers", "ir_names"}``
    with sites deduped by global ``(CS, IP)`` (see module docstring);
    ``int_sites``: non-api platform-effect tag -> deduped site count;
    ``unresolved_tags``: tag -> site count for api tags with no parseable
    (module, ordinal) identity."""
    # site (cs, ip) -> (tag, caller_symbol, caller_is_generated)
    seen: dict[tuple[int, str], tuple[str, str, bool]] = {}
    for entry in sorted(doc["functions"]):
        rec = doc["functions"][entry]
        if not rec.get("liftable"):
            continue                    # refused scans carry no reliable edges
        cs = int(entry.split(":")[0], 16)
        symbol = rec.get("symbol") or entry
        generated = "entry_origin" in rec
        for blk in rec.get("blocks", ()):
            for inst in blk["instructions"]:
                effect = inst.get("platform_effect")
                if not effect:
                    continue
                site = (cs, inst["ip"])
                prev = seen.get(site)
                if prev is None or (prev[2] and not generated):
                    seen[site] = (effect, symbol, generated)

    api_sites: dict[tuple[str, int], dict] = {}
    int_sites: dict[str, int] = {}
    unresolved: dict[str, int] = {}
    for (cs, ip), (tag, symbol, _gen) in sorted(seen.items()):
        if not tag.startswith("api:"):
            int_sites[tag] = int_sites.get(tag, 0) + 1
            continue
        parsed = parse_api_tag(tag)
        if parsed is None:
            unresolved[tag] = unresolved.get(tag, 0) + 1
            continue
        module, ordinal, ir_name = parsed
        bucket = api_sites.setdefault((module, ordinal), {
            "sites": [], "callers": set(), "ir_names": set()})
        bucket["sites"].append(f"{cs:04X}:{ip}")
        bucket["callers"].add(symbol)
        if ir_name:
            bucket["ir_names"].add(ir_name)
    return api_sites, int_sites, unresolved


# --------------------------------------------------------------------------
# registry joins
# --------------------------------------------------------------------------

def implementation_status(registry, module: str, ordinal: int) -> str:
    """``handler`` / ``handler-raw`` / ``equate`` / ``tripwire`` (a slot with
    no registered handler raises ``Win16ApiGap`` when called)."""
    key = (module.upper(), ordinal)
    entry = registry.entries.get(key)
    if entry is not None and entry.handler is not None:
        return "handler-raw" if entry.raw else "handler"
    if key in registry.equates:
        return "equate"
    return "tripwire"


def resolve_name(registry, module: str, ordinal: int,
                 ir_name: str | None = None) -> tuple[str | None, str]:
    """-> ``(name | None, source)`` — see module docstring for the priority
    order.  ``None`` means genuinely unnamed (reported honestly)."""
    module = module.upper()
    known = ORDINAL_NAMES.get(module, {}).get(ordinal)
    if known:
        return known, "ordinal-table"
    entry = registry.entries.get((module, ordinal))
    if entry is not None:
        if entry.name and not entry.name.startswith("#"):
            return entry.name, "registry-entry"
        handler_name = getattr(entry.handler, "__name__", "")
        if handler_name and not handler_name.startswith(("<", "_")):
            return handler_name, "handler-name"
    if ir_name:
        return ir_name, "ir-tag"
    return None, "unnamed"


# --------------------------------------------------------------------------
# runtime instrumentation
# --------------------------------------------------------------------------

@dataclass
class RuntimeCounts:
    """Dispatch counts collected live by ``instrument_machine``."""
    api: dict[tuple[str, int], int] = field(default_factory=dict)
    procs: dict[tuple[str, str], int] = field(default_factory=dict)
    ints: dict[str, int] = field(default_factory=dict)
    #: GetProcAddress requests we do NOT implement (mint returned NULL).
    mint_misses: dict[tuple[str, str], int] = field(default_factory=dict)
    description: str = ""


def _counting(orig, table: dict, key) -> object:
    def dispatch(cpu) -> None:
        table[key] = table.get(key, 0) + 1
        orig(cpu)
    dispatch._apicoverage_wrapped = True       # idempotence marker
    return dispatch


def instrument_machine(machine, *, description: str = "") -> RuntimeCounts:
    """Wrap the machine's API-thunk dispatch, GetProcAddress minting and
    interrupt handler with counters.  Call after the registry is installed
    and before the run; mutates the live machine (wrappers preserve the
    original handlers' behaviour exactly)."""
    counts = RuntimeCounts(description=description)
    registry, cpu = machine.api, machine.cpu
    thunk_seg = registry._thunk_seg

    # 1. Static import slots.
    for key, off in registry.slots.items():
        hkey = (thunk_seg, off)
        orig = cpu.replacement_hooks.get(hkey)
        if orig is not None and not getattr(orig, "_apicoverage_wrapped", False):
            cpu.replacement_hooks[hkey] = _counting(orig, counts.api, key)

    # 2. GetProcAddress-minted procs (installed lazily at run time).
    orig_mint = registry.mint_proc_thunk

    def mint(module: str, name: str) -> int:
        far = orig_mint(module, name)
        key = (module.upper(), name)
        if not far:
            counts.mint_misses[key] = counts.mint_misses.get(key, 0) + 1
            return far
        hkey = (registry._thunk_seg, registry._proc_thunks[key])
        hook = cpu.replacement_hooks[hkey]
        if not getattr(hook, "_apicoverage_wrapped", False):
            cpu.replacement_hooks[hkey] = _counting(hook, counts.procs, key)
        return far

    registry.mint_proc_thunk = mint

    # 3. Software interrupts, per service (INT 21h keys on AH).
    orig_int = cpu.interrupt_handler

    def interrupt(c, num: int) -> None:
        if num == 0x21:
            key = f"int21:{(c.s.ax >> 8) & 0xFF:02X}"
        else:
            key = f"int{num:02X}"
        counts.ints[key] = counts.ints.get(key, 0) + 1
        orig_int(c, num)

    cpu.interrupt_handler = interrupt
    return counts


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------

def build_coverage(doc: dict, registry, *,
                   runtime: RuntimeCounts | None = None) -> dict:
    """Join the IR's static api surface, the registry's import slots and
    implemented surface, and (optionally) runtime-dispatch counts into one
    report dict (JSON-serializable, deterministic ordering)."""
    api_sites, int_sites, unresolved_tags = static_usage(doc)

    targets: set[tuple[str, int]] = set(api_sites)
    targets.update(registry.slots)
    targets.update(registry.equates)
    if runtime is not None:
        targets.update(runtime.api)

    have_runtime = runtime is not None
    out_targets: dict[str, dict] = {}
    for module, ordinal in sorted(targets):
        key = (module, ordinal)
        usage = api_sites.get(key)
        ir_names = sorted(usage["ir_names"]) if usage else []
        name, source = resolve_name(registry, module, ordinal,
                                    ir_name=ir_names[0] if ir_names else None)
        impl = implementation_status(registry, module, ordinal)
        calls = runtime.api.get(key, 0) if have_runtime else None
        if impl == "equate":
            classification = "equate"           # data import — never dispatched
        elif impl == "tripwire":
            classification = "unimplemented-tripwire"
        elif not have_runtime:
            classification = "implemented"
        elif calls:
            classification = "implemented+exercised"
        else:
            classification = "implemented+never-exercised"
        out_targets[f"{module}.{ordinal}"] = {
            "module": module,
            "ordinal": ordinal,
            "name": name,
            "name_source": source,
            "unnamed": name is None,
            "implemented": impl,
            "imported": key in registry.slots or key in registry.equates,
            "static_sites": len(usage["sites"]) if usage else 0,
            "callers": sorted(usage["callers"]) if usage else [],
            "runtime_calls": calls,
            "classification": classification,
        }

    # Dynamic (GetProcAddress) surface: everything the registry offers by
    # name, everything actually minted, everything requested and missed.
    dynamic: dict[str, dict] = {}
    minted = getattr(registry, "_proc_thunks", {})
    proc_keys: set[tuple[str, str]] = set(registry.named_procs)
    proc_keys.update(minted)
    if runtime is not None:
        proc_keys.update(runtime.procs)
        proc_keys.update(runtime.mint_misses)
    for module, name in sorted(proc_keys):
        key = (module, name)
        dynamic[f"{module}.{name}"] = {
            "implemented": key in registry.named_procs,
            "minted": key in minted,
            "runtime_calls": runtime.procs.get(key, 0) if have_runtime else None,
            "mint_misses": runtime.mint_misses.get(key, 0) if have_runtime else None,
        }

    counts = [t for t in out_targets.values()]
    summary = {
        "targets": len(out_targets),
        "imported": sum(1 for t in counts if t["imported"]),
        "implemented": sum(1 for t in counts
                           if t["implemented"].startswith("handler")),
        "equates": sum(1 for t in counts if t["implemented"] == "equate"),
        "tripwires": sum(1 for t in counts if t["implemented"] == "tripwire"),
        "static_call_sites": sum(t["static_sites"] for t in counts),
        "unnamed": sum(1 for t in counts if t["unnamed"]),
        "exercised": (sum(1 for t in counts
                          if t["classification"] == "implemented+exercised")
                      if have_runtime else None),
        "never_exercised": (sum(1 for t in counts if t["classification"]
                                == "implemented+never-exercised")
                            if have_runtime else None),
        "dynamic_procs_implemented": sum(1 for d in dynamic.values()
                                         if d["implemented"]),
        "dynamic_procs_minted": sum(1 for d in dynamic.values() if d["minted"]),
        # Registered handlers this program neither imports nor calls — the
        # registry's surface beyond this game's, for context.
        "registry_entries_unreferenced": sum(
            1 for k in registry.entries if k not in targets),
    }

    return {
        "provenance": {
            "ir": doc.get("provenance", {}),
            "runtime": runtime.description if runtime is not None else None,
        },
        "summary": summary,
        "targets": out_targets,
        "dynamic_procs": dynamic,
        "unresolved_api_tags": unresolved_tags,
        "ints": {
            "static_sites": int_sites,
            "runtime": dict(sorted(runtime.ints.items()))
            if runtime is not None else None,
        },
    }


def format_table(report: dict, *, risk_rows: int = 25) -> str:
    """Human-readable coverage table + the stub-quality risk list (implemented
    but never exercised by the instrumented run, ranked by static sites)."""
    targets = report["targets"]
    have_runtime = report["provenance"]["runtime"] is not None
    lines: list[str] = []
    s = report["summary"]
    lines.append(f"API targets: {s['targets']} "
                 f"(implemented {s['implemented']}, equates {s['equates']}, "
                 f"tripwires {s['tripwires']}, unnamed {s['unnamed']}) — "
                 f"{s['static_call_sites']} static call sites")
    if have_runtime:
        lines.append(f"runtime [{report['provenance']['runtime']}]: "
                     f"{s['exercised']} exercised, "
                     f"{s['never_exercised']} implemented+never-exercised")
    lines.append("")
    hdr = f"{'TARGET':<14} {'NAME':<24} {'IMPL':<12} {'SITES':>5}"
    if have_runtime:
        hdr += f" {'RUNTIME':>9}"
    hdr += "  CLASS"
    lines.append(hdr)
    lines.append("-" * len(hdr))
    ordered = sorted(targets.items(),
                     key=lambda kv: (-kv[1]["static_sites"], kv[0]))
    for label, t in ordered:
        row = (f"{label:<14} {t['name'] or '(unnamed)':<24} "
               f"{t['implemented']:<12} {t['static_sites']:>5}")
        if have_runtime:
            row += f" {t['runtime_calls'] if t['runtime_calls'] is not None else '-':>9}"
        row += f"  {t['classification']}"
        lines.append(row)

    if have_runtime:
        risk = [(label, t) for label, t in ordered
                if t["classification"] == "implemented+never-exercised"]
        lines.append("")
        lines.append(f"stub-quality risk (implemented, never exercised by "
                     f"this run): {len(risk)} targets")
        for label, t in risk[:risk_rows]:
            callers = ", ".join(t["callers"][:4])
            more = len(t["callers"]) - 4
            if more > 0:
                callers += f", +{more}"
            lines.append(f"  {t['static_sites']:>4} sites  {label:<14} "
                         f"{t['name'] or '(unnamed)':<24} [{callers}]")

    dynamic = report["dynamic_procs"]
    if dynamic:
        lines.append("")
        lines.append("dynamic (GetProcAddress) surface:")
        for label, d in sorted(dynamic.items()):
            bits = ["implemented" if d["implemented"] else "UNIMPLEMENTED",
                    "minted" if d["minted"] else "never minted"]
            if d["runtime_calls"]:
                bits.append(f"{d['runtime_calls']} calls")
            if d["mint_misses"]:
                bits.append(f"{d['mint_misses']} NULL mints")
            lines.append(f"  {label:<32} {', '.join(bits)}")

    ints = report["ints"]
    lines.append("")
    lines.append(f"interrupt effects: static "
                 f"{ints['static_sites'] or '(none)'}")
    if ints["runtime"] is not None:
        lines.append(f"  runtime per service: {ints['runtime'] or '(none)'}")
    if report["unresolved_api_tags"]:
        lines.append(f"unresolved api tags (no module.ordinal identity): "
                     f"{report['unresolved_api_tags']}")
    return "\n".join(lines)
