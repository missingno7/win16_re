"""SimAnt lifted islands — hot ASM routines reimplemented in Python.

The dos_re method applied to SimAnt: PC-sampling (`python -m simant.probes.
profile`) ranks the game's time by routine (names from SIMANTW.SYM).  The
runaway #1 is `__aFuldiv` — the Microsoft C far 32-bit UNSIGNED long-divide
runtime helper — called constantly for the map/coordinate scaling math (~14%
of all samples, its inner shift-subtract loop runs dozens of interpreted
instructions per divide).  It is a pure function with a fixed ABI, so it lifts
to one exact Python `//`.

Each island is installed at a routine's entry CS:IP, verified against the
routine's real prologue bytes at install time (an island landing on different
code corrupts silently — so we refuse to install on mismatch).  The island
computes the result, writes back the exact ABI-guaranteed exit state (result
registers, preserved registers, the `retf` stack unwind) and jumps to the
caller.  Correctness is gated by `simant/tests/test_hooks.py`, which runs the
ORIGINAL routine and the island over the same inputs and compares the full
register result — the byte-exact proof that makes this a recovery, not an
approximation.

ABI of __aFuldiv (far, callee-cleans — verified by live trace):
    entry SP -> [ret_ip][ret_cs][dividend:dword][divisor:dword]
    quotient in DX:AX; CX clobbered to divisor-low; BX/SI/DI/BP preserved;
    returns `retf 8` (SP += 4 ret + 8 args = 12).
"""
from __future__ import annotations

from dos_re.cpu import AF, CF, OF, PF, SF, ZF

from .recovered import lzss

_ARITH = CF | PF | AF | ZF | SF | OF

# NE segment (1-based) holding the C runtime helpers; resolved to a base at
# install time.  SimAnt's __aF* math helpers live in segment 4.
RT_SEG_INDEX = 4

# __aFuldiv entry offset within segment 4 (SIMANTW.SYM) and its prologue:
#   55        push bp
#   8b ec     mov bp,sp
#   53        push bx
#   56        push si
#   8b 46 0c  mov ax,[bp+0C]     ; divisor high word
#   0b c0     or ax,ax
#   75        jnz ...            ; high != 0 -> full 32-bit path
AFULDIV_OFF = 0x0A60
AFULDIV_SIG = bytes.fromhex("558bec53568b460c0bc075")


def _stack_word(cpu, delta: int) -> int:
    return cpu.mem.rw(cpu.s.ss, (cpu.s.sp + delta) & 0xFFFF)


def _make_uldiv_island(entry_off: int):
    """Island for __aFuldiv at segment-relative `entry_off` (only used for the
    hook-name label; the island reads everything live off the stack)."""

    def island(cpu) -> None:
        s = cpu.s
        sp = s.sp
        ret_ip = _stack_word(cpu, 0)
        ret_cs = _stack_word(cpu, 2)
        dividend = _stack_word(cpu, 4) | (_stack_word(cpu, 6) << 16)
        divisor = _stack_word(cpu, 8) | (_stack_word(cpu, 10) << 16)
        if divisor == 0:
            # The real routine faults (#DE) inside `div`.  Never hit in normal
            # play; fail loud rather than silently returning a wrong quotient.
            raise ZeroDivisionError(
                "__aFuldiv island: divide by zero (dividend "
                f"{dividend:#x}) — the ASM would #DE here")
        quotient = (dividend // divisor) & 0xFFFFFFFF
        s.ax = quotient & 0xFFFF
        s.dx = (quotient >> 16) & 0xFFFF
        s.cx = divisor & 0xFFFF          # routine leaves divisor-low in CX
        # BX, SI, DI, BP, ES, DS, flags: untouched (routine preserves them).
        s.sp = (sp + 12) & 0xFFFF        # retf 8: pop ret (4) + args (8)
        s.cs = ret_cs
        s.ip = ret_ip

    return island


# -- _Unpack: the LZSS asset decompressor (the load bottleneck) --------------
#
# ~90% of load time is this one loop at seg7:A668 (`_Unpack`, per SIMANTW.SYM) —
# the classic Okumura LZSS: 4KB sliding window (window seg = [B7C0], byte r at
# offset r+4), window pre-filled with spaces, decode pointer r starts at
# 0x0FEE = N-F, THRESHOLD = [B7C2] (=2), F = 18.  It is a *resumable streaming*
# decoder: the caller asks for [bp+10] output bytes per call, and cross-call
# state lives in DGROUP globals.
#
# The decode ALGORITHM is recovered VM-free in `simant/recovered/lzss.py` (a
# native port calls it directly).  This island is a thin ADAPTER: it reads the
# routine's state from the DGROUP globals + stack, drives `lzss.decode_chunk`
# over memoryviews straight into VM memory, and writes back the exact ABI exit
# state — gated byte-exact against the ASM by simant/tests/test_hooks.py.  On a
# mid-operation resume (entry [B7D4] != 0) it passes through to the real routine
# (keeps the delicate two-sided-streaming resume path authoritative).  Exit
# codes written to [B7D4] mirror the ASM's own resume re-entry codes (see
# lzss.CODE_*): 0 clean, 1-4 input-exhaust points, 5 mid-match.
UNPACK_SEG_INDEX = 7
UNPACK_OFF = 0xA668
UNPACK_SIG = bytes.fromhex("558bec83ec045756")   # push bp;mov bp,sp;sub sp,4;push di;push si
DG_SEG_INDEX = 10                                # DGROUP (auto-data) segment


def _make_unpack_island(machine):
    dg = machine.seg_bases[DG_SEG_INDEX]

    def island(cpu) -> None:
        m = cpu.mem
        s = cpu.s
        rw, ww = m.rw, m.ww

        resume = rw(dg, 0xB7D4)
        if resume != 0:
            # Mid-operation resume — let the real routine handle it.  Emulate the
            # hooked `push bp` and continue at A669 (mov bp,sp).
            s.sp = (s.sp - 2) & 0xFFFF
            ww(s.ss, s.sp, s.bp)
            s.ip = (UNPACK_OFF + 1) & 0xFFFF
            return

        sp = s.sp
        ret_ip, ret_cs = rw(s.ss, sp), rw(s.ss, (sp + 2) & 0xFFFF)
        out_off = rw(s.ss, (sp + 4) & 0xFFFF)
        out_seg = rw(s.ss, (sp + 6) & 0xFFFF)
        budget = rw(s.ss, (sp + 8) & 0xFFFF)      # [bp+10] output byte count

        r = rw(dg, 0xB7CA)                        # window write pos (bx)
        dx = rw(dg, 0xB7CE)
        cx = rw(dg, 0xB7D0)
        flags = rw(dg, 0xB7CC)                    # flag bit buffer (ax)
        win_seg = rw(dg, 0xB7C0)
        thresh = rw(dg, 0xB7C2)
        src_seg = rw(dg, 0xB7C6)
        src_off = rw(dg, 0xB7C4)
        in_rem = rw(dg, 0xB7C8)                   # signed input-remaining counter
        if in_rem >= 0x8000:
            in_rem -= 0x10000

        # Drive the recovered VM-free decoder (simant/recovered/lzss.py) over
        # memoryviews straight into VM memory — no copies.  window[i] IS the
        # ASM's win_seg:[i+4]; source and output are contiguous from their far
        # pointers.  The pure decoder writes the window + output in place and
        # returns the full resumable state.
        data = memoryview(m.data)
        win_lin = m._xlat(win_seg, 4)
        out_lin = m._xlat(out_seg, out_off)
        st_ = lzss.decode_chunk(
            data[m._xlat(src_seg, src_off):],             # source (reads <= in_rem)
            0,
            data[win_lin:win_lin + lzss.WINDOW_SIZE],     # 4KB sliding window
            data[out_lin:out_lin + budget],               # output (writes <= budget)
            0, r, flags, in_rem, budget, thresh, dx, cx)
        code = st_.code
        count = st_.out_pos
        r, flags, in_rem, dx, cx = st_.r, st_.flags, st_.in_rem, st_.dx, st_.cx
        src_off = (src_off + st_.src_pos) & 0xFFFF
        if code == lzss.CODE_MATCH_COPY:
            ww(dg, 0xB7D2, st_.match_rem & 0xFFFF)        # save match countdown

        # -- write back the exit state exactly as A779 does ------------------
        ww(dg, 0xB7D4, code)
        ww(dg, 0xB7CC, flags & 0xFFFF)
        ww(dg, 0xB7CA, r & 0xFFFF)
        ww(dg, 0xB7C4, src_off & 0xFFFF)
        ww(dg, 0xB7CE, dx & 0xFFFF)
        ww(dg, 0xB7D0, cx & 0xFFFF)
        ww(dg, 0xB7C8, in_rem & 0xFFFF)           # ASM decremented this in place
        # Reproduce the values the ASM leaves in its stack frame — after the
        # retf that memory is freed, but SimAnt (C, uninitialised locals) can
        # read the scratch, so the freed-frame contents must match byte-for-byte
        # or a later read diverges.  Frame (bp = sp-2 after push bp):
        #   [sp-2]=old bp  [sp-4]=[bp-2] count  [sp-6]=[bp-4] win seg
        #   [sp-8]=pushed di  [sp-10]=pushed si  [sp-12]=pushed ds
        ww(s.ss, (sp - 2) & 0xFFFF, s.bp)
        ww(s.ss, (sp - 4) & 0xFFFF, count & 0xFFFF)
        ww(s.ss, (sp - 6) & 0xFFFF, win_seg)
        ww(s.ss, (sp - 8) & 0xFFFF, s.di)
        ww(s.ss, (sp - 10) & 0xFFFF, s.si)
        ww(s.ss, (sp - 12) & 0xFFFF, s.ds)
        # Registers at retf: AX=output count, BX=r, CX, DX as above; ES=output
        # seg; SI/DI/DS/BP restored to the caller's (the island never touched
        # the real SI/DI/DS/BP).  retf has no arg cleanup — caller pops args.
        s.ax = count & 0xFFFF
        s.bx = r & 0xFFFF
        s.cx = cx & 0xFFFF
        s.dx = dx & 0xFFFF
        s.es = out_seg
        s.sp = (sp + 4) & 0xFFFF
        s.cs = ret_cs
        s.ip = ret_ip

    return island


# -- a far byte-memcpy (seg2:3460) — the tile/map block copy -----------------
#
# A compiler-emitted far byte-copy loop (the profiler mislabels the region;
# the neighbouring hot 24% is actually a GetTickCount frame-pacing busy-wait,
# NOT a tile blit, and is left alone because accelerating it would shift the
# RNG-seeded worldgen).  This loop copies SI bytes from a huge source pointer
# (offset @bp-8, selector @bp-6) to a huge dest pointer (offset @bp-12,
# selector @bp-10), each advancing one byte at a time with a +8 selector bump
# on every 64K wrap.  Observed: 960-byte tile rows, ~9.5% of load.  Consecutive
# hugeheap selectors map to contiguous linear memory, so the whole run is one
# linear block move (the island detects the rare overlapping-forward case and
# falls back to a smearing byte copy to stay byte-exact).
BYTECOPY_SEG_INDEX = 2
BYTECOPY_OFF = 0x3460
BYTECOPY_SIG = bytes.fromhex(                    # les..jnz, 37 bytes
    "c45ef88346f80173058146fa0800268a07c45ef48346f40173058146f608002688074e75db")
BYTECOPY_EXIT = BYTECOPY_OFF + len(BYTECOPY_SIG)  # 0x3485 (after jnz not taken)


def _make_bytecopy_island(machine):
    def island(cpu) -> None:
        m = cpu.mem
        s = cpu.s
        ss, bp = s.ss, s.bp
        rw, ww, xlat = m.rw, m.ww, m._xlat

        n = s.si or 0x10000                      # SI==0 loops the full 64K
        src_off, src_sel = rw(ss, (bp - 8) & 0xFFFF), rw(ss, (bp - 6) & 0xFFFF)
        dst_off, dst_sel = rw(ss, (bp - 12) & 0xFFFF), rw(ss, (bp - 10) & 0xFFFF)
        src_lin, dst_lin = xlat(src_sel, src_off), xlat(dst_sel, dst_off)

        d = m.data
        if src_lin < dst_lin < src_lin + n:      # overlapping forward -> smear
            for i in range(n):
                d[dst_lin + i] = d[src_lin + i]
        else:                                    # non-overlapping linear move
            d[dst_lin:dst_lin + n] = bytes(d[src_lin:src_lin + n])

        # Advance both huge pointers exactly as the per-byte adds + selector
        # bumps would, and set the registers/flags the loop exit leaves.
        ww(ss, (bp - 8) & 0xFFFF, (src_off + n) & 0xFFFF)
        ww(ss, (bp - 6) & 0xFFFF, (src_sel + 8 * ((src_off + n) >> 16)) & 0xFFFF)
        ww(ss, (bp - 12) & 0xFFFF, (dst_off + n) & 0xFFFF)
        ww(ss, (bp - 10) & 0xFFFF, (dst_sel + 8 * ((dst_off + n) >> 16)) & 0xFFFF)
        s.ax = (s.ax & 0xFF00) | d[src_lin + n - 1]      # AL = last byte copied
        s.bx = (dst_off + n - 1) & 0xFFFF                # last dest offset used
        s.es = (dst_sel + 8 * ((dst_off + n - 1) >> 16)) & 0xFFFF
        s.si = 0
        # `dec si` -> 0 then `jnz` not taken: ZF/PF set, others clear; CF from
        # the last pointer add is 0 for any run that does not overflow a 16-bit
        # offset into a >0xFFFF selector (never happens for these buffers).
        s.flags = (s.flags & ~_ARITH) | ZF | PF
        s.ip = BYTECOPY_EXIT & 0xFFFF

    return island


# -- _Windows_MakeTable4x4 (seg4:4674) — the terrain tile-to-pixel expander ---
#
# The game's own routine that paints a 4-scanline terrain band into a huge DIB
# frame buffer.  Per column it does one `lodsb` (a tile colour index) then four
# `stosw`, reading each scanline's fill word from a 4x32-word table at
# SS:0x1A56 (row stride 0x40).  The four rows sit at DI, DI+2*count, DI+4*count,
# DI+6*count (stride = 2*count words = the DIB scanline); DI advances one word
# per column.  ES stays a single selector for the call (the huge-pointer walk
# across selectors is the caller's), so the whole band is a plain write within
# one linear span.  Preserves every register/segment (pusha/popa + push bp +
# push ds/es); `retf` (caller cleans the 10 arg bytes).  The pixel logic is
# recovered VM-free in simant/recovered/render.py.
MAKETABLE4X4_SEG_INDEX = 4
MAKETABLE4X4_OFF = 0x4674
MAKETABLE4X4_TABLE_OFF = 0x1A56                  # SS-relative colour table base
MAKETABLE4X4_SIG = bytes.fromhex(                # prologue + the table-base load
    "558bec601e06c57606c47e0a8b4e0e8bd1d1e24a4abb561a")


def _make_maketable4x4_island(machine):
    from .recovered.render import windows_make_table_4x4

    def island(cpu) -> None:
        m = cpu.mem
        s = cpu.s
        ss, sp = s.ss, s.sp
        rw = m.rw
        ret_ip, ret_cs = rw(ss, sp), rw(ss, (sp + 2) & 0xFFFF)
        src_off, src_seg = rw(ss, (sp + 4) & 0xFFFF), rw(ss, (sp + 6) & 0xFFFF)
        dst_off, dst_seg = rw(ss, (sp + 8) & 0xFFFF), rw(ss, (sp + 0x0A) & 0xFFFF)
        count = rw(ss, (sp + 0x0C) & 0xFFFF)

        tiles = [m.rb(src_seg, (src_off + i) & 0xFFFF) for i in range(count)]
        table = [[rw(ss, (MAKETABLE4X4_TABLE_OFF + row * 0x40 + t * 2) & 0xFFFF)
                  for t in range(32)] for row in range(4)]
        rows = windows_make_table_4x4(tiles, table)

        stride = (2 * count) & 0xFFFF             # DI += dx+2 between scanlines
        for r in range(4):
            base = (dst_off + r * stride) & 0xFFFF
            row = rows[r]
            for c in range(count):
                m.ww(dst_seg, (base + c * 2) & 0xFFFF, row[c])

        # Every register/segment/flag is preserved by the routine; only SP and
        # CS:IP change (retf pops the return address, the caller cleans args).
        s.sp = (sp + 4) & 0xFFFF
        s.cs = ret_cs
        s.ip = ret_ip

    return island


# -- _Windows_MakeTable1x1 (seg4:46BB) — the 1:1 (no-zoom) tile packer --------
#
# The sibling of MakeTable4x4 for the un-zoomed view: it packs pairs of source
# tile bytes into single 4bpp pixel bytes via an XLAT table at SS:0x1B56.  Per
# iteration (count>>1 of them): lodsb t0; al = ss:[0x1B56+t0]; ah = al; lodsb
# t1; al = ss:[0x1B66+t1]; al |= ah; stosb.  Same full-preservation + retf ABI.
MAKETABLE1X1_SEG_INDEX = 4
MAKETABLE1X1_OFF = 0x46BB
MAKETABLE1X1_TABLE_OFF = 0x1B56                  # SS-relative XLAT table base
MAKETABLE1X1_SIG = bytes.fromhex(
    "558bec601e06c57606c47e0abb561b8b4e0ed1e9")


def _make_maketable1x1_island(machine):
    from .recovered.render import windows_make_table_1x1

    def island(cpu) -> None:
        m = cpu.mem
        s = cpu.s
        ss, sp = s.ss, s.sp
        rw, rb = m.rw, m.rb
        ret_ip, ret_cs = rw(ss, sp), rw(ss, (sp + 2) & 0xFFFF)
        src_off, src_seg = rw(ss, (sp + 4) & 0xFFFF), rw(ss, (sp + 6) & 0xFFFF)
        dst_off, dst_seg = rw(ss, (sp + 8) & 0xFFFF), rw(ss, (sp + 0x0A) & 0xFFFF)
        count = rw(ss, (sp + 0x0C) & 0xFFFF)

        pairs = count >> 1
        tiles = [rb(src_seg, (src_off + i) & 0xFFFF) for i in range(2 * pairs)]
        table = bytes(rb(ss, (MAKETABLE1X1_TABLE_OFF + i) & 0xFFFF)
                      for i in range(0x110))       # covers XLAT of 0..255 at +0 and +0x10
        out = windows_make_table_1x1(tiles, table)
        for i, byteval in enumerate(out):
            m.wb(dst_seg, (dst_off + i) & 0xFFFF, byteval)

        s.sp = (sp + 4) & 0xFFFF
        s.cs = ret_cs
        s.ip = ret_ip

    return island


# Registry of (segment index, entry offset, signature, island factory, name).
# Each factory takes (machine, off) and returns the hook fn.
_ISLANDS = [
    (RT_SEG_INDEX, AFULDIV_OFF, AFULDIV_SIG,
     lambda machine, off: _make_uldiv_island(off), "__aFuldiv"),
    (UNPACK_SEG_INDEX, UNPACK_OFF, UNPACK_SIG,
     lambda machine, off: _make_unpack_island(machine), "_Unpack"),
    (BYTECOPY_SEG_INDEX, BYTECOPY_OFF, BYTECOPY_SIG,
     lambda machine, off: _make_bytecopy_island(machine), "bytecopy"),
    (MAKETABLE4X4_SEG_INDEX, MAKETABLE4X4_OFF, MAKETABLE4X4_SIG,
     lambda machine, off: _make_maketable4x4_island(machine),
     "_Windows_MakeTable4x4"),
    (MAKETABLE1X1_SEG_INDEX, MAKETABLE1X1_OFF, MAKETABLE1X1_SIG,
     lambda machine, off: _make_maketable1x1_island(machine),
     "_Windows_MakeTable1x1"),
]


def install(machine) -> int:
    """Install every SimAnt island whose entry bytes still match its recorded
    prologue.  Returns the number installed.  Refuses (AssertionError) if a
    routine's signature does not match — an island on the wrong code corrupts
    silently."""
    cpu = machine.cpu
    count = 0
    for seg_index, off, sig, factory, name in _ISLANDS:
        cs = machine.seg_bases[seg_index]
        actual = machine.mem.block(cs, off, len(sig))
        if actual != sig:
            raise AssertionError(
                f"simant island {name}: prologue at seg{seg_index}:{off:04X} is "
                f"{actual.hex()}, expected {sig.hex()} — wrong binary/offset?")
        cpu.replacement_hooks[(cs, off)] = factory(machine, off)
        cpu.hook_names[(cs, off)] = f"{name}@{seg_index}:{off:04X}"
        count += 1
    return count
