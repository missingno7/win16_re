"""Selector-based global heap: huge-pointer layout + reclamation."""
from win16.hugeheap import SEG, HugeHeap


def test_small_block_one_selector():
    sb: dict[int, int] = {}
    h = HugeHeap(sb, 0x100000, 0x400000)
    s = h.alloc(1000)
    assert s and s in sb
    assert sb[s] == 0x100000                # first block at the linear start
    assert h.size_of(s) == 1000
    # only one selector mapped
    assert sum(1 for k in sb if k // 8 == s // 8) == 1


def test_huge_block_consecutive_selectors_contiguous():
    sb: dict[int, int] = {}
    h = HugeHeap(sb, 0x100000, 0x400000)
    s = h.alloc(164352)                      # 512x320 page: 3 x 64K
    assert s
    # three consecutive selectors 8 apart, mapping to contiguous 64K regions —
    # this is what makes `selector += __AHINCR(8)` walk the block correctly.
    assert sb[s] == 0x100000
    assert sb[s + 8] == 0x100000 + SEG
    assert sb[s + 16] == 0x100000 + 2 * SEG
    assert (s + 24) not in sb               # only 3 selectors


def test_free_reclaims_linear_and_selectors():
    sb: dict[int, int] = {}
    h = HugeHeap(sb, 0x100000, 0x110000)     # tight 64K space
    a = h.alloc(40000)
    b = h.alloc(20000)
    assert a and b
    assert h.alloc(40000) == 0               # exhausted
    assert h.free(a)
    assert a not in sb                        # selector unmapped
    c = h.alloc(40000)                        # reuses a's reclaimed space
    assert c and sb[c] == sb.get(c)
    assert h.free(b) and h.free(c)


def test_exhaustion_returns_zero_not_crash():
    sb: dict[int, int] = {}
    h = HugeHeap(sb, 0x100000, 0x101000)     # only 4K
    assert h.alloc(1000)
    assert h.alloc(1000)
    assert h.alloc(1000)
    assert h.alloc(1000)
    assert h.alloc(1000) == 0                 # out of linear space
