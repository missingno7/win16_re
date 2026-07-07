"""MICROMAN lifted islands — the two WAP inner loops that dominate runtime.

PC-sampling (2026-07-07, run_status.md) found >50% of all executed
instructions in two loops of the WAP engine (NE segment 2):

* seg2:8D70-8DB1 — the RLE RUN FILL of WAP's page decoder: writes one byte
  per iteration at a DESCENDING 32-bit offset, recomputing the destination
  selector (`shl hi,3; add base_sel; mov es`) for every byte — ~25
  interpreted instructions per byte written.
* seg2:926C-9297 — the huge-pointer DWORD COPY (page compose/present):
  4 bytes per iteration with the classic `add off,4 / jnc / selector += 8`
  huge-pointer walk on both source and destination.

Both loops address memory through the selector heap, where consecutive
selectors (8 apart) map to consecutive 64K — so each loop's whole effect is
ONE contiguous linear slice operation.  Each island replaces the loop from
its head: it performs all remaining iterations in one shot, writes back the
exact final register/flag/locals state the ASM would have produced, and
jumps to the loop's exit instruction.  Derived from live traces
(artifacts/loop_tr.txt); verified by tests/test_microman_hooks.py, which
runs a hooked and an unhooked machine side by side and requires identical
window pixels at every checkpoint.
"""
from __future__ import annotations

# x86 FLAGS bits (dos_re cpu encoding).
CF, PF, AF, ZF, SF, OF = 0x001, 0x004, 0x010, 0x040, 0x080, 0x800
ARITH = CF | PF | AF | ZF | SF | OF

CODE_SEG_INDEX = 2              # both loops live in NE segment 2

# Code-byte signatures at the hook addresses (refuse to install on mismatch).
FILL_IP = 0x8D70
FILL_EXIT = 0x8DB2
FILL_SIG = bytes.fromhex("8a46fb2bd28946fc8956fe8bf88bf2")     # 8D70..
COPY_IP = 0x926C
COPY_EXIT = 0x9299
COPY_SIG = bytes.fromhex("c476f88346f80473058146fa0800")       # 926C..


def _frame_word(cpu, off: int) -> int:
    return cpu.mem.rw(cpu.s.ss, (cpu.s.bp + off) & 0xFFFF)


def _set_frame_word(cpu, off: int, value: int) -> None:
    cpu.mem.ww(cpu.s.ss, (cpu.s.bp + off) & 0xFFFF, value & 0xFFFF)


def _huge_lin(mem, sel: int) -> int:
    """Linear base of a selector; fail loud off the selector heap — these
    loops only ever address GlobalAlloc'd page buffers."""
    base = mem.sel_base.get(sel & 0xFFFF)
    if base is None:
        raise AssertionError(
            f"microman island: selector {sel:04X} is not a heap selector")
    return base


def _fill_island(cpu) -> None:
    """seg2:8D70 — write COUNT bytes of VALUE at descending huge offsets.

    Frame: [bp-5]=count byte (entry guard 8D6B guarantees nonzero),
    [bp-23]=value, [bp-14/12]=dest offset (dword, decremented per byte),
    [bp-34]=offset bias, [bp-32]=base selector.  Exits at 8DB2.
    """
    s = cpu.s
    mem = cpu.mem
    count = (s.ax & 0xFF00) | cpu.mem.rb(s.ss, (s.bp - 5) & 0xFFFF)
    value = cpu.mem.rb(s.ss, (s.bp - 23) & 0xFFFF)
    dest = (_frame_word(cpu, -12) << 16) | _frame_word(cpu, -14)
    bias = _frame_word(cpu, -34)
    base_sel = _frame_word(cpu, -32)

    start_full = (dest + bias) & 0xFFFFFFFF
    final_full = (start_full - (count - 1)) & 0xFFFFFFFF
    lin = _huge_lin(mem, base_sel)
    mem.data[lin + final_full:lin + start_full + 1] = bytes([value]) * count

    dest_after = (dest - count) & 0xFFFFFFFF
    _set_frame_word(cpu, -14, dest_after)
    _set_frame_word(cpu, -12, dest_after >> 16)
    _set_frame_word(cpu, -4, count)             # mov [bp-4],ax (ax=count|ah)
    _set_frame_word(cpu, -2, 0)

    last_sel = (base_sel + ((final_full >> 16) << 3)) & 0xFFFF
    s.ax = (final_full & 0xFF00) | value
    s.bx = final_full & 0xFFFF
    s.cx = 3
    s.dx = last_sel
    s.es = last_sel
    s.si = 0xFFFF                               # dec si from 0
    s.di = 0
    # CF: last `sbb [bp-12],0` borrows only if dest32 was 0 before the last
    # decrement; then `dec si` (0 -> FFFF) sets SF/AF/PF, clears ZF/OF.
    cf = CF if ((dest - (count - 1)) & 0xFFFFFFFF) == 0 else 0
    s.flags = (s.flags & ~ARITH) | SF | AF | PF | cf
    s.ip = FILL_EXIT


def _copy_island(cpu) -> None:
    """seg2:926C — copy BX dwords between huge pointers (selector += 8 on
    16-bit offset wrap).  Frame: [bp-8/-6]=src off/sel, [bp-4/-2]=dst
    off/sel.  Exits at 9299 with BX=0 and AX:DX = the last dword copied.
    """
    s = cpu.s
    mem = cpu.mem
    n_dwords = s.bx & 0xFFFF
    if n_dwords == 0:                           # jnz semantics: 0 == 65536
        n_dwords = 0x10000
    n = n_dwords * 4
    src_off, src_sel = _frame_word(cpu, -8), _frame_word(cpu, -6)
    dst_off, dst_sel = _frame_word(cpu, -4), _frame_word(cpu, -2)

    src_lin = _huge_lin(mem, src_sel) + src_off
    dst_lin = _huge_lin(mem, dst_sel) + dst_off
    if src_lin < dst_lin < src_lin + n:
        # Forward-overlap propagation (ASM copies low-to-high in 4-byte
        # steps); a bytearray slice assignment would memmove instead.
        for k in range(0, n, 4):
            mem.data[dst_lin + k:dst_lin + k + 4] = \
                mem.data[src_lin + k:src_lin + k + 4]
    else:
        mem.data[dst_lin:dst_lin + n] = mem.data[src_lin:src_lin + n]

    # Advance both huge pointers exactly as the per-iteration adds would.
    _set_frame_word(cpu, -8, (src_off + n) & 0xFFFF)
    _set_frame_word(cpu, -6, (src_sel + 8 * ((src_off + n) >> 16)) & 0xFFFF)
    _set_frame_word(cpu, -4, (dst_off + n) & 0xFFFF)
    _set_frame_word(cpu, -2, (dst_sel + 8 * ((dst_off + n) >> 16)) & 0xFFFF)

    # Registers as the last iteration leaves them.
    last_src = src_lin + n - 4
    last_dst_off16 = (dst_off + n - 4) & 0xFFFF
    last_dst_sel = (dst_sel + 8 * ((dst_off + n - 4) >> 16)) & 0xFFFF
    s.ax = mem.data[last_src] | (mem.data[last_src + 1] << 8)
    s.dx = mem.data[last_src + 2] | (mem.data[last_src + 3] << 8)
    s.si = last_dst_off16
    s.es = last_dst_sel
    s.bx = 0
    # CF from the last `add [bp-4],4`; then `dec bx` (1 -> 0): ZF/PF set.
    cf = CF if last_dst_off16 >= 0xFFFC else 0
    s.flags = (s.flags & ~ARITH) | ZF | PF | cf
    s.ip = COPY_EXIT


def install(machine) -> int:
    """Register both islands; verify code signatures first."""
    cpu = machine.cpu
    cs = machine.seg_bases[CODE_SEG_INDEX]
    for ip, sig, name in ((FILL_IP, FILL_SIG, "wap_rle_fill"),
                          (COPY_IP, COPY_SIG, "wap_huge_copy")):
        live = machine.mem.block(cs, ip, len(sig))
        if live != sig:
            raise AssertionError(
                f"microman hook {name} at {cs:04X}:{ip:04X}: code bytes "
                f"{live.hex()} != expected {sig.hex()} — refusing to install")
    cpu.replacement_hooks[(cs, FILL_IP)] = _fill_island
    cpu.hook_names[(cs, FILL_IP)] = "wap_rle_fill"
    cpu.replacement_hooks[(cs, COPY_IP)] = _copy_island
    cpu.hook_names[(cs, COPY_IP)] = "wap_huge_copy"
    return 2
