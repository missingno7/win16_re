"""Static call extraction over a loaded NE image — bottom-up recovery support.

Game-agnostic: works on the loaded machine (relocations applied, import
thunks resolved), so a far-call operand in memory is the REAL target — either
the import thunk segment (an OS API call, nameable via ``cpu.hook_names``) or
another game segment's paragraph base.  The game package supplies routine
names/spans (e.g. from a shipped .SYM) and interprets the result; this module
only extracts call sites.

Triage-grade by design, like the profiler: routine spans in code segments can
contain data (jump tables, string constants), which the length walker crosses
noisily.  Every scan returns its anomaly count so consumers can distrust
noisy ranges; recovery decisions are always confirmed against a disassembly.
"""
from __future__ import annotations

from dataclasses import dataclass

from .insn import walk
from .loader import THUNK_SEG


@dataclass(frozen=True)
class Call:
    site: int               # offset of the call instruction in its segment
    kind: str               # "near" | "far" | "api" | "far_unmapped"
                            # | "indirect_near" | "indirect_far"
    seg: int | None         # NE segment of the target (near/far)
    off: int | None         # target offset (near/far), raw seg16 for unmapped
    api: str | None         # "api:MODULE.ordinal" label for thunk calls


def calls_in_range(machine, seg_index: int, lo: int, hi: int
                   ) -> tuple[list[Call], int]:
    """All call sites in [lo, hi) of NE segment `seg_index` (1-based).

    Returns (calls, anomaly_count).  Near-call targets are segment-local
    offsets; far calls resolve through the loaded seg_bases (game segment) or
    THUNK_SEG (API, named from cpu.hook_names); anything else is
    "far_unmapped" — almost always the walker crossing data, not code.
    """
    base = machine.seg_bases[seg_index] * 16
    code = bytes(machine.mem.data[base:base + hi])
    base_to_seg = {b: i for i, b in enumerate(machine.seg_bases) if b}
    calls: list[Call] = []
    anomalies = 0
    for ins in walk(code, lo, hi):
        if ins.anomaly:
            anomalies += 1
            continue
        op = ins.opcode
        end = ins.pos + ins.length
        if op == 0xE8:
            rel = int.from_bytes(code[end - 2:end], "little")
            target = (end + rel) & 0xFFFF
            calls.append(Call(ins.pos, "near", seg_index, target, None))
        elif op == 0x9A:
            off16 = int.from_bytes(code[end - 4:end - 2], "little")
            seg16 = int.from_bytes(code[end - 2:end], "little")
            if seg16 == THUNK_SEG:
                label = machine.cpu.hook_names.get(
                    (THUNK_SEG, off16), f"api:slot@{off16:#06x}")
                calls.append(Call(ins.pos, "api", None, off16, label))
            elif seg16 in base_to_seg:
                calls.append(Call(ins.pos, "far", base_to_seg[seg16], off16,
                                  None))
            else:
                calls.append(Call(ins.pos, "far_unmapped", None, seg16, None))
        elif op == 0xFF and ins.modrm is not None:
            reg = (ins.modrm >> 3) & 7
            if reg == 2:
                calls.append(Call(ins.pos, "indirect_near", None, None, None))
            elif reg == 3:
                calls.append(Call(ins.pos, "indirect_far", None, None, None))
    return calls, anomalies
