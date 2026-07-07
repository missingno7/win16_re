"""LocalHeap allocator unit tests (no game assets needed)."""
from win16.api.localheap import LocalHeap


def test_alloc_free_reuse():
    h = LocalHeap(0x100, 0x200)
    a = h.alloc(16)
    b = h.alloc(16)
    assert a == 0x100 and b == 0x110
    assert h.size_of(a) == 16
    assert h.free_block(a)
    assert h.alloc(8) == a          # first fit reuses the hole
    assert not h.free_block(0x1234)  # unknown pointer


def test_rounding_and_exhaustion():
    h = LocalHeap(0, 32)
    a = h.alloc(1)
    assert h.size_of(a) == 4        # granularity rounding
    assert h.alloc(100) == 0        # exhausted -> 0, like the real API


def test_coalescing():
    h = LocalHeap(0, 48)
    a, b, c = h.alloc(16), h.alloc(16), h.alloc(16)
    assert h.alloc(4) == 0
    h.free_block(a)
    h.free_block(c)
    h.free_block(b)                 # middle free must merge all three
    assert h.alloc(48) == 0x0
