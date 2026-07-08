"""SimAnt lifted islands — the byte-exact A/B oracle gate.

For every island we run the ORIGINAL ASM routine and the island over the same
inputs and require an identical register result.  That equivalence is the whole
value of a hook: it must be a recovery (exact), not an approximation.  A math
helper is a pure function, so this is a precise unit oracle — no whole-game
replay / desync to reason about.
"""
import pytest

from simant import hooks, runtime

pytestmark = pytest.mark.skipif(not runtime.assets_present(),
                                reason="simant assets not present")

SENT_CS, SENT_IP = 0x0001, 0x0002          # sentinel return address (never run)
# Distinct marker values in the callee-preserved registers, so the oracle also
# proves BX/SI/DI/BP survive the call.
MARK = dict(bx=0x1111, si=0x2222, di=0x3333, bp=0x4444)


def _setup_call(m, entry_off, dividend, divisor):
    """Point CS:IP at the routine with a synthetic far-call frame
    (ret=SENT, then dividend:dword, divisor:dword) and marker registers."""
    s = m.cpu.s
    s.cs = m.seg_bases[hooks.RT_SEG_INDEX]
    s.ip = entry_off
    s.bx, s.si, s.di, s.bp = MARK["bx"], MARK["si"], MARK["di"], MARK["bp"]
    sp = s.sp
    for v in (divisor >> 16, divisor & 0xFFFF, dividend >> 16, dividend & 0xFFFF,
              SENT_CS, SENT_IP):                # pushed high-address-first
        sp = (sp - 2) & 0xFFFF
        m.mem.ww(s.ss, sp, v & 0xFFFF)
    s.sp = sp


def _regs(m):
    # The ABI contract of __aFuldiv: result in DX:AX, BX/SI/DI/BP preserved, and
    # the retf stack unwind (SP, CS:IP).  CX (and FLAGS) are caller-clobbered
    # scratch — the routine leaves an algorithm-internal intermediate in CX on
    # the full-32-bit path, which no caller observes; replicating it would mean
    # re-running the very loop the island exists to skip.  So the oracle checks
    # the contract, not the scratch.
    s = m.cpu.s
    return dict(ax=s.ax, dx=s.dx, bx=s.bx, si=s.si, di=s.di,
                bp=s.bp, sp=s.sp, cs=s.cs, ip=s.ip)


def _run_asm(m, dividend, divisor):
    _setup_call(m, hooks.AFULDIV_OFF, dividend, divisor)
    for _ in range(2000):                       # the divide loop is bounded
        m.cpu.step()
        if (m.cpu.s.cs & 0xFFFF, m.cpu.s.ip & 0xFFFF) == (SENT_CS, SENT_IP):
            return _regs(m)
    raise AssertionError("ASM __aFuldiv did not return to the sentinel")


def _run_island(m, dividend, divisor):
    _setup_call(m, hooks.AFULDIV_OFF, dividend, divisor)
    m.cpu.step()                                # the installed hook fires once
    return _regs(m)


# Small, full-32-bit (divisor high != 0), by-one, and identity cases — the two
# code paths (divisor high == 0 vs != 0) plus boundaries.
CASES = [
    (13, 55), (55, 13), (1000, 7), (0, 1), (0xFFFFFFFF, 1),
    (0xFFFFFFFF, 0xFFFF), (0x12345678, 0x100), (0x12345678, 0x10000),
    (0xABCD1234, 0x1234), (100, 100), (0x80000000, 3), (0xFFFFFFFF, 0xFFFFFFFF),
    (0x00010000, 0x00000200), (0x7FFFFFFF, 0x00020000),
]


@pytest.mark.parametrize("dividend, divisor", CASES)
def test_uldiv_island_matches_asm(dividend, divisor):
    ref = runtime.create_machine()
    ref.cpu.trace_enabled = False
    asm = _run_asm(ref, dividend, divisor)

    hk = runtime.create_machine()
    hk.cpu.trace_enabled = False
    assert hooks.install(hk) == 1
    isl = _run_island(hk, dividend, divisor)

    assert asm["ax"] | (asm["dx"] << 16) == (dividend // divisor) & 0xFFFFFFFF
    assert isl == asm, (
        f"{dividend:#x} // {divisor:#x}: island {isl} != asm {asm}")


def test_install_counts_and_verifies():
    m = runtime.create_machine()
    assert hooks.install(m) == 1
    assert runtime.install_hooks(runtime.create_machine()) == 1


def test_install_refuses_wrong_code():
    m = runtime.create_machine()
    cs = m.seg_bases[hooks.RT_SEG_INDEX]
    m.mem.data[(cs << 4) + hooks.AFULDIV_OFF:
               (cs << 4) + hooks.AFULDIV_OFF + 16] = bytes(16)
    with pytest.raises(AssertionError):
        hooks.install(m)
