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


def test_boot_runs_to_idle(machine):
    """The full boot arc: crt0 -> WinMain -> WM_CREATE (level data, bitmaps,
    timers) -> intro window (4s timer) -> teardown -> the idle message loop.
    Must run a big step budget with no gap, leaving both game windows alive
    and the Paulie-O-Meter actually rendered (text pixels present)."""
    machine.cpu.trace_enabled = False
    machine.cpu.run(1_500_000)          # any Win16ApiGap/opcode gap raises

    sys_obj = machine.api.services["system"]
    names = [w.wndclass.name for w in sys_obj.windows]
    assert names == ["PYTHON", "PaulieOMeter"]
    assert all(w.visible for w in sys_obj.windows)
    assert sys_obj.timers, "game heartbeat timers must stay armed"

    meter = sys_obj.windows[1].surface
    assert any(meter.pixels), "scoreboard must have rendered non-black pixels"

    called = [c.split("(")[0] for c in machine.api.call_log]
    for expected in ("KERNEL.91:InitTask", "USER.41:CreateWindow",
                     "USER.108:GetMessage", "USER.114:DispatchMessage",
                     "USER.39:BeginPaint", "GDI.34:BitBlt", "GDI.33:TextOut",
                     "USER.420:wsprintf", "USER.53:DestroyWindow"):
        assert expected in called, expected
    # All 26 LoadBitmap calls must resolve through the NAMETABLE (no zeros).
    assert called.count("USER.175:LoadBitmap") == 26
