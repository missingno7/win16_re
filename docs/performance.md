# Performance — running win16_re workloads fast

The full method and rationale live in
[`dos_re/docs/performance.md`](../dos_re/docs/performance.md) — PyPy for long
headless runs, CPython + pytest-xdist for big suites, and the equivalence gate
any interpreter change must pass. This page records only what is *different*
about the win16 layer, plus measured numbers on a real NE game (SimAnt, via
`simant_port`).

## PyPy: works, ~8x on headless interpretation

The win16 layer is importable under PyPy as-is. Its third-party dependencies
all resolve:

- **numpy** (compositor, GDI/USER blit fast paths) — ships PyPy 3.11 wheels.
- **pygame** (audio only, `win16/audio.py`) — no PyPy wheels, but the import
  is guarded: audio degrades to disabled with a console note. Headless
  workloads don't care.
- **tkinter / PIL** — only the interactive viewer (`play.py` in a game-port
  project). The viewer stays on CPython, same rule as dos_re's pygame viewer.

Measured (PyPy 3.11 v7.3.20 vs CPython 3.11, Windows, SimAnt boot,
20M instructions, identical end CS:IP on both):

| Workload | CPython | PyPy | speedup |
|---|---|---|---|
| headless interpretation, trace off | 0.46M instr/s | 3.69M instr/s | **8x** |
| `boot.py` frontier probe (trace **on**) | 0.20M instr/s | 0.38M instr/s | 1.9x |
| simant_port test suite | 6.5s | 4.6s | 1.4x |

Why 8x and not dos_re's 13–17x: a Win16 game constantly crosses the API hook
boundary into Python service code (message loop, GDI, timers), which breaks
JIT traces; a pure-ASM DOS loop doesn't. Trace-enabled runs are worse still —
per-instruction string formatting dominates and doesn't JIT. So: **PyPy pays
off on replay, island A/B oracles, and verify sweeps; don't bother for
`boot.py`**, whose whole point is the trace.

No install/config is needed beyond the interpreter itself: every entry point
reaches `dos_re` through the repo-relative `sys.path` shims (`simant/_env.py`
→ `win16/_env.py`), never through a pip install, so any interpreter that can
run the script resolves the whole chain.

## pytest-xdist: measured, currently a loss here

`-n auto` on the simant_port suite: 9.6s vs 6.5s serial — worker startup
outweighs the win on a ~36-test suite. dos_re's rule of thumb holds
unchanged (xdist needs many similar-cost tests); revisit when the suite has
grown a few times over, don't cargo-cult `-n auto` before then.
