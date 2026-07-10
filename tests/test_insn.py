"""The 16-bit instruction-length walker: hand-assembled vectors.

Each vector is (bytes, expected_length) for the FIRST instruction; the walk
test then checks resynchronization across a realistic MSC-style prologue.
"""
from __future__ import annotations

import pytest

from win16.insn import decode_len, walk

VECTORS = [
    (bytes.fromhex("55"), 1),                    # push bp
    (bytes.fromhex("8bec"), 2),                  # mov bp,sp
    (bytes.fromhex("8b4606"), 3),                # mov ax,[bp+6]
    (bytes.fromhex("8b873412"), 4),              # mov ax,[bx+0x1234]
    (bytes.fromhex("a13412"), 3),                # mov ax,[0x1234]
    (bytes.fromhex("83c408"), 3),                # add sp,8
    (bytes.fromhex("81c40001"), 4),              # add sp,0x100
    (bytes.fromhex("f7260020"), 4),              # mul word [0x2000]   (no imm)
    (bytes.fromhex("f70600203412"), 6),          # test word [0x2000],0x1234
    (bytes.fromhex("f6c101"), 3),                # test cl,1
    (bytes.fromhex("9a0c026000"), 5),            # call far 0060:020C
    (bytes.fromhex("e8fdff"), 3),                # call near
    (bytes.fromhex("2e8b07"), 3),                # cs: mov ax,[bx]
    (bytes.fromhex("f3a4"), 2),                  # rep movsb
    (bytes.fromhex("cd21"), 2),                  # int 21h
    (bytes.fromhex("cd340600 2000".replace(" ", "")), 5),  # fpu-int fadd [0x0020]
    (bytes.fromhex("cd34c1"), 3),                # fpu-int, reg form (mod=3)
    (bytes.fromhex("cd3d"), 2),                  # fpu FWAIT form
    (bytes.fromhex("c8040000"), 4),              # enter 4,0
    (bytes.fromhex("c20800"), 3),                # ret 8
    (bytes.fromhex("6a05"), 2),                  # push 5
    (bytes.fromhex("683412"), 3),                # push 0x1234
    (bytes.fromhex("6bc028"), 3),                # imul ax,ax,0x28
    (bytes.fromhex("d1e0"), 2),                  # shl ax,1
    (bytes.fromhex("c1e003"), 3),                # shl ax,3 (186)
    (bytes.fromhex("ff7608"), 3),                # push word [bp+8]
    (bytes.fromhex("ff1e0020"), 4),              # call far [0x2000]
    (bytes.fromhex("ea00016000"), 5),            # jmp far 0060:0100
    (bytes.fromhex("60"), 1),                    # pusha (186)
    (bytes.fromhex("c47e0a"), 3),                # les di,[bp+0xA]
]


@pytest.mark.parametrize("code,length", VECTORS,
                         ids=[v[0].hex() for v in VECTORS])
def test_single_instruction_lengths(code, length):
    ins = decode_len(code + b"\x90" * 4, 0)
    assert not ins.anomaly
    assert ins.length == length


def test_walk_resynchronizes_across_a_real_prologue():
    # push bp / mov bp,sp / sub sp,8 / push si / push di /
    # mov ax,[bp+6] / call near / add sp,2 / pop di / pop si /
    # mov sp,bp / pop bp / ret
    code = bytes.fromhex("558bec83ec0856578b4606e81234"
                         "83c4025f5e8be55dc3")
    starts = [i.pos for i in walk(code, 0, len(code))]
    assert starts == [0, 1, 3, 6, 7, 8, 11, 14, 17, 18, 19, 21, 22]
    assert not any(i.anomaly for i in walk(code, 0, len(code)))


def test_unknown_opcode_is_flagged_not_fatal():
    ins = decode_len(b"\x0f\x90\x90", 0)
    assert ins.anomaly and ins.length == 1


def test_f6_f7_group_imm_depends_on_reg_field():
    assert decode_len(bytes.fromhex("f7d8"), 0).length == 2      # neg ax
    assert decode_len(bytes.fromhex("f7c03412"), 0).length == 4  # test ax,imm16
