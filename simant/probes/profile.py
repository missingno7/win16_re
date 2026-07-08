"""PC-sampling profiler for SimAnt — find the hot loops worth lifting to hooks.

SimAnt has six code segments, so unlike microman we first have to LOCATE the
hot machine code before writing any island.  This samples the CPU program
counter every Nth instruction while the game runs, buckets by
(NE-segment, offset&~0xF), and reports the hottest buckets with the owning
segment resolved — the shortlist of loops to disassemble and lift.

    python -m simant.probes.profile [total_steps] [warmup_steps]

Prints the top buckets and (when SIMANTW.SYM is present) the nearest symbol,
so a hot address maps straight to a named routine to recover.
"""
from __future__ import annotations

import sys
from collections import Counter

from .. import _env  # noqa: F401  (dos_re on sys.path)
from .. import runtime
from .symbols import nearest_symbol
from dos_re.cpu import CPU8086


def profile(total_steps: int = 8_000_000, warmup: int = 3_400_000,
            stride: int = 16):
    """Boot SimAnt, run `warmup` steps, then PC-sample one in `stride`
    instructions over the next `total_steps`.  Returns (machine, Counter)
    where the Counter maps (seg_index, offset_bucket) -> hits."""
    m = runtime.create_machine()
    m.cpu.trace_enabled = False
    # seg base -> NE segment index (1-based), for resolving CS at sample time.
    base_to_seg = {b: i for i, b in enumerate(m.seg_bases) if b}

    try:
        m.cpu.run(warmup)
    except Exception:  # noqa: BLE001 — a frontier during warmup is still useful
        pass

    hist: Counter = Counter()
    st = m.cpu.s
    tick = 0
    orig = CPU8086.step

    def sample(self):
        nonlocal tick
        orig(self)
        tick += 1
        if tick % stride == 0:
            seg = base_to_seg.get(st.cs & 0xFFFF)
            if seg is not None:                     # ignore thunk/BIOS frames
                hist[(seg, st.ip & 0xFFF0)] += 1

    CPU8086.step = sample
    try:
        m.cpu.run(total_steps)
    except Exception:  # noqa: BLE001
        pass
    finally:
        CPU8086.step = orig
    return m, hist


def main(argv: list[str]) -> None:
    total = int(argv[0]) if argv else 8_000_000
    warmup = int(argv[1]) if len(argv) > 1 else 3_400_000
    _m, hist = profile(total, warmup)
    grand = sum(hist.values()) or 1
    print(f"sampled {grand} points over ~{total} steps "
          f"(after {warmup} warmup)\n")
    print(f"{'seg:off':>10}  {'hits':>6}  {'%':>5}  nearest symbol")
    for (seg, off), n in hist.most_common(25):
        sym = nearest_symbol(seg, off)
        print(f"  {seg}:{off:04X}  {n:6d}  {100.0 * n / grand:4.1f}%  {sym}")


if __name__ == "__main__":
    main(sys.argv[1:])
