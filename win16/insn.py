"""16-bit x86 instruction-length walker — static-analysis support, decode only.

Enough of the 8086/80186 opcode map to step from one instruction start to the
next without executing: prefixes, modrm+displacement sizes, immediate sizes,
and the F6/F7 groups whose immediate depends on the modrm reg field.  This is
NOT a disassembler and NOT the CPU (dos_re's decoder is fused with execution
by design); it exists so tools like the call-graph extractor can walk routine
bodies without misreading operand bytes as opcodes.

One Win16 quirk is load-bearing: a no-x87 build keeps the MS floating-point
emulator interrupt forms in its code — ``CD 34..CD 3B`` is an x87 escape
(D8..DF) with the ORIGINAL modrm+displacement following the int, ``CD 3C``
carries an extra segment-override byte before the modrm, and ``CD 3D`` is a
bare FWAIT.  A walker that treats those as plain 2-byte INTs desynchronizes on
every FP site.

Unknown/286+ opcodes don't raise: the caller is doing triage over possibly
data-polluted ranges (jump tables live in code segments), so the walker flags
the instruction as an anomaly, advances one byte, and keeps going.  Consumers
count anomalies and treat noisy ranges with suspicion.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

_SEG_PREFIXES = {0x26, 0x2E, 0x36, 0x3E}
_REP_LOCK_PREFIXES = {0xF0, 0xF2, 0xF3}

# opcode -> (has_modrm, immediate_bytes).  Built once; specials handled in code.
_SPEC: dict[int, tuple[bool, int]] = {}


def _fill(rng, modrm: bool, imm: int) -> None:
    for op in rng:
        _SPEC[op] = (modrm, imm)


# ALU rows 00..3F: +0..3 modrm, +4 imm8, +5 imm16, +6/+7 single byte
for _row in range(0x00, 0x40, 0x08):
    _fill(range(_row, _row + 4), True, 0)
    _SPEC[_row + 4] = (False, 1)
    _SPEC[_row + 5] = (False, 2)
    _SPEC[_row + 6] = (False, 0)
    _SPEC[_row + 7] = (False, 0)
del _SPEC[0x0F]                             # 286+ escape (8086 POP CS): anomaly
_fill(range(0x40, 0x60), False, 0)          # inc/dec/push/pop reg
_fill((0x60, 0x61), False, 0)               # pusha/popa (186)
_SPEC[0x62] = (True, 0)                     # bound (186)
_SPEC[0x68] = (False, 2)                    # push imm16 (186)
_SPEC[0x69] = (True, 2)                     # imul r,rm,imm16 (186)
_SPEC[0x6A] = (False, 1)                    # push imm8 (186)
_SPEC[0x6B] = (True, 1)                     # imul r,rm,imm8 (186)
_fill(range(0x6C, 0x70), False, 0)          # ins/outs (186)
_fill(range(0x70, 0x80), False, 1)          # Jcc short
_SPEC[0x80] = (True, 1)
_SPEC[0x81] = (True, 2)
_SPEC[0x82] = (True, 1)                     # alias of 80
_SPEC[0x83] = (True, 1)
_fill(range(0x84, 0x90), True, 0)           # test/xchg/mov/lea/pop rm
_fill(range(0x90, 0x9A), False, 0)          # nop/xchg/cbw/cwd
_SPEC[0x9A] = (False, 4)                    # call far seg:off
_fill(range(0x9B, 0xA0), False, 0)          # wait/pushf/popf/sahf/lahf
_fill(range(0xA0, 0xA4), False, 2)          # mov acc<->moffs16
_fill(range(0xA4, 0xA8), False, 0)          # movs/cmps
_SPEC[0xA8] = (False, 1)
_SPEC[0xA9] = (False, 2)
_fill(range(0xAA, 0xB0), False, 0)          # stos/lods/scas
_fill(range(0xB0, 0xB8), False, 1)          # mov r8,imm8
_fill(range(0xB8, 0xC0), False, 2)          # mov r16,imm16
_SPEC[0xC0] = (True, 1)                     # shift rm,imm8 (186)
_SPEC[0xC1] = (True, 1)
_SPEC[0xC2] = (False, 2)                    # ret imm16
_SPEC[0xC3] = (False, 0)
_SPEC[0xC4] = (True, 0)                     # les
_SPEC[0xC5] = (True, 0)                     # lds
_SPEC[0xC6] = (True, 1)
_SPEC[0xC7] = (True, 2)
_SPEC[0xC8] = (False, 3)                    # enter imm16,imm8 (186)
_SPEC[0xC9] = (False, 0)                    # leave (186)
_SPEC[0xCA] = (False, 2)                    # retf imm16
_fill((0xCB, 0xCC), False, 0)
_SPEC[0xCD] = (False, 1)                    # int imm8 (FP-emu forms special-cased)
_fill((0xCE, 0xCF), False, 0)
_fill(range(0xD0, 0xD4), True, 0)           # shift rm,1 / rm,cl
_fill((0xD4, 0xD5), False, 1)               # aam/aad
_fill((0xD6, 0xD7), False, 0)               # salc/xlat
_fill(range(0xD8, 0xE0), True, 0)           # x87 escapes (if osfixups applied)
_fill(range(0xE0, 0xE8), False, 1)          # loop/jcxz/in/out imm8
_fill((0xE8, 0xE9), False, 2)               # call/jmp near rel16
_SPEC[0xEA] = (False, 4)                    # jmp far seg:off
_SPEC[0xEB] = (False, 1)                    # jmp short
_fill(range(0xEC, 0xF0), False, 0)          # in/out dx
_SPEC[0xF1] = (False, 0)
_fill((0xF4, 0xF5), False, 0)               # hlt/cmc
_SPEC[0xF6] = (True, 0)                     # group3 byte: imm8 iff /0 or /1
_SPEC[0xF7] = (True, 0)                     # group3 word: imm16 iff /0 or /1
_fill(range(0xF8, 0xFE), False, 0)          # clc..std
_SPEC[0xFE] = (True, 0)                     # group4
_SPEC[0xFF] = (True, 0)                     # group5 (inc/dec/call/jmp/push rm)


def _disp_len(modrm: int) -> int:
    mod, rm = modrm >> 6, modrm & 7
    if mod == 0:
        return 2 if rm == 6 else 0
    if mod == 1:
        return 1
    if mod == 2:
        return 2
    return 0


@dataclass(frozen=True)
class Insn:
    pos: int                # offset of the first byte (prefixes included)
    opcode: int             # the opcode byte (after prefixes)
    modrm: int | None
    length: int             # total length, prefixes included
    anomaly: bool           # unknown opcode — length is a 1-byte guess


def decode_len(code: bytes, pos: int) -> Insn:
    """Length-decode the instruction at `pos`; never raises inside `code`."""
    start = pos
    while pos < len(code) and (code[pos] in _SEG_PREFIXES
                               or code[pos] in _REP_LOCK_PREFIXES):
        pos += 1
    if pos >= len(code):
        return Insn(start, 0, None, len(code) - start or 1, True)
    op = code[pos]
    spec = _SPEC.get(op)
    if spec is None:
        return Insn(start, op, None, pos - start + 1, True)
    has_modrm, imm = spec
    length = pos - start + 1
    modrm = None
    if op == 0xCD and pos + 1 < len(code) and 0x34 <= code[pos + 1] <= 0x3D:
        # MS FP-emulator interrupt forms (no-x87 build): the original x87
        # modrm+displacement follows the INT.
        vec = code[pos + 1]
        length += 1                              # the vector byte
        if vec <= 0x3B:                          # CD 34..3B == D8..DF escape
            if pos + 2 < len(code):
                modrm = code[pos + 2]
                length += 1 + _disp_len(modrm)
        elif vec == 0x3C:                        # seg-override form: extra byte
            if pos + 3 < len(code):
                modrm = code[pos + 3]
                length += 2 + _disp_len(modrm)
        # CD 3D: bare FWAIT — nothing follows.
        return Insn(start, op, modrm, length, False)
    if has_modrm:
        if pos + 1 >= len(code):
            return Insn(start, op, None, length, True)
        modrm = code[pos + 1]
        length += 1 + _disp_len(modrm)
        if op in (0xF6, 0xF7) and (modrm >> 3) & 7 in (0, 1):
            imm = 1 if op == 0xF6 else 2         # TEST rm,imm
    return Insn(start, op, modrm, length + imm, False)


def walk(code: bytes, lo: int, hi: int) -> Iterator[Insn]:
    """Yield instructions from `lo` until the next start would reach `hi`."""
    pos = lo
    while pos < hi:
        ins = decode_len(code, pos)
        yield ins
        pos = ins.pos + ins.length
