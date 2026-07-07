"""NE parser facts, verified against PYTHON.EXE (skips when assets missing)."""
import pytest

from ppython import runtime

pytestmark = pytest.mark.skipif(not runtime.assets_present(),
                                reason="game assets not present")


@pytest.fixture(scope="module")
def exe():
    return runtime.load_exe()


def test_header(exe):
    h = exe.header
    assert h.segment_count == 2
    assert h.auto_data_seg == 2
    assert (h.entry_seg, h.entry_ip) == (1, 0x61EA)
    assert (h.initial_ss_seg, h.initial_sp) == (2, 0)
    assert h.stack_size == 0x1400
    assert h.heap_size == 0x1000
    assert h.align_shift == 4
    assert h.target_os == 2  # Windows


def test_segments(exe):
    code, data = exe.segments
    assert not code.is_data and code.file_length == 0x8C91
    assert data.is_data and data.file_length == 0x5940
    assert len(exe.segment_bytes(code)) == code.alloc_size
    # both carry relocations
    assert code.relocations and data.relocations


def test_modules_and_imports(exe):
    assert exe.modules == ("win87em", "KERNEL", "GDI", "USER", "SOUND")
    from win16.ne import TARGET_IMPORTORDINAL, TARGET_OSFIXUP
    imports = set()
    osfixups = 0
    for seg in exe.segments:
        for rel in seg.relocations:
            if rel.target_type == TARGET_IMPORTORDINAL:
                imports.add((exe.modules[rel.target1 - 1].upper(), rel.target2))
            elif rel.target_type == TARGET_OSFIXUP:
                osfixups += 1
    assert ("KERNEL", 91) in imports       # InitTask
    assert ("USER", 108) in imports        # GetMessage
    assert ("GDI", 34) in imports          # BitBlt
    assert ("SOUND", 4) in imports         # SetVoiceNote
    assert ("WIN87EM", 1) in imports       # __fpMath
    assert len(imports) == 105
    assert osfixups == 82


def test_resources(exe):
    bitmaps = exe.find_resources("BITMAP")
    assert len(bitmaps) == 25
    # every bitmap resource is a DIB (BITMAPINFOHEADER, biSize == 40)
    for r in bitmaps:
        assert int.from_bytes(r.data[:4], "little") == 40
    assert len(exe.find_resources("MENU")) == 1
    assert len(exe.find_resources("DIALOG")) == 6
    assert len(exe.find_resources("ACCELERATOR")) == 1


def test_resident_names(exe):
    assert exe.resident_names[0] == ("PYTHON", 0)
