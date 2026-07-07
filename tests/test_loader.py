"""Loader facts: segment mapping, relocation patching, boot frontier."""
import pytest

from ppython import runtime

pytestmark = pytest.mark.skipif(not runtime.assets_present(),
                                reason="game assets not present")


@pytest.fixture()
def machine():
    return runtime.create_machine()


def test_entry_state(machine):
    cpu, exe = machine.cpu, machine.exe
    assert (cpu.s.cs & 0xFFFF, cpu.s.ip & 0xFFFF) == (machine.seg_bases[1], 0x61EA)
    assert cpu.s.ds == cpu.s.es == cpu.s.ss == machine.seg_bases[2]
    # SP = static data + stack, even
    assert cpu.s.sp == (0x5940 + 0x1400) & ~1


def test_code_bytes_mapped(machine):
    # entry bytes: xor bp,bp; push bp; call far (9A) — MSC Win16 startup
    base = machine.seg_bases[1]
    assert machine.mem.block(base, 0x61EA, 3) == b"\x33\xed\x55"
    assert machine.mem.rb(base, 0x61ED) == 0x9A


def test_relocations_resolved(machine):
    """Every import call site must point into the thunk segment."""
    from win16.loader import THUNK_SEG
    from win16.ne import ADDR_FARADDR32, TARGET_IMPORTORDINAL
    exe, mem = machine.exe, machine.mem
    checked = 0
    for seg in exe.segments:
        base = machine.seg_bases[seg.index]
        for rel in seg.relocations:
            if rel.target_type == TARGET_IMPORTORDINAL and \
                    rel.addr_type == ADDR_FARADDR32 and not rel.additive:
                assert mem.rw(base, (rel.offset + 2) & 0xFFFF) == THUNK_SEG
                checked += 1
    assert checked >= 100


def test_osfixups_left_unapplied(machine):
    assert len(machine.osfixups) == 82


def test_boot_reaches_inittask(machine):
    """The interpreter must run the startup prologue and fail loud at the
    first unimplemented API — never silently misbehave."""
    from win16.api.core import Win16ApiGap
    with pytest.raises(Win16ApiGap, match="KERNEL.91:InitTask"):
        machine.cpu.run(100)
    assert machine.cpu.instruction_count == 3
