"""The Win16 local heap: allocations inside DGROUP's heap region.

A deliberately simple first-fit allocator with a free list.  Blocks are
handle == near pointer (fixed) — LocalLock/LocalUnlock are not modelled until
an executable imports them.  Sizes round to 4 bytes like real LOCALHEAP
granularity; LocalSize reports the rounded size.
"""
from __future__ import annotations

from dataclasses import dataclass, field

LMEM_ZEROINIT = 0x0040
GRANULARITY = 4


@dataclass
class LocalHeap:
    start: int                  # first usable offset within DGROUP
    end: int                    # one past the last usable offset
    blocks: dict[int, int] = field(default_factory=dict)   # ptr -> size
    free: list[tuple[int, int]] = field(default_factory=list)  # (ptr, size)

    def __post_init__(self) -> None:
        self.free = [(self.start, self.end - self.start)]

    def alloc(self, size: int) -> int:
        """Returns the block's near pointer, or 0 when the heap is exhausted."""
        size = max((size + GRANULARITY - 1) & ~(GRANULARITY - 1), GRANULARITY)
        for i, (ptr, avail) in enumerate(self.free):
            if avail >= size:
                if avail == size:
                    del self.free[i]
                else:
                    self.free[i] = (ptr + size, avail - size)
                self.blocks[ptr] = size
                return ptr
        return 0

    def free_block(self, ptr: int) -> bool:
        size = self.blocks.pop(ptr, None)
        if size is None:
            return False
        self.free.append((ptr, size))
        self.free.sort()
        # Coalesce adjacent free runs.
        merged: list[tuple[int, int]] = []
        for p, s in self.free:
            if merged and merged[-1][0] + merged[-1][1] == p:
                merged[-1] = (merged[-1][0], merged[-1][1] + s)
            else:
                merged.append((p, s))
        self.free = merged
        return True

    def size_of(self, ptr: int) -> int:
        return self.blocks.get(ptr, 0)
