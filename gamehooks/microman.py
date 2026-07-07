"""MICROMAN lifted islands — WAP's inlined hot loops, signature-scanned.

PC-sampling (2026-07-07, run_status.md) found the game's time in WAP's
per-byte huge-memory loops (NE segment 2).  WAP inlines the same compiled
macro many times — page transitions use a DESCENDING byte-fill (the RLE run
decoder), gameplay sprite drawing uses the ASCENDING twin, and page compose
uses a dword copy.  All address memory through the selector heap, where
consecutive selectors (8 apart) map to consecutive 64K, so each loop's whole
effect is ONE contiguous linear slice operation.

Instead of hand-hooking addresses, install() scans the code segment for the
exact machine-code BODY of each loop family and hooks every clone at its
loop head.  An island performs all remaining iterations in one shot, writes
back the exact final register/flag/locals state the ASM would produce, and
jumps to the loop's exit.  Semantics derived from live traces
(artifacts/loop_tr.txt, artifacts/gl.txt); verified by
tests/test_microman_hooks.py (A/B pixel oracle) and
tests/test_microman_snapshot.py (bit-exact hooked resume).

The byte-fill body (locals: [bp-14/12] = 32-bit destination offset,
[bp-34] = offset bias, [bp-32] = base selector, [bp-23] = value; DI = count):

    L:  mov ax,[bp-14]      ; full32 = dest32 + bias
        mov dx,[bp-12]
        add ax,[bp-34]
        adc dx,0
        mov cx,3            ; selector = base_sel + (full>>16)*8
        shl dx,cl
        add dx,[bp-32]
        mov es,dx
        mov bx,ax
        mov al,[bp-23]
        mov es:[bx],al      ; ONE byte per ~25 interpreted instructions
        add/sub [bp-14],1   ; ascending / descending variants
        adc/sbb [bp-12],0
        dec di
        jnz L

The dword-copy body (locals: [bp-8/-6] = src off/sel, [bp-4/-2] = dst
off/sel; BX = dword count; selector += 8 on 16-bit offset wrap):

    L:  les si,[bp-8]
        add word [bp-8],4
        jnb +5 / add word [bp-6],8
        mov ax,es:[si]
        mov dx,es:[si+2]
        les si,[bp-4]
        add word [bp-4],4
        jnb +5 / add word [bp-2],8
        mov es:[si],ax
        mov es:[si+2],dx
        dec bx
        jnz L
"""
from __future__ import annotations

# x86 FLAGS bits (dos_re cpu encoding).
CF, PF, AF, ZF, SF, OF = 0x001, 0x004, 0x010, 0x040, 0x080, 0x800
ARITH = CF | PF | AF | ZF | SF | OF

CODE_SEG_INDEX = 2              # the WAP engine lives in NE segment 2

_FILL_COMMON = "8b46f28b56f40346de83d200b90300d3e20356e08ec28bd88a46e9268807"
BODY_FILL_ASC = bytes.fromhex(_FILL_COMMON + "8346f2018356f4004f75d7")
BODY_FILL_DESC = bytes.fromhex(_FILL_COMMON + "836ef201835ef4004f75d7")
BODY_COPY = bytes.fromhex(
    "c476f88346f80473058146fa0800268b04268b5402"
    "c476fc8346fc0473058146fe0800268904268954024b75d3")


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


def _make_fill_island(exit_ip: int, step: int):
    """Generic byte-fill island (step=+1 ascending, -1 descending), hooked at
    the loop head: DI iterations remain (0 == 65536 per dec/jnz semantics)."""

    def island(cpu) -> None:
        s = cpu.s
        mem = cpu.mem
        n = s.di & 0xFFFF
        if n == 0:
            n = 0x10000
        value = mem.rb(s.ss, (s.bp - 23) & 0xFFFF)
        dest = (_frame_word(cpu, -12) << 16) | _frame_word(cpu, -14)
        bias = _frame_word(cpu, -34)
        base_sel = _frame_word(cpu, -32)

        first_full = (dest + bias) & 0xFFFFFFFF
        last_full = (first_full + step * (n - 1)) & 0xFFFFFFFF
        lin = _huge_lin(mem, base_sel)
        lo, hi = min(first_full, last_full), max(first_full, last_full)
        mem.data[lin + lo:lin + hi + 1] = bytes([value]) * n

        dest_after = (dest + step * n) & 0xFFFFFFFF
        _set_frame_word(cpu, -14, dest_after)
        _set_frame_word(cpu, -12, dest_after >> 16)

        last_sel = (base_sel + ((last_full >> 16) << 3)) & 0xFFFF
        s.ax = (last_full & 0xFF00) | value
        s.bx = last_full & 0xFFFF
        s.cx = 3
        s.dx = last_sel
        s.es = last_sel
        s.di = 0
        # Last adc/sbb [bp-12],0 carries/borrows only when dest32 wrapped on
        # the final step; then `dec di` (1 -> 0) sets ZF/PF, clears SF/AF/OF.
        before_last = (dest + step * (n - 1)) & 0xFFFFFFFF
        wrapped = (before_last == 0xFFFFFFFF) if step > 0 else (before_last == 0)
        s.flags = (s.flags & ~ARITH) | ZF | PF | (CF if wrapped else 0)
        s.ip = exit_ip

    return island


def _make_copy_island(exit_ip: int):
    """Huge-pointer dword copy island, hooked at the loop head: BX dwords
    remain (0 == 65536)."""

    def island(cpu) -> None:
        s = cpu.s
        mem = cpu.mem
        n_dwords = s.bx & 0xFFFF
        if n_dwords == 0:
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
        s.ax = mem.data[last_src] | (mem.data[last_src + 1] << 8)
        s.dx = mem.data[last_src + 2] | (mem.data[last_src + 3] << 8)
        s.si = last_dst_off16
        s.es = (dst_sel + 8 * ((dst_off + n - 4) >> 16)) & 0xFFFF
        s.bx = 0
        # CF from the last `add [bp-4],4`; then `dec bx` (1 -> 0): ZF/PF set.
        cf = CF if last_dst_off16 >= 0xFFFC else 0
        s.flags = (s.flags & ~ARITH) | ZF | PF | cf
        s.ip = exit_ip

    return island


def install(machine) -> int:
    """Scan the code segment for every clone of the loop bodies and hook each
    at its loop head.  Returns the number of islands installed."""
    cpu = machine.cpu
    cs = machine.seg_bases[CODE_SEG_INDEX]
    code = machine.mem.block(cs, 0, 0x10000)
    count = 0
    for body, factory, name in (
            (BODY_FILL_ASC, lambda ip: _make_fill_island(ip, +1), "wap_fill_asc"),
            (BODY_FILL_DESC, lambda ip: _make_fill_island(ip, -1), "wap_fill_desc"),
            (BODY_COPY, _make_copy_island, "wap_huge_copy")):
        pos = code.find(body)
        while pos != -1:
            exit_ip = pos + len(body)
            cpu.replacement_hooks[(cs, pos)] = factory(exit_ip)
            cpu.hook_names[(cs, pos)] = f"{name}@{pos:04X}"
            count += 1
            pos = code.find(body, pos + 1)
    if count < 3:
        raise AssertionError(
            f"microman islands: expected the WAP loop families in segment "
            f"{CODE_SEG_INDEX}, found only {count} clone(s) — wrong binary?")
    return count
