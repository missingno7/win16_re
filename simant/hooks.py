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
# The island is a faithful 1:1 transliteration of the ASM (setup A668-A698,
# main loop A6C8, literal A6DD, match A706-A764, exit A779) so it produces the
# identical output, window, and exit state — gated byte-exact against the ASM
# by simant/tests/test_hooks.py.  On a mid-operation resume (entry [B7D4] != 0)
# it passes through to the real routine (rare; keeps the delicate resume path
# authoritative).  Exit codes written to [B7D4]: 0 clean (next call fresh),
# 1 flag-read / 2 literal-read / 3 match-byte1 / 4 match-byte2 input-exhaust,
# 5 mid-match (budget hit) — each the ASM's own resume re-entry code.
UNPACK_SEG_INDEX = 7
UNPACK_OFF = 0xA668
UNPACK_SIG = bytes.fromhex("558bec83ec045756")   # push bp;mov bp,sp;sub sp,4;push di;push si
N_WINDOW = 4096
DG_SEG_INDEX = 10                                # DGROUP (auto-data) segment


def _make_unpack_island(machine):
    dg = machine.seg_bases[DG_SEG_INDEX]

    def island(cpu) -> None:
        m = cpu.mem
        s = cpu.s
        rb, wb, rw, ww = m.rb, m.wb, m.rw, m.ww

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

        di = out_off
        count = 0
        code = 0

        def rd():                                 # read one source byte, advance si
            nonlocal src_off
            b = rb(src_seg, src_off)
            src_off = (src_off + 1) & 0xFFFF
            return b

        while True:                               # main loop @ A6C8
            flags >>= 1
            if (flags & 0x100) == 0:              # need a fresh flag byte
                in_rem -= 1
                if in_rem < 0:
                    code = 1
                    break
                flags = rd() | 0xFF00
            if flags & 1:                         # literal (@ A6DD)
                in_rem -= 1
                if in_rem < 0:
                    code = 2
                    break
                c = rd()
                dx = (dx & 0xFF00) | c             # A6E4 mov dl,[si] leaves dl=c
                wb(out_seg, di, c)
                di = (di + 1) & 0xFFFF
                wb(win_seg, (r + 4) & 0xFFFF, c)
                r = (r + 1) & (N_WINDOW - 1)
                count += 1
                budget -= 1
                if budget == 0:
                    code = 0
                    break
            else:                                 # match (@ A706)
                in_rem -= 1
                if in_rem < 0:
                    code = 3
                    break
                b1 = rd()
                in_rem -= 1
                if in_rem < 0:
                    code = 4
                    break
                b2 = rd()
                off = b1 | ((b2 >> 4) << 8)        # 12-bit window offset (cx)
                length = (b2 & 0x0F) + thresh      # dx
                dx = length
                lrem = length                      # [B7D2] match countdown
                while True:                        # copy loop @ A736
                    c = rb(win_seg, (off + 4) & 0xFFFF)
                    dx = c                          # A738 mov dl,[bx+4] leaves dl=c
                    off = (off + 1) & (N_WINDOW - 1)
                    wb(out_seg, di, c)
                    di = (di + 1) & 0xFFFF
                    wb(win_seg, (r + 4) & 0xFFFF, c)
                    r = (r + 1) & (N_WINDOW - 1)
                    count += 1
                    budget -= 1
                    if budget == 0:
                        cx = off
                        code = 5
                        break
                    lrem -= 1
                    if lrem < 0:
                        break
                cx = off
                if code == 5:
                    ww(dg, 0xB7D2, lrem & 0xFFFF)  # save match countdown for resume
                    break

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


# Registry of (segment index, entry offset, signature, island factory, name).
# Each factory takes (machine, off) and returns the hook fn.
_ISLANDS = [
    (RT_SEG_INDEX, AFULDIV_OFF, AFULDIV_SIG,
     lambda machine, off: _make_uldiv_island(off), "__aFuldiv"),
    (UNPACK_SEG_INDEX, UNPACK_OFF, UNPACK_SIG,
     lambda machine, off: _make_unpack_island(machine), "_Unpack"),
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
