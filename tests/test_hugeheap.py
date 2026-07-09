"""Selector-based global heap: huge-pointer layout + reclamation."""
from win16.hugeheap import SEG, HugeHeap


def test_small_block_one_selector():
    sb: dict[int, int] = {}
    h = HugeHeap(sb, 0x100000, 0x400000)
    s = h.alloc(1000)
    assert s and s in sb
    assert sb[s] == 0x100000                # first block at the linear start
    assert h.size_of(s) == 1000
    # one descriptor, mapped under all four RPL aliases — protected-mode
    # hardware ignores a selector's RPL bits, and Win16 huge-pointer arithmetic
    # relies on that (see _map_rpl_aliases); every alias maps to the same base.
    aliases = [k for k in sb if k & 0xFFFC == s & 0xFFFC]
    assert sorted(aliases) == [(s & 0xFFFC) | r for r in range(4)]
    assert all(sb[k] == 0x100000 for k in aliases)


def test_rpl_alias_selectors_resolve_to_same_block():
    # SimAnt's terrain rasterizer sign-extends a 16-bit offset before adding it
    # to the base selector, so crossing offset 0x8000 yields selector s-1
    # (RPL 3 -> 2).  That must address the SAME block, not miss and fall through
    # to real-mode (which left the DIB's top half black).
    sb: dict[int, int] = {}
    h = HugeHeap(sb, 0x100000, 0x400000)
    s = h.alloc(0x10000)                     # a full 64K block
    for variant in (s, s - 1, s - 2, s - 3):
        assert sb.get(variant) == 0x100000
    assert h.free(s)
    assert all(((s & 0xFFFC) | r) not in sb for r in range(4))


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
