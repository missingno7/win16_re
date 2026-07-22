"""Data-only Win16 boot image: build + EXE-free load (dos_re_2.0 §1a').

The EXE-independence wall for an NE program:

    The EXE goes into the recovery pipeline.  Generated host code and data
    come out.  The VMless runtime never sees the EXE again.

**Build** (:func:`build_boot_image`, EXE consumed HERE, never at run time):
the port boots its machine through the normal NE loader to the canonical
post-relocation entry state (for an NE program that is instruction zero — the
loader does all its work at load time, unlike a DOS self-extractor), captures
it as a vmsnap snapshot, then

* **poisons the recovered code** — every byte the recovery IR decoded as an
  instruction is ZEROED, except ranges the port's recovery facts declare
  ``code_as_data`` (jump tables, inline data the game reads);
* pickles an EXE-free **program identity** (the parsed NE minus its raw
  bytes; resources carry their own data, so LoadBitmap/DialogBox/LoadMenu
  keep working without the file);
* records a **manifest**: source-EXE provenance (name + SHA-256 + size), the
  segment map, the resolved API import slot table (assigned lazily in
  relocation order at load time — NOT re-derivable without the EXE), the
  registry equates, poison accounting, code_as_data, a region classification
  of every NE segment, and the post-poison memory hash.

**Load** (:func:`load_boot_image`): reconstructs a live ``Win16Machine`` from
the image + a fresh API registry (the port's loader-free registry factory,
e.g. ``win16.api.surface.build_registry``) — no NE parsing, no EXE read.
:func:`boot_vmless_machine` adds the wall enforcement: lifted-graph install,
``cpu.code_poisoned`` (entry-signature guards off against zeroed bytes) and
the interpreter poison ``cpu.interp_forbidden`` armed from instruction zero.

**Audit** (:func:`audit_boot_image`): the image is a legitimate data-only
artifact — no bundled executable (name, suffix, MZ header, or content hash),
every IR-decoded instruction byte zero unless declared ``code_as_data``, and
— stronger than the DOS audit — every nonzero byte inside a CODE segment is
accounted for: IR-decoded (and thus poisoned) or covered by a declared
``code_as_data`` range.

The runtime file-access guard is generic already —
``dos_re.independence.exe_access_guard_from_manifest`` reads the same
manifest shape this module writes.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import pickle
from pathlib import Path

from dos_re.bootimage import coalesce, instruction_ranges, sha256_file
from dos_re.independence import GeneratedGraphBootstrapError

from .machine import (          # CPU-free: never the loader
    BOOT_MANIFEST_SCHEMA, THUNK_SEG, Win16Machine)
from .ne import NEExecutable
from .vmsnap import restore_machine_state, save_snapshot


def strip_program(exe: NEExecutable) -> NEExecutable:
    """The EXE-free program identity: the parsed NE minus its raw bytes.

    Everything the RUNTIME consults survives — header fields, segment
    metadata (``alloc_size``), the resource tree (each ``Resource`` carries
    its own ``data`` bytes), name tables — while ``segment_bytes`` (the code/
    data loader path) fails loud on the empty ``raw``.  The path keeps only
    the basename: the runtime may print it, never open it."""
    return dataclasses.replace(exe, raw=b"", path=Path(exe.path.name))


def _linear_ranges(pairs) -> list[tuple[int, int]]:
    return [(int(start), int(length)) for start, length in pairs]


def build_boot_image(machine: Win16Machine, out_dir: str | Path, *,
                     ir_path: str | Path,
                     code_as_data: list[tuple[int, int]] = (),
                     game: str = "", note: str = "",
                     poison: bool = True,
                     extra_manifest: dict | None = None) -> dict:
    """Capture ``machine`` (a freshly loaded NE machine at its canonical
    entry) as a data-only boot image in ``out_dir``.  Returns the manifest.

    ``code_as_data`` — (linear_start, length) ranges preserved from the
    poison (the port's declared jump tables / inline data facts)."""
    out = Path(out_dir)
    exe = machine.exe
    if not isinstance(exe, NEExecutable) or not exe.raw:
        raise GeneratedGraphBootstrapError(
            "build_boot_image needs a machine loaded from the real NE "
            "executable (the EXE is consumed at BUILD time only)")

    save_snapshot(machine, out, note=note or "data-only VMless boot image",
                  game=game)
    (out / "program.pickle").write_bytes(pickle.dumps(strip_program(exe)))

    # --- poison the recovered code ---------------------------------------
    ir = json.loads(Path(ir_path).read_text(encoding="utf-8"))
    inst_ranges = instruction_ranges(ir)         # linear (cs<<4 + ip) ranges
    poison_offsets: set[int] = set()
    for start, length in inst_ranges:
        poison_offsets.update(range(start, start + length))
    keep_offsets: set[int] = set()
    for start, length in code_as_data:
        keep_offsets.update(range(start, start + length))
    # A code_as_data fact may not overlap DECODED instructions: preserving
    # instruction bytes through a sloppy data declaration would smuggle
    # recovered code into the "data-only" image.  (dos_re's DOS images allow
    # the overlap deliberately, for self-checksummed code; no Win16 case has
    # needed it — parameterize when one does, never default to it.)
    overlap = poison_offsets & keep_offsets
    if overlap:
        first = ", ".join(hex(o) for o in sorted(overlap)[:8])
        raise GeneratedGraphBootstrapError(
            f"code_as_data range(s) overlap {len(overlap)} decoded "
            f"instruction byte(s) (first at {first}) — split the fact")

    mem_path = out / "memory.bin"
    image = bytearray(mem_path.read_bytes())
    present_before = sum(1 for off in poison_offsets if image[off] != 0)
    if poison:
        for off in poison_offsets:
            image[off] = 0
    present_after = sum(1 for off in poison_offsets if image[off] != 0)
    poison_runs = coalesce(poison_offsets)

    # --- stage 2: NOTHING undeclared survives in a code segment ----------
    # Beyond the decoded instructions, a code segment still carries alignment
    # NOPs, statically unreached code (alternate FP-dispatch bodies, dead
    # tails) and inline data.  Independence policy: zero EVERY remaining
    # nonzero code-segment byte that no code_as_data fact claims — the image
    # then contains no original code-segment content beyond the declared
    # data.  A byte the game actually reads as data must be declared, and an
    # omission cannot pass silently: the whole-demo differential against the
    # interpreted oracle diverges after the first read of a zeroed byte.
    undeclared_offsets: set[int] = set()
    if poison:
        for seg in exe.segments:
            if seg.is_data:
                continue
            base = machine.seg_bases[seg.index] << 4
            for off in range(base, base + seg.file_length):
                if image[off] and off not in keep_offsets:
                    undeclared_offsets.add(off)
                    image[off] = 0
        mem_path.write_bytes(bytes(image))
    undeclared_runs = coalesce(undeclared_offsets)

    # --- region map: every NE segment, declared --------------------------
    regions = []
    for seg in exe.segments:
        para = machine.seg_bases[seg.index]
        regions.append({
            "ne_seg": seg.index,
            "para": para,
            "start": para << 4,
            "end": (para << 4) + seg.alloc_size,
            "loaded": seg.file_length,
            "kind": "data" if seg.is_data else "code",
        })

    manifest = {
        "_notice": "GENERATED data-only Win16 boot image -- the strict-VMless "
                   "runtime loads THIS, never the original executable "
                   "(dos_re_2.0 section 1a').",
        "schema": BOOT_MANIFEST_SCHEMA,
        "source_exe": {
            "name": exe.path.name,
            "sha256": sha256_file(exe.path),
            "size": exe.path.stat().st_size,
        },
        "memory_size": len(image),
        "seg_bases": list(machine.seg_bases),
        "thunk_seg": THUNK_SEG,
        "api_slots": {f"{mod}.{ordn}": off
                      for (mod, ordn), off in sorted(machine.api.slots.items())},
        "api_equates": {f"{mod}.{ordn}": val
                        for (mod, ordn), val in sorted(machine.api.equates.items())},
        "poison": {
            "enabled": poison,
            "policy": "zero recovered-instruction bytes; preserve inter-"
                      "instruction data and declared code_as_data ranges",
            "censused_functions": len(ir["functions"]),
            "instruction_ranges": len(inst_ranges),
            "poisoned_bytes": len(poison_offsets),
            "poisoned_runs": len(poison_runs),
            "code_bytes_present_before": present_before,
            "code_bytes_present_after": present_after,
            "ranges": [[start, end - start] for start, end in poison_runs],
            "undeclared_bytes_zeroed": len(undeclared_offsets),
            "undeclared_runs": [[start, end - start]
                                for start, end in undeclared_runs],
        },
        "code_as_data": {
            "policy": "instruction-region ranges the game reads as data "
                      "(jump tables); preserved and declared here",
            "ranges": [[start, length] for start, length in code_as_data],
        },
        "regions": regions,
        "memory_sha256": hashlib.sha256(bytes(image)).hexdigest(),
        "artifacts": {"memory": "memory.bin", "state": "state.json",
                      "system": "system.pickle", "program": "program.pickle"},
    }
    manifest.update(extra_manifest or {})
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2),
                                       encoding="utf-8")
    return manifest


def load_boot_manifest(boot_dir: str | Path) -> dict:
    manifest = json.loads((Path(boot_dir) / "manifest.json")
                          .read_text(encoding="utf-8"))
    if manifest.get("schema") != BOOT_MANIFEST_SCHEMA:
        raise GeneratedGraphBootstrapError(
            f"unrecognized boot image schema in {boot_dir} "
            f"(want {BOOT_MANIFEST_SCHEMA!r}, got {manifest.get('schema')!r})")
    return manifest


def load_boot_image(boot_dir: str | Path, registry_factory, *,
                    game_root: str | Path | None = None):
    """Reconstruct a live ``Win16Machine`` from a data-only boot image —
    the EXE-free load path (no NE parsing, no executable read).

    ``registry_factory`` builds the port's API surface WITHOUT the loader
    (e.g. ``lambda: win16.api.surface.build_registry(winflags=...)``); the
    resolved import slot table is restored from the manifest (it was assigned
    in relocation order at build time and is not re-derivable without the
    EXE) and cross-checked against the fresh registry's equates.

    Returns ``(machine, manifest)``."""
    from dos_re.cpu import CPU8086, CPUState
    from dos_re.memory import Memory

    boot = Path(boot_dir)
    manifest = load_boot_manifest(boot)
    meta = json.loads((boot / "state.json").read_text())

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
    cpu = CPU8086(mem, CPUState(**meta["cpu"]))
    cpu.trace_enabled = False
    machine.cpu = cpu
    cpu.interrupt_handler = machine.interrupt
    api.install(cpu, manifest["thunk_seg"])

    restore_machine_state(machine, boot, meta)

    if manifest.get("poison", {}).get("enabled"):
        # The recovered code is zeroed by design: entry-signature guards must
        # not compare against the poisoned bytes.
        cpu.code_poisoned = True
    if game_root is not None:
        machine.api.services["system"].file_root = Path(game_root)
    return machine, manifest


def boot_vmless_machine(boot_dir: str | Path, registry_factory, *,
                        lift_dir: str | Path,
                        skip: frozenset[str] = frozenset(),
                        game_root: str | Path | None = None,
                        install_graph: bool = True,
                        arm_wall: bool = True):
    """The strict-VMless boot: EXE-free image load, full lifted-graph
    install, interpreter poison armed from instruction zero.

    ``skip`` is the port's keep-interpreted set: a strict boot REFUSES to
    start while any entry is configured for interpreted execution — that is
    the hard wall gate (an entry merely OUTSIDE the corpus is fine: reaching
    it raises).  Returns ``(machine, manifest, installed)``."""
    from dos_re.lift.install import activate_generated_graph

    machine, manifest = load_boot_image(boot_dir, registry_factory,
                                        game_root=game_root)
    installed = {}
    if install_graph:
        if skip:
            raise GeneratedGraphBootstrapError(
                "the generated-graph wall is not satisfied -- "
                f"{len(skip)} routine(s) configured for interpreted "
                f"execution ({', '.join(sorted(skip))})")
        installed = activate_generated_graph(machine.cpu, Path(lift_dir))
    if arm_wall:
        # THE PHYSICAL WALL: interpretation is now impossible; any uncovered
        # address raises rather than falling back to the interpreter.
        machine.cpu.interp_forbidden = True
    return machine, manifest, installed


# --- audit -----------------------------------------------------------------

def audit_boot_image(boot_dir: str | Path, ir_path: str | Path):
    """Verify a generated boot image is a legitimate data-only artifact.

    Returns ``(fails, info)`` — lists of finding strings; empty ``fails``
    means the audit passes.  Checks:

    1. no bundled executable (MZ header, executable suffix, or content
       hashing to the recorded source EXE) anywhere under the image dir,
       and the pickled program identity carries no raw bytes;
    2. provenance present (source EXE hash + region map);
    3. every IR-decoded instruction byte is ZERO unless declared
       code_as_data; manifest accounting consistent;
    4. CODE-SEGMENT COVERAGE (the win16 strengthening): every nonzero byte
       inside a code segment's loaded extent is either IR-decoded (case 3
       zeroed it) or inside a declared code_as_data range — nothing survives
       unaccounted."""
    boot = Path(boot_dir)
    fails: list[str] = []
    info: list[str] = []
    manifest = load_boot_manifest(boot)
    exe_hash = manifest.get("source_exe", {}).get("sha256")

    # 1. no bundled executable
    for f in sorted(boot.rglob("*")):
        if not f.is_file():
            continue
        head = f.read_bytes()[:2]
        if head in (b"MZ", b"ZM"):
            fails.append(f"bundled executable image: {f.name} has an MZ header")
        if f.suffix.lower() in (".exe", ".com", ".dll"):
            fails.append(f"executable-suffixed file in image dir: {f.name}")
        if exe_hash and hashlib.sha256(f.read_bytes()).hexdigest() == exe_hash:
            fails.append(f"file {f.name} IS the source EXE (hash match) — "
                         f"renaming does not launder it")
    program = pickle.loads((boot / manifest["artifacts"]["program"]).read_bytes())
    if getattr(program, "raw", b""):
        fails.append("program.pickle carries raw executable bytes")

    # 2. provenance
    if not exe_hash:
        fails.append("manifest has no source_exe.sha256 (no provenance)")
    if not manifest.get("regions"):
        fails.append("manifest has no region map")

    poison = manifest.get("poison", {})
    if not poison.get("enabled"):
        fails.append("poison is DISABLED — the image carries recovered code")
        return fails, info

    image = (boot / manifest["artifacts"]["memory"]).read_bytes()
    ir = json.loads(Path(ir_path).read_text(encoding="utf-8"))
    keep: set[int] = set()
    for start, length in manifest.get("code_as_data", {}).get("ranges", ()):
        keep.update(range(start, start + length))

    # 3. poison completeness over the IR's instruction ranges
    decoded: set[int] = set()
    present = 0
    offenders: list[int] = []
    for start, length in instruction_ranges(ir):
        decoded.update(range(start, start + length))
        for off in range(start, start + length):
            if off in keep:
                continue
            if image[off] != 0:
                present += 1
                if len(offenders) < 8:
                    offenders.append(off)
    if present:
        fails.append(f"{present} recovered code byte(s) present (non-zero) "
                     f"and NOT declared code_as_data — first at "
                     f"{', '.join(hex(o) for o in offenders)}")
    if poison.get("code_bytes_present_after", 1) != 0:
        fails.append("manifest records code_bytes_present_after != 0")

    # 4. code-segment coverage (the win16 strengthening): a code segment in
    # the image contains NOTHING except declared code_as_data — every other
    # byte (decoded instructions, alignment NOPs, unreached code, undeclared
    # inline data) is zero.
    unaccounted = 0
    first: list[str] = []
    for region in manifest["regions"]:
        if region["kind"] != "code":
            continue
        lo = region["start"]
        hi = lo + region["loaded"]
        for off in range(lo, hi):
            if image[off] == 0 or off in keep:
                continue
            unaccounted += 1
            if len(first) < 8:
                first.append(f"seg{region['ne_seg']}+{off - lo:#06x}")
        info.append(f"code seg{region['ne_seg']}: {hi - lo} loaded bytes")
    if unaccounted:
        fails.append(
            f"{unaccounted} nonzero code-segment byte(s) not declared "
            f"code_as_data — first at {', '.join(first)}; declare them "
            f"(code_as_data fact) or rebuild the image")
    info.append(f"poison: {poison.get('poisoned_bytes')} bytes / "
                f"{poison.get('poisoned_runs')} runs "
                f"+ {poison.get('undeclared_bytes_zeroed', 0)} undeclared "
                f"bytes zeroed; code_as_data ranges: "
                f"{len(manifest.get('code_as_data', {}).get('ranges', ()))}")
    return fails, info


def mask_ranges_from_manifest(manifest: dict) -> list[tuple[int, int]]:
    """Every (start, length) range the boot image zeroed — the digest mask
    for comparing a poisoned-image run against an EXE-full oracle run
    (``win16.vmsnap.digest(machine, mask_ranges=...)``)."""
    p = manifest.get("poison", {})
    return ([(int(s), int(n)) for s, n in p.get("ranges", ())]
            + [(int(s), int(n)) for s, n in p.get("undeclared_runs", ())])


def independence_report(manifest: dict, *,
                        exe_present_at_runtime: bool = False) -> str:
    """The DERIVED hard-gate banner (each line a computed fact, not a config
    string) — the win16 mirror of ``dos_re.independence.independence_report``."""
    p = manifest.get("poison", {})
    holds = (not exe_present_at_runtime
             and p.get("enabled")
             and p.get("code_bytes_present_after", 1) == 0)
    lines = [
        f"Boot source: generated data-only boot image "
        f"({manifest['artifacts']['memory']} + {manifest['artifacts']['state']}"
        f" + {manifest['artifacts']['system']} + {manifest['artifacts']['program']})",
        f"Original EXE required at runtime: no "
        f"(source {manifest['source_exe']['name']} sha256 "
        f"{manifest['source_exe']['sha256'][:12]}... consumed at BUILD time only)",
        f"Recovered code poisoned: {p.get('poisoned_bytes', 0)} bytes in "
        f"{p.get('poisoned_runs', 0)} runs; "
        f"code bytes still present: {p.get('code_bytes_present_after', '?')}",
        "Interpreter fallback: forbidden (wall poison armed)",
        f"EXE-independence wall: {'HOLDS' if holds else 'DOES NOT HOLD'}",
    ]
    return "\n".join(lines)
