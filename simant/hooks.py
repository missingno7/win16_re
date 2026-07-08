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


# Registry of (segment index, entry offset, signature, island factory, name).
_ISLANDS = [
    (RT_SEG_INDEX, AFULDIV_OFF, AFULDIV_SIG, _make_uldiv_island, "__aFuldiv"),
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
        cpu.replacement_hooks[(cs, off)] = factory(off)
        cpu.hook_names[(cs, off)] = f"{name}@{seg_index}:{off:04X}"
        count += 1
    return count
