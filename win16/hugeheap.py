"""Selector-based global memory over a large linear space.

The win16 analogue of the Windows 3.x protected-mode global heap.  A 16-bit
selector is NOT a paragraph base here — it indexes `sel_base` (a dict the VM
Memory consults) mapping to an arbitrary linear address.  That lifts the 1MB
real-mode ceiling: `GlobalAlloc` blocks live in a large linear space (e.g.
[1MB, 4MB)), while the loaded program's own segments stay real-mode-addressed
in low memory (unmapped selectors fall back to `seg<<4`).

Huge (>64K) blocks get CONSECUTIVE selectors 8 apart, each mapping to the next
contiguous 64K of the block — so the app's huge-pointer walk (`selector +=
__AHINCR`, __AHINCR == 8) lands on the right 64K, and a linear read across the
whole block is still contiguous in the backing store.
"""
from __future__ import annotations

SEG = 0x10000                       # 64K per selector step
SEL_RPL = 0x07                      # TI=1 (LDT), RPL=3 — a typical Win16 selector


def descriptor(sel: int) -> int:
    """The descriptor a selector resolves to: index + TI, with the RPL bits
    masked off.  The VM Memory keys sel_base by this and masks the RPL on every
    lookup (see dos_re Memory._xlat), so all four RPL aliases of a selector
    resolve to one block.  Win16 relies on that: SimAnt's terrain rasterizer
    walks a 64K DIB with a huge pointer whose 16-bit offset it SIGN-EXTENDS
    before adding to the base selector, so crossing offset 0x8000 decrements the
    selector (RPL 3 -> 2) — a no-op on real hardware, but it would miss a
    selector-exact map."""
    return sel & 0xFFFC


class HugeHeap:
    def __init__(self, sel_base: dict[int, int], lin_start: int, lin_end: int,
                 first_index: int = 0x0300) -> None:
        self.sel_base = sel_base                    # shared with the VM Memory
        self._lin_free: list[tuple[int, int]] = [(lin_start, lin_end - lin_start)]
        self._next_index = first_index              # selector = index<<3 | RPL
        self.first_selector = (first_index << 3) | SEL_RPL
        # Lowest selector the VM should treat as a global-heap selector: the
        # RPL-0 alias of the first (see _map_rpl_aliases), so RPL variants of it
        # still clear the sel_min fast-path gate in Memory.
        self.selector_floor = self.first_selector & 0xFFFC
        self._sel_free: list[tuple[int, int]] = []  # (start_selector, count)
        self._blocks: dict[int, tuple[int, int, int, int]] = {}
        #              base_selector -> (lin_base, lin_size, n_selectors, req_size)
        # GlobalFlags state: GMEM_DISCARDABLE blocks + per-handle lock counts.
        # A discardable cache (SimAnt's tile chunk-heap) evicts by GlobalFlags:
        # it only frees blocks reported discardable and NOT locked, so both must
        # be tracked or the cache finds nothing evictable and panics.
        self._discardable: set[int] = set()
        self._locks: dict[int, int] = {}

    def __setstate__(self, state: dict) -> None:
        """Restore from a pickle (vmsnap snapshots / verifier clones).

        Snapshots recorded before the GlobalFlags state existed carry no
        `_discardable`/`_locks`; backfill them so a resumed machine's first
        GlobalLock/GlobalFlags does not die on the restored heap."""
        self.__dict__.update(state)
        self.__dict__.setdefault("_discardable", set())
        self.__dict__.setdefault("_locks", {})

    # -- linear space (byte-granular, coalescing) --------------------------
    def _alloc_lin(self, size: int) -> int | None:
        for i, (base, avail) in enumerate(self._lin_free):
            if avail >= size:
                if avail == size:
                    del self._lin_free[i]
                else:
                    self._lin_free[i] = (base + size, avail - size)
                return base
        return None

    def _free_lin(self, base: int, size: int) -> None:
        self._lin_free.append((base, size))
        self._lin_free.sort()
        merged: list[tuple[int, int]] = []
        for b, n in self._lin_free:
            if merged and merged[-1][0] + merged[-1][1] == b:
                merged[-1] = (merged[-1][0], merged[-1][1] + n)
            else:
                merged.append((b, n))
        self._lin_free = merged

    # -- selector values (runs) --------------------------------------------
    def _alloc_selectors(self, count: int) -> int | None:
        for i, (start, n) in enumerate(self._sel_free):
            if n >= count:
                if n == count:
                    del self._sel_free[i]
                else:
                    self._sel_free[i] = (start + count * 8, n - count)
                return start
        start = (self._next_index << 3) | SEL_RPL
        if ((self._next_index + count) << 3) > 0xFFFF:
            return None                             # 16-bit selector space full
        self._next_index += count
        return start

    def _free_selectors(self, start: int, count: int) -> None:
        self._sel_free.append((start, count))

    # -- public API --------------------------------------------------------
    def alloc(self, size: int, *, discardable: bool = False) -> int:
        """Returns a base selector (handle), or 0 on failure."""
        n = max((size + SEG - 1) // SEG, 1)
        lin_size = n * SEG if n > 1 else max(size, 1)
        lin_base = self._alloc_lin(lin_size)
        if lin_base is None:
            return 0
        base_sel = self._alloc_selectors(n)
        if base_sel is None:
            self._free_lin(lin_base, lin_size)
            return 0
        for k in range(n):
            self.sel_base[descriptor(base_sel + k * 8)] = lin_base + k * SEG
        self._blocks[base_sel] = (lin_base, lin_size, n, size)
        if discardable:
            self._discardable.add(base_sel)
        return base_sel

    # -- GlobalFlags state (lock count + GMEM_DISCARDABLE) ------------------
    def lock(self, base_sel: int) -> int:
        """GlobalLock: bump the lock count (saturating at 0xFF, as GlobalFlags'
        low byte does)."""
        if base_sel in self._blocks:
            self._locks[base_sel] = min(self._locks.get(base_sel, 0) + 1, 0xFF)
        return self._locks.get(base_sel, 0)

    def unlock(self, base_sel: int) -> int:
        """GlobalUnlock: drop the lock count; returns the remaining count."""
        n = self._locks.get(base_sel, 0)
        if n > 0:
            n -= 1
            self._locks[base_sel] = n
        return n

    def set_discardable(self, base_sel: int, on: bool) -> None:
        """GlobalReAlloc(GMEM_MODIFY): toggle the GMEM_DISCARDABLE attribute of
        an existing block without moving it.  Win16 apps allocate moveable then
        re-mark discardable, which is what makes a block cache-evictable."""
        if base_sel not in self._blocks:
            return
        if on:
            self._discardable.add(base_sel)
        else:
            self._discardable.discard(base_sel)

    def flags(self, base_sel: int) -> int:
        """GlobalFlags word: low byte = lock count, 0x0100 = GMEM_DISCARDABLE."""
        if base_sel not in self._blocks:
            return 0
        f = self._locks.get(base_sel, 0) & 0xFF
        if base_sel in self._discardable:
            f |= 0x0100
        return f

    def free(self, base_sel: int) -> bool:
        info = self._blocks.pop(base_sel, None)
        if info is None:
            return False
        lin_base, lin_size, n, _size = info
        for k in range(n):
            self.sel_base.pop(descriptor(base_sel + k * 8), None)
        self._free_lin(lin_base, lin_size)
        self._free_selectors(base_sel, n)
        self._discardable.discard(base_sel)
        self._locks.pop(base_sel, None)
        return True

    def free_bytes(self) -> int:
        """Total unallocated linear space (GetFreeSpace)."""
        return sum(avail for _base, avail in self._lin_free)

    def largest_free_block(self) -> int:
        """Biggest single contiguous free run (GlobalCompact return)."""
        return max((avail for _base, avail in self._lin_free), default=0)

    def linear_base(self, base_sel: int) -> int | None:
        info = self._blocks.get(base_sel)
        return info[0] if info else None

    def size_of(self, base_sel: int) -> int:
        info = self._blocks.get(base_sel)
        return info[3] if info else 0

    def is_block(self, base_sel: int) -> bool:
        return base_sel in self._blocks
