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


class HugeHeap:
    def __init__(self, sel_base: dict[int, int], lin_start: int, lin_end: int,
                 first_index: int = 0x0300) -> None:
        self.sel_base = sel_base                    # shared with the VM Memory
        self._lin_free: list[tuple[int, int]] = [(lin_start, lin_end - lin_start)]
        self._next_index = first_index              # selector = index<<3 | RPL
        self.first_selector = (first_index << 3) | SEL_RPL
        self._sel_free: list[tuple[int, int]] = []  # (start_selector, count)
        self._blocks: dict[int, tuple[int, int, int, int]] = {}
        #              base_selector -> (lin_base, lin_size, n_selectors, req_size)

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
    def alloc(self, size: int) -> int:
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
            self.sel_base[base_sel + k * 8] = lin_base + k * SEG
        self._blocks[base_sel] = (lin_base, lin_size, n, size)
        return base_sel

    def free(self, base_sel: int) -> bool:
        info = self._blocks.pop(base_sel, None)
        if info is None:
            return False
        lin_base, lin_size, n, _size = info
        for k in range(n):
            self.sel_base.pop(base_sel + k * 8, None)
        self._free_lin(lin_base, lin_size)
        self._free_selectors(base_sel, n)
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
