"""NE (New Executable, 16-bit Windows) file parser.  Pure, stdlib-only.

Parses exactly what a Win16 loader needs: header, segment table, per-segment
relocation records, module references / imported names, entry table, resident
and non-resident name tables, and the resource table.  Semantics follow the
observed behaviour of real NE files (chained fixups, additive fixups, movable
entry points), implemented as needed by real executables — fail loud on
anything unrecognized rather than guessing.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

# Relocation address types (what gets patched at the source offset).
ADDR_LOBYTE = 0
ADDR_SEGMENT16 = 2
ADDR_FARADDR32 = 3
ADDR_OFFSET16 = 5

# Relocation target types (low 2 bits of the reloc-type byte).
TARGET_INTERNALREF = 0
TARGET_IMPORTORDINAL = 1
TARGET_IMPORTNAME = 2
TARGET_OSFIXUP = 3
RELOC_ADDITIVE = 0x04

# Segment flags.
SEG_DATA = 0x0001          # else code
SEG_MOVABLE = 0x0010
SEG_PRELOAD = 0x0040
SEG_HAS_RELOCS = 0x0100

RESOURCE_TYPE_NAMES = {
    1: "CURSOR", 2: "BITMAP", 3: "ICON", 4: "MENU", 5: "DIALOG",
    6: "STRING", 7: "FONTDIR", 8: "FONT", 9: "ACCELERATOR", 10: "RCDATA",
    12: "GROUP_CURSOR", 14: "GROUP_ICON", 15: "NAMETABLE", 16: "VERSION",
}


@dataclass(frozen=True)
class NEHeader:
    ne_offset: int
    linker_version: tuple[int, int]
    flags: int
    auto_data_seg: int          # 1-based segment number of DGROUP (0 = none)
    heap_size: int
    stack_size: int
    entry_seg: int              # 1-based segment number
    entry_ip: int
    initial_ss_seg: int         # 1-based segment number
    initial_sp: int
    segment_count: int
    module_count: int
    align_shift: int
    target_os: int


@dataclass(frozen=True)
class Relocation:
    addr_type: int              # ADDR_* constant
    flags: int                  # target type in low 2 bits, RELOC_ADDITIVE bit
    offset: int                 # patch site offset within the segment
    target1: int
    target2: int

    @property
    def target_type(self) -> int:
        return self.flags & 0x03

    @property
    def additive(self) -> bool:
        return bool(self.flags & RELOC_ADDITIVE)


@dataclass(frozen=True)
class Segment:
    index: int                  # 1-based, as NE numbers them
    file_offset: int            # 0 = no file data (bss-like)
    file_length: int            # bytes present in the file
    min_alloc: int              # bytes to allocate (0 means 0x10000)
    flags: int
    relocations: tuple[Relocation, ...]

    @property
    def is_data(self) -> bool:
        return bool(self.flags & SEG_DATA)

    @property
    def alloc_size(self) -> int:
        return self.min_alloc or 0x10000


@dataclass(frozen=True)
class EntryPoint:
    ordinal: int
    segment: int                # 1-based segment number
    offset: int
    flags: int
    movable: bool


@dataclass(frozen=True)
class Resource:
    type_id: int | None         # numeric type id, or None if named type
    type_name: str              # "BITMAP", "#42", or the string name
    res_id: int | None          # numeric id, or None if named
    res_name: str
    flags: int
    data: bytes


@dataclass(frozen=True)
class NEExecutable:
    path: Path
    raw: bytes
    header: NEHeader
    segments: tuple[Segment, ...]
    modules: tuple[str, ...]            # referenced module names, in modref order
    imported_names: dict[int, str]      # offset in imported-names table -> name
    entry_points: tuple[EntryPoint, ...]
    resident_names: tuple[tuple[str, int], ...]
    resources: tuple[Resource, ...]
    resource_name_map: dict[tuple[int, str], int]
    # (type_id, NAME) -> numeric resource id, from the Win3.x NAMETABLE
    # resource (type 15) that rc.exe emits when named resources are stored
    # under numeric ids.

    def segment_bytes(self, seg: Segment) -> bytes:
        """The segment's file image, zero-padded to its allocation size."""
        data = self.raw[seg.file_offset:seg.file_offset + seg.file_length]
        if len(data) != seg.file_length:
            raise ValueError(f"segment {seg.index}: file data truncated")
        return data + b"\x00" * (seg.alloc_size - len(data))

    def import_name(self, name_offset: int) -> str:
        try:
            return self.imported_names[name_offset]
        except KeyError:
            raise ValueError(f"no imported name at table offset {name_offset:#x}") from None

    def find_resources(self, type_name: str) -> list[Resource]:
        return [r for r in self.resources if r.type_name == type_name]

    def lookup_resource(self, type_name: str, name: int | str) -> Resource | None:
        """Resolve a LoadBitmap/LoadIcon-style name: integer atom, direct
        string name, or a NAMETABLE-mapped name."""
        pool = self.find_resources(type_name)
        if isinstance(name, int):
            return next((r for r in pool if r.res_id == name), None)
        upper = name.upper()
        direct = next((r for r in pool if r.res_name.upper() == upper), None)
        if direct is not None:
            return direct
        type_id = next((r.type_id for r in pool if r.type_id is not None), None)
        if type_id is None:
            return None
        mapped = self.resource_name_map.get((type_id, upper))
        if mapped is None:
            return None
        return next((r for r in pool if r.res_id == mapped), None)


def _pascal_string(raw: bytes, off: int) -> str:
    n = raw[off]
    return raw[off + 1:off + 1 + n].decode("latin-1")


def parse_ne(path: str | Path) -> NEExecutable:
    path = Path(path)
    raw = path.read_bytes()
    if raw[:2] != b"MZ":
        raise ValueError(f"{path}: not an MZ file")
    ne_off = struct.unpack_from("<I", raw, 0x3C)[0]
    if raw[ne_off:ne_off + 2] != b"NE":
        raise ValueError(f"{path}: no NE header at {ne_off:#x}")

    (_sig, linker_maj, linker_min, entry_tab_off, entry_tab_len, _crc, flags,
     auto_data_seg, heap_size, stack_size, cs_ip, ss_sp, seg_count, mod_count,
     _nonres_size, seg_tab_off, res_tab_off, resname_tab_off, modref_tab_off,
     impname_tab_off, _nonres_off, _movable_entries, align_shift, _res_count,
     target_os) = struct.unpack_from("<HBBHHIHHHHIIHHHHHHHHIHHHB", raw, ne_off)

    align_shift = align_shift or 9
    header = NEHeader(
        ne_offset=ne_off,
        linker_version=(linker_maj, linker_min),
        flags=flags,
        auto_data_seg=auto_data_seg,
        heap_size=heap_size,
        stack_size=stack_size,
        entry_seg=cs_ip >> 16,
        entry_ip=cs_ip & 0xFFFF,
        initial_ss_seg=ss_sp >> 16,
        initial_sp=ss_sp & 0xFFFF,
        segment_count=seg_count,
        module_count=mod_count,
        align_shift=align_shift,
        target_os=target_os,
    )

    # --- segment table + per-segment relocations ---
    segments: list[Segment] = []
    for i in range(seg_count):
        sector, length, sflags, minalloc = struct.unpack_from(
            "<HHHH", raw, ne_off + seg_tab_off + i * 8)
        file_off = sector << align_shift if sector else 0
        file_len = (length or 0x10000) if sector else 0
        relocs: list[Relocation] = []
        if sflags & SEG_HAS_RELOCS:
            p = file_off + file_len
            (nrel,) = struct.unpack_from("<H", raw, p)
            p += 2
            for _ in range(nrel):
                atype, rflags, roff, t1, t2 = struct.unpack_from("<BBHHH", raw, p)
                p += 8
                relocs.append(Relocation(atype, rflags, roff, t1, t2))
        segments.append(Segment(i + 1, file_off, file_len, minalloc, sflags,
                                tuple(relocs)))

    # --- module references + imported names ---
    modules: list[str] = []
    for i in range(mod_count):
        (name_off,) = struct.unpack_from("<H", raw, ne_off + modref_tab_off + i * 2)
        modules.append(_pascal_string(raw, ne_off + impname_tab_off + name_off))
    imported_names: dict[int, str] = {}
    p = ne_off + impname_tab_off
    end = ne_off + entry_tab_off  # imported-names table runs up to the entry table
    while p < end and raw[p]:
        imported_names[p - (ne_off + impname_tab_off)] = _pascal_string(raw, p)
        p += raw[p] + 1

    # --- entry table (bundles) ---
    entry_points: list[EntryPoint] = []
    p = ne_off + entry_tab_off
    tab_end = p + entry_tab_len
    ordinal = 1
    while p < tab_end:
        count = raw[p]
        if count == 0:
            break
        indicator = raw[p + 1]
        p += 2
        if indicator == 0x00:           # unused bundle
            ordinal += count
            continue
        for _ in range(count):
            if indicator == 0xFF:       # movable: flags, INT 3Fh, segno, offset
                eflags = raw[p]
                segno = raw[p + 3]
                (off,) = struct.unpack_from("<H", raw, p + 4)
                p += 6
                entry_points.append(EntryPoint(ordinal, segno, off, eflags, True))
            else:                       # fixed segment: flags, offset
                eflags = raw[p]
                (off,) = struct.unpack_from("<H", raw, p + 1)
                p += 3
                entry_points.append(EntryPoint(ordinal, indicator, off, eflags, False))
            ordinal += 1

    # --- resident names ---
    resident: list[tuple[str, int]] = []
    p = ne_off + resname_tab_off
    while raw[p]:
        n = raw[p]
        name = raw[p + 1:p + 1 + n].decode("latin-1")
        (ordv,) = struct.unpack_from("<H", raw, p + 1 + n)
        resident.append((name, ordv))
        p += n + 3

    # --- resources ---
    resources: list[Resource] = []
    if res_tab_off != resname_tab_off:  # empty table when the two collide
        base = ne_off + res_tab_off
        (rshift,) = struct.unpack_from("<H", raw, base)
        p = base + 2
        while True:
            (type_id,) = struct.unpack_from("<H", raw, p)
            if type_id == 0:
                break
            (count,) = struct.unpack_from("<H", raw, p + 2)
            if type_id & 0x8000:
                tid: int | None = type_id & 0x7FFF
                tname = RESOURCE_TYPE_NAMES.get(tid, f"#{tid}")
            else:
                tid = None
                tname = _pascal_string(raw, base + type_id)
            p += 8
            for _ in range(count):
                r_off, r_len, r_flags, r_id = struct.unpack_from("<HHHH", raw, p)
                p += 12
                if r_id & 0x8000:
                    rid: int | None = r_id & 0x7FFF
                    rname = f"#{rid}"
                else:
                    rid = None
                    rname = _pascal_string(raw, base + r_id)
                data = raw[r_off << rshift:(r_off << rshift) + (r_len << rshift)]
                resources.append(Resource(tid, tname, rid, rname, r_flags, data))

    # --- NAMETABLE (type 15): (type, id) -> name entries ---
    name_map: dict[tuple[int, str], int] = {}
    for res in resources:
        if res.type_id != 15:
            continue
        p = 0
        data = res.data
        while p + 6 <= len(data):
            (esize,) = struct.unpack_from("<H", data, p)
            if esize == 0:
                break
            type_id, res_id = struct.unpack_from("<HH", data, p + 2)
            # After the header: a zero byte (no type name), then the ASCIIZ name.
            name = data[p + 7:p + esize].split(b"\x00")[0].decode("latin-1")
            if res_id & 0x8000 and name:
                name_map[(type_id & 0x7FFF, name.upper())] = res_id & 0x7FFF
            p += esize

    return NEExecutable(
        path=path,
        raw=raw,
        header=header,
        segments=tuple(segments),
        modules=tuple(modules),
        imported_names=imported_names,
        entry_points=tuple(entry_points),
        resident_names=tuple(resident),
        resources=tuple(resources),
        resource_name_map=name_map,
    )
