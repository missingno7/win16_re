"""Selector-based global heap: huge-pointer layout + reclamation."""
from win16.hugeheap import SEG, HugeHeap, descriptor


def test_small_block_one_selector():
    sb: dict[int, int] = {}
    h = HugeHeap(sb, 0x100000, 0x400000)
    s = h.alloc(1000)
    assert s
    d = descriptor(s)
    assert sb[d] == 0x100000                 # first block at the linear start
    assert h.size_of(s) == 1000
    # one mapping, keyed by the descriptor (RPL masked off); Memory resolves any
    # RPL alias of it — see test_core.test_selector_translation.
    assert sum(1 for k in sb if descriptor(k) == d) == 1


def test_registered_by_descriptor_not_exact_selector():
    # Memory resolves selectors RPL-agnostically (masks the RPL — see dos_re
    # Memory._xlat), so the heap registers ONE entry per descriptor.  SimAnt's
    # terrain rasterizer sign-extends a 16-bit offset before adding it to the
    # base selector, so crossing offset 0x8000 flips the selector (RPL 3 -> 2);
    # both selectors share a descriptor and must map to the same block, not miss
    # and fall through to real-mode (which left the DIB's top half black).
    sb: dict[int, int] = {}
    h = HugeHeap(sb, 0x100000, 0x400000)
    s = h.alloc(0x10000)                     # a full 64K block
    assert descriptor(s) == descriptor(s - 1)
    assert sb[descriptor(s)] == 0x100000
    assert h.free(s)
    assert descriptor(s) not in sb


def test_huge_block_consecutive_selectors_contiguous():
    sb: dict[int, int] = {}
    h = HugeHeap(sb, 0x100000, 0x400000)
    s = h.alloc(164352)                      # 512x320 page: 3 x 64K
    assert s
    # three consecutive selectors 8 apart, mapping to contiguous 64K regions —
    # this is what makes `selector += __AHINCR(8)` walk the block correctly.
    assert sb[descriptor(s)] == 0x100000
    assert sb[descriptor(s + 8)] == 0x100000 + SEG
    assert sb[descriptor(s + 16)] == 0x100000 + 2 * SEG
    assert descriptor(s + 24) not in sb      # only 3 selectors


def test_free_reclaims_linear_and_selectors():
    sb: dict[int, int] = {}
    h = HugeHeap(sb, 0x100000, 0x110000)     # tight 64K space
    a = h.alloc(40000)
    b = h.alloc(20000)
    assert a and b
    assert h.alloc(40000) == 0               # exhausted
    assert h.free(a)
    assert descriptor(a) not in sb           # selector unmapped
    c = h.alloc(40000)                        # reuses a's reclaimed space
    assert c and descriptor(c) in sb
    assert h.free(b) and h.free(c)


def test_exhaustion_returns_zero_not_crash():
    sb: dict[int, int] = {}
    h = HugeHeap(sb, 0x100000, 0x101000)     # only 4K
    assert h.alloc(1000)
    assert h.alloc(1000)
    assert h.alloc(1000)
    assert h.alloc(1000)
    assert h.alloc(1000) == 0                 # out of linear space


def test_globalflags_lock_count_low_byte():
    # GlobalFlags' low byte is the lock count; a discardable cache reads it to
    # skip blocks still in use.  Lock/unlock count, saturating and never below 0.
    sb: dict[int, int] = {}
    h = HugeHeap(sb, 0x100000, 0x400000)
    s = h.alloc(1000)
    assert h.flags(s) == 0                    # freshly allocated, unlocked
    h.lock(s); h.lock(s)
    assert h.flags(s) & 0xFF == 2             # two locks held
    assert h.unlock(s) == 1 and h.flags(s) & 0xFF == 1
    assert h.unlock(s) == 0 and h.flags(s) & 0xFF == 0
    assert h.unlock(s) == 0                   # never underflows
    assert h.flags(0xDEAD) == 0              # unknown handle -> 0, no crash


def test_discardable_attribute_toggles_and_clears_on_free():
    # A block allocated moveable is later re-marked discardable in place
    # (GlobalReAlloc GMEM_MODIFY); GlobalFlags must then report 0x0100 so the
    # tile cache can identify it as evictable.  Freeing forgets the attribute.
    sb: dict[int, int] = {}
    h = HugeHeap(sb, 0x100000, 0x400000)
    s = h.alloc(1000)                         # moveable, not discardable
    assert not (h.flags(s) & 0x0100)
    h.set_discardable(s, True)
    assert h.flags(s) & 0x0100               # now reported discardable
    h.set_discardable(s, False)
    assert not (h.flags(s) & 0x0100)
    # alloc(discardable=True) is the direct path
    d = h.alloc(1000, discardable=True)
    assert h.flags(d) & 0x0100
    h.free(d)
    assert h.flags(d) == 0                    # attribute + lock state gone
    h.set_discardable(0xDEAD, True)          # unknown handle -> no-op, no crash
    assert h.flags(0xDEAD) == 0


def test_pickle_restore_backfills_pre_globalflags_snapshots():
    # Regression: vmsnap snapshots recorded before the GlobalFlags state
    # existed pickle a HugeHeap with no _discardable/_locks; the resumed
    # machine's first GlobalLock then died with AttributeError.  __setstate__
    # must backfill the missing fields.
    import pickle

    sb: dict[int, int] = {}
    h = HugeHeap(sb, 0x100000, 0x400000)
    s = h.alloc(1000)
    state = h.__getstate__() if hasattr(h, "__getstate__") else dict(h.__dict__)
    state = dict(state)
    state.pop("_discardable")
    state.pop("_locks")                      # the pre-GlobalFlags pickle shape
    restored = pickle.loads(pickle.dumps(h))
    restored.__setstate__(state)
    assert restored.lock(s) == 1             # backfilled, no AttributeError
    assert restored.flags(s) & 0xFF == 1
    assert not (restored.flags(s) & 0x0100)
