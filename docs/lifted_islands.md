# Lifted islands — recovering a game's hot routines

Booting a game (the API frontier loop) gets it *running* in the interpreter.
Recovering the game's own hot code — for speed and for understanding — is the
dos_re **island** method, applied per game. An island is a Python
reimplementation of one hot ASM loop, hooked at its CS:IP via the dos_re CPU's
`replacement_hooks`, doing all the loop's work in one shot and writing back the
exact register/flag/memory state the ASM would have produced.

This repo has no game package of its own — the worked examples live in a consuming
game-port project. Currently that's `simant_port`'s `hooks.py`: the `__aFuldiv`
32-bit divide helper, the `_Unpack` LZSS asset decompressor, a far byte-memcpy, and
the `_Windows_MakeTable4x4` / `_1x1` terrain tile expanders — each gated byte-exact
by that project's `tests/test_hooks.py`.

## The pipeline

```text
PC-sample  →  live-trace the hot loop  →  lift it as a Python island  →  A/B pixel oracle
```

1. **PC-sample.** Wrap `CPU.step` and sample `CS:IP` every N instructions over
   a real run (drive from a snapshot of the interesting state — e.g. level 1).
   The buckets that dominate are your candidates.
2. **Live-trace the loop.** Enable `cpu.trace` at the loop head for ~100
   instructions and read the exact instruction/register sequence: what it reads,
   what it writes, how it addresses memory (huge-pointer walk? per-byte selector
   recompute?), and the exit state (final registers, flags, stack locals).
3. **Lift it.** Write a Python function that performs all remaining iterations
   as one slice operation over `mem.data`, then sets the exact final CPU state
   and jumps to the loop's exit IP. Match the loop **structurally** (read the
   frame offsets out of the matched code bytes) so one island covers every
   compiled clone, and **verify the code signature** at install time — refuse to
   install if the bytes don't match (a hook landing on different code corrupts
   silently).
4. **A/B pixel oracle.** Run a hooked and an unhooked machine over the same
   deterministic drive; require the window pixels to be **sha256-identical** at
   every checkpoint, and require each island family to actually fire. Byte-exact
   speed is the only accepted outcome — an island that renders "close" is a bug.

## Where islands live

Per game, never in `win16/`: the game-port project's `<game>/hooks.py` exposes
`install(machine) → int` (count installed), and `<game>/runtime.py` exposes
`install_hooks(machine)`. That project's own `play.py` installs them
(`--no-hooks` runs pure ASM). The selector heap makes these lifts clean:
consecutive selectors map to contiguous linear memory, so a
huge-pointer loop over VM memory is one `numpy`/`bytearray` slice.

## The memory-model advantage

Because `win16/hugeheap.py` maps a >64K block's consecutive selectors to
contiguous backing memory, an ASM loop that walks a huge pointer 64K at a time
(`selector += __AHINCR` on offset wrap) is, in our model, a single contiguous
linear span. That is what makes the WAP fill/copy loops — 25 interpreted
instructions per byte in the original — collapse to one `mem.data[a:b] = ...`.
This is the SimAnt rehearsal: its simulation will have the same shape of hot
loop, and the same pipeline lifts it.
