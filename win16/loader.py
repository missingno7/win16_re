"""NE image loader: maps segments into the dos_re VM and applies relocations.

Model: real-mode-style flat mapping.  Each NE segment is assigned a paragraph
base; its "selector" IS that paragraph value (Win16 code treats selectors as
opaque, so this is sound until an executable proves otherwise — huge-pointer
arithmetic would surface here and fail loud).

Imports resolve through an ApiRegistry to slots in a dedicated thunk segment;
a replacement hook at each slot's CS:IP services the API in Python.  Floating
point OSFIXUP relocations are deliberately NOT applied: the in-file bytes keep
their INT 34h..3Dh forms (the no-x87 configuration) and those interrupts are
serviced by the machine's interrupt handler.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from dos_re.cpu import CPU8086, CPUState
from dos_re.memory import Memory

from .api.core import ApiRegistry, Win16ApiGap
from .ne import (
    ADDR_FARADDR32, ADDR_LOBYTE, ADDR_OFFSET16, ADDR_SEGMENT16,
    NEExecutable, Segment,
    TARGET_IMPORTNAME, TARGET_IMPORTORDINAL, TARGET_INTERNALREF, TARGET_OSFIXUP,
)

THUNK_SEG = 0x0060          # import thunk slots live here (hooked CS:IP values)
IMAGE_BASE_PARA = 0x0100    # first NE segment maps at this paragraph

# Win16 uses selector translation to lift the 1MB real-mode ceiling: the loaded
# program's own segments stay in low real-mode memory (< 1MB); GlobalAlloc
# blocks live above it as selectors mapping into the linear space
# [GLOBAL_LIN_START, WIN16_MEM_SIZE).  The gap below GLOBAL_LIN_START avoids the
# dos_re EGA shadow region at 0x100000 (unused by Win16 but reserved).
WIN16_MEM_SIZE = 0x400000       # 4 MB
GLOBAL_LIN_START = 0x140000     # global heap starts here (after EGA shadow)


class LoaderError(RuntimeError):
    pass


@dataclass
class Win16Machine:
    exe: NEExecutable
    cpu: CPU8086
    mem: Memory
    api: ApiRegistry
    seg_bases: list[int]                    # 1-based NE segment -> paragraph base
    free_para: int                          # bump allocator frontier (paragraphs)
    osfixups: list[tuple[int, int, int]] = field(default_factory=list)  # (seg, off, kind)

    def seg_base(self, ne_segment: int) -> int:
        return self.seg_bases[ne_segment]

    def alloc_paragraphs(self, paras: int) -> int:
        """Allocate a paragraph-aligned block; returns its segment value."""
        base = self.free_para
        if (base + paras) << 4 > self.mem.size:
            raise LoaderError("out of VM memory")
        self.free_para += paras
        return base

    def interrupt(self, cpu: CPU8086, num: int) -> None:
        raise Win16ApiGap(
            f"INT {num:02X}h at {cpu.s.cs:04X}:{cpu.s.ip:04X} — no Win16 service installed")


def load_ne(exe: NEExecutable, api: ApiRegistry, *,
            extra_handlers: bool = True) -> Win16Machine:
    """Load an NE executable into a fresh VM, ready to run from its entry point."""
    hdr = exe.header
    mem = Memory(size=WIN16_MEM_SIZE, sel_base={})
    seg_bases = [0] * (len(exe.segments) + 1)

    # --- place segments ---
    para = IMAGE_BASE_PARA
    for seg in exe.segments:
        alloc = seg.alloc_size
        if seg.index == hdr.auto_data_seg:
            # DGROUP = static data + stack + local heap, one 64K-max segment.
            alloc = alloc + hdr.stack_size + hdr.heap_size
            if alloc > 0x10000:
                raise LoaderError(f"DGROUP overflows 64K ({alloc:#x})")
        seg_bases[seg.index] = para
        mem.load(para, 0, exe.segment_bytes(seg))
        para += (alloc + 15) >> 4

    machine = Win16Machine(exe=exe, cpu=None, mem=mem, api=api,  # type: ignore[arg-type]
                           seg_bases=seg_bases, free_para=para)

    # --- apply relocations ---
    for seg in exe.segments:
        _apply_relocations(machine, seg)

    # --- mark thunk slots with INT3 tripwires (hooks intercept before decode) ---
    for (_mod, _ordn), off in api.slots.items():
        for i in range(ApiRegistry.SLOT_STRIDE):
            mem.wb(THUNK_SEG, off + i, 0xCC)

    # --- initial CPU state, per NE conventions ---
    if hdr.initial_ss_seg == hdr.auto_data_seg and hdr.initial_sp == 0:
        # Loader-provided stack: SP = top of static data + stack area.
        data_len = exe.segments[hdr.auto_data_seg - 1].alloc_size
        sp = (data_len + hdr.stack_size) & ~1
    else:
        sp = hdr.initial_sp
    dgroup = seg_bases[hdr.auto_data_seg] if hdr.auto_data_seg else 0
    state = CPUState(
        ax=0, bx=0, cx=0, dx=0, si=0, di=0, bp=0,
        sp=sp,
        cs=seg_bases[hdr.entry_seg], ip=hdr.entry_ip,
        ds=dgroup, es=dgroup,
        ss=seg_bases[hdr.initial_ss_seg] if hdr.initial_ss_seg else dgroup,
    )
    cpu = CPU8086(mem, state)
    machine.cpu = cpu
    cpu.interrupt_handler = machine.interrupt
    api.install(cpu, THUNK_SEG)
    return machine


def _apply_relocations(machine: Win16Machine, seg: Segment) -> None:
    exe, mem, api = machine.exe, machine.mem, machine.api
    base = machine.seg_bases[seg.index]

    for rel in seg.relocations:
        # -- resolve the target value (tgt_seg, tgt_off) --
        tt = rel.target_type
        if tt == TARGET_INTERNALREF:
            if rel.target1 == 0xFF:
                ep = next((e for e in exe.entry_points if e.ordinal == rel.target2), None)
                if ep is None:
                    raise LoaderError(f"internal ref to unknown entry ordinal {rel.target2}")
                tgt_seg, tgt_off = machine.seg_bases[ep.segment], ep.offset
            else:
                tgt_seg, tgt_off = machine.seg_bases[rel.target1], rel.target2
        elif tt in (TARGET_IMPORTORDINAL, TARGET_IMPORTNAME):
            module = exe.modules[rel.target1 - 1]
            if tt == TARGET_IMPORTORDINAL:
                kind, value = api.resolve_import(module, rel.target2)
            else:
                name = exe.import_name(rel.target2)
                raise LoaderError(f"import by name not yet needed: {module}.{name}")
            if kind == "equate":
                if rel.addr_type != ADDR_OFFSET16 or rel.additive:
                    raise LoaderError(
                        f"equate import {module}.{rel.target2} with addr_type "
                        f"{rel.addr_type} additive={rel.additive} — unsupported")
                tgt_seg, tgt_off = 0, value
            else:
                tgt_seg, tgt_off = THUNK_SEG, value
        elif tt == TARGET_OSFIXUP:
            # Floating-point fixups: deliberately unapplied (no x87 in the VM;
            # the INT 34h..3Dh emulator forms stay in place).  Recorded so the
            # FP service layer can audit which sites exist.
            machine.osfixups.append((seg.index, rel.offset, rel.target1))
            continue
        else:
            raise LoaderError(f"unknown reloc target type {tt}")

        # -- patch (chained when non-additive) --
        offset = rel.offset
        seen = 0
        while True:
            if rel.additive:
                _patch(mem, base, offset, rel.addr_type, tgt_seg, tgt_off, additive=True)
                break
            next_off = mem.rw(base, offset)  # chain link lives at the patch site
            _patch(mem, base, offset, rel.addr_type, tgt_seg, tgt_off, additive=False)
            if next_off == 0xFFFF:
                break
            if next_off == offset or seen > 0x4000:
                raise LoaderError(f"relocation chain loop in segment {seg.index} at {offset:#x}")
            offset = next_off
            seen += 1


def _patch(mem: Memory, seg_para: int, offset: int, addr_type: int,
           tgt_seg: int, tgt_off: int, *, additive: bool) -> None:
    if addr_type == ADDR_FARADDR32:
        if additive:
            raise LoaderError("additive far32 fixup not yet needed")
        mem.ww(seg_para, offset, tgt_off)
        mem.ww(seg_para, (offset + 2) & 0xFFFF, tgt_seg)
    elif addr_type == ADDR_OFFSET16:
        value = (mem.rw(seg_para, offset) + tgt_off) & 0xFFFF if additive else tgt_off
        mem.ww(seg_para, offset, value)
    elif addr_type == ADDR_SEGMENT16:
        if additive:
            raise LoaderError("additive segment16 fixup not yet needed")
        mem.ww(seg_para, offset, tgt_seg)
    elif addr_type == ADDR_LOBYTE:
        value = (mem.rb(seg_para, offset) + tgt_off) & 0xFF if additive else tgt_off & 0xFF
        mem.wb(seg_para, offset, value)
    else:
        raise LoaderError(f"unknown reloc addr type {addr_type}")
