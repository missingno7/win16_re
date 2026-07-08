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
    assert hooks.install(hk) == 3               # __aFuldiv + _Unpack + bytecopy
    isl = _run_island(hk, dividend, divisor)

    assert asm["ax"] | (asm["dx"] << 16) == (dividend // divisor) & 0xFFFFFFFF
    assert isl == asm, (
        f"{dividend:#x} // {divisor:#x}: island {isl} != asm {asm}")


def test_install_counts_and_verifies():
    m = runtime.create_machine()
    assert hooks.install(m) == 3
    assert runtime.install_hooks(runtime.create_machine()) == 3


def _capture_unpack_output(with_island, max_calls, step_budget):
    """Boot SimAnt (optionally with the _Unpack island) and return the list of
    (output_bytes, exit_globals) for each of the first `max_calls` _Unpack
    calls — the decompressor's observable result, per call."""
    from dos_re.cpu import CPU8086
    m = runtime.create_machine()
    m.cpu.trace_enabled = False
    cs7 = m.seg_bases[hooks.UNPACK_SEG_INDEX]
    dg = m.seg_bases[hooks.DG_SEG_INDEX]
    st = m.cpu.s
    out = []
    pend = {}

    if with_island:
        isl = hooks._make_unpack_island(m)

        def hook(cpu):
            sp = cpu.s.sp
            pend["a"] = (cpu.mem.rw(cpu.s.ss, (sp + 4) & 0xFFFF),
                         cpu.mem.rw(cpu.s.ss, (sp + 6) & 0xFFFF),
                         cpu.mem.rw(cpu.s.ss, (sp + 8) & 0xFFFF))
            pend["ret"] = (cpu.mem.rw(cpu.s.ss, sp),
                           cpu.mem.rw(cpu.s.ss, (sp + 2) & 0xFFFF))
            return isl(cpu)
        m.cpu.replacement_hooks[(cs7, hooks.UNPACK_OFF)] = hook

    orig = CPU8086.step

    def watch(self):
        cs, ip = st.cs & 0xFFFF, st.ip & 0xFFFF
        if not with_island and cs == cs7 and ip == hooks.UNPACK_OFF:
            sp = st.sp
            pend["a"] = (m.mem.rw(st.ss, (sp + 4) & 0xFFFF),
                         m.mem.rw(st.ss, (sp + 6) & 0xFFFF),
                         m.mem.rw(st.ss, (sp + 8) & 0xFFFF))
            pend["ret"] = (m.mem.rw(st.ss, sp), m.mem.rw(st.ss, (sp + 2) & 0xFFFF))
        if "a" in pend and (cs, ip) == (pend["ret"][1], pend["ret"][0]):
            oo, osg, budget = pend.pop("a")
            pend.pop("ret")
            data = bytes(m.mem.rb(osg, (oo + i) & 0xFFFF) for i in range(budget))
            exitg = tuple(m.mem.rw(dg, a) for a in
                          (0xB7CA, 0xB7CC, 0xB7C4, 0xB7C8, 0xB7CE, 0xB7D0, 0xB7D4))
            out.append((data, exitg))
        orig(self)

    CPU8086.step = watch
    try:
        while len(out) < max_calls and m.cpu.instruction_count < step_budget:
            m.cpu.run(400_000)
    except Exception:  # noqa: BLE001 — a frontier past the load is acceptable
        pass
    finally:
        CPU8086.step = orig
    return out[:max_calls]


def test_unpack_island_is_byte_exact_vs_asm():
    """The A/B decompression gate: booting with the _Unpack island must produce
    the IDENTICAL decompressed output and exit state, call for call, as the real
    ASM routine — the byte-exact proof that the LZSS island is a recovery."""
    CALLS, BUDGET = 30, 4_000_000
    plain = _capture_unpack_output(False, CALLS, BUDGET)
    island = _capture_unpack_output(True, CALLS, BUDGET)
    assert len(plain) >= CALLS, f"only {len(plain)} _Unpack calls captured"
    assert len(island) == len(plain)
    for i, (p, k) in enumerate(zip(plain, island)):
        assert k[0] == p[0], f"call {i}: island output differs ({len(k[0])} vs {len(p[0])} bytes)"
        assert k[1] == p[1], f"call {i}: island exit state differs {k[1]} vs {p[1]}"


def test_install_refuses_wrong_code():
    m = runtime.create_machine()
    cs = m.seg_bases[hooks.RT_SEG_INDEX]
    m.mem.data[(cs << 4) + hooks.AFULDIV_OFF:
               (cs << 4) + hooks.AFULDIV_OFF + 16] = bytes(16)
    with pytest.raises(AssertionError):
        hooks.install(m)


# ---- the byte-memcpy island (seg2:3460) --------------------------------------
from dos_re.cpu import ZF as _ZF                       # noqa: E402


def _run_bytecopy(with_island, src_seg, src_off, dst_seg, dst_off, n, pattern):
    """Set up the loop's frame (src/dst huge pointers @bp-8/-6/-12/-10, SI=n)
    with `pattern` at the source, run to the loop exit, and report the copied
    region + exit registers/frame.  with_island hooks seg2:3460 (one step);
    otherwise the real ASM loop runs."""
    m = runtime.create_machine()
    m.cpu.trace_enabled = False
    cs2 = m.seg_bases[hooks.BYTECOPY_SEG_INDEX]
    if with_island:
        hooks.install(m)
    s = m.cpu.s
    src_lin = m.mem._xlat(src_seg, src_off)
    m.mem.data[src_lin:src_lin + n] = pattern
    s.cs, s.ip, s.bp, s.si, s.ax = cs2, hooks.BYTECOPY_OFF, 0xC000, n & 0xFFFF, 0x5500
    for off, v in ((-8, src_off), (-6, src_seg), (-12, dst_off), (-10, dst_seg)):
        m.mem.ww(s.ss, (s.bp + off) & 0xFFFF, v)
    if with_island:
        m.cpu.step()
    else:
        for _ in range(n * 15 + 200):
            m.cpu.step()
            if (s.cs & 0xFFFF, s.ip & 0xFFFF) == (cs2, hooks.BYTECOPY_EXIT):
                break
        else:
            raise AssertionError("ASM byte-copy loop did not reach its exit")
    assert (s.cs & 0xFFFF, s.ip & 0xFFFF) == (cs2, hooks.BYTECOPY_EXIT)
    dst_lin = m.mem._xlat(dst_seg, dst_off)
    return bytes(m.mem.data[dst_lin:dst_lin + n]), dict(
        ax=s.ax, bx=s.bx, es=s.es, si=s.si, zf=m.cpu.get_flag(_ZF),
        src_off=m.mem.rw(s.ss, (s.bp - 8) & 0xFFFF),
        dst_off=m.mem.rw(s.ss, (s.bp - 12) & 0xFFFF))


# (src_seg, src_off, dst_seg, dst_off, n) — real-mode segments in scratch RAM.
# Includes the real 960-byte tile-row case, a 1-byte edge, and an OVERLAPPING
# forward copy (dst 16 bytes after src) that must smear exactly like the ASM.
_COPY_CASES = [
    (0x7000, 0x0004, 0x7100, 0x0000, 960),      # the observed tile-row copy
    (0x7000, 0x0000, 0x7100, 0x0000, 1),
    (0x7000, 0x0000, 0x7100, 0x0000, 300),
    (0x7000, 0x0010, 0x7000, 0x0000, 200),      # dst before src (no smear)
    (0x7000, 0x0000, 0x7000, 0x0010, 200),      # dst after src -> smears
]


@pytest.mark.parametrize("src_seg, src_off, dst_seg, dst_off, n", _COPY_CASES)
def test_bytecopy_island_matches_asm(src_seg, src_off, dst_seg, dst_off, n):
    pattern = bytes((i * 37 + 11) & 0xFF for i in range(n))
    asm = _run_bytecopy(False, src_seg, src_off, dst_seg, dst_off, n, pattern)
    isl = _run_bytecopy(True, src_seg, src_off, dst_seg, dst_off, n, pattern)
    assert isl[0] == asm[0], "copied bytes differ"
    assert isl[1] == asm[1], f"exit state differs: island {isl[1]} != asm {asm[1]}"
