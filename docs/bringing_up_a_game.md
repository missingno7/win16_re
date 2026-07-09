# Bringing up a new Win16 game — the frontier loop

This is the concrete procedure for getting a new NE game from "an EXE + its
data files" to "booted and rendering". It is the Win16 version of dos_re's
"load & run" bring-up, shaped by the fact that the OS API is the hook layer.

The whole procedure is a **fail-loud frontier loop**: run until the VM stops at
something unimplemented, identify exactly what it is *from its call site*,
implement the observed contract, repeat. Never stub a guess to move on — the
honest frontier is the value.

## 0. Register the game and make a package

Add it to `scripts/games.py` (`name → (exe path under assets/, winflags)`).
Create a package mirroring `simant/`: `_env.py` (copy), `__init__.py`,
`runtime.py` (`EXE_PATH`, `GAME_NAME`, `assets_present`, `create_machine`,
optional `install_hooks`), and `tests/`.

## 1. Probe the frontier

```bash
python scripts/boot.py <game> [max_steps]
```

`boot.py` loads the NE and runs it, then prints how far it got and **what
stopped it** — the unimplemented API / opcode / DOS service with CS:IP, the
last trace lines, and the API call log. That is the honest bring-up report. A
`Win16ApiGap` naming `USER.232` or `INT 21h AH=3Dh` is the next thing to do.

## 2. Identify the call — from its site, not a guess

The frontier tells you the *ordinal* (e.g. `USER.50`), not the function. Do not
guess from the ordinal number; **read the call site**. The reliable signals:

1. **Disassemble the pushes before the call.** The `push` sequence is the
   argument list (pascal order = left-to-right; a far pointer is pushed
   segment-then-offset). Count the words: it disambiguates `lstrlen` (one far
   pointer) from `lstrcmp` (two).
2. **Read the argument *values* at the gap.** Run to the gap, inspect the
   stack above the far-return address and the registers. A far pointer whose
   bytes spell `"fontres.fon"` identifies `AddFontResource`; a pointer to the
   PSP command tail identifies a command-line scan.
3. **Map neighbouring thunk slots.** The slot before a call is often a related
   function (`BeginPaint` then a text call → the second takes an hdc); the map
   is `{off: (module, ordinal)}` from `machine.api.slots`.
4. **Look at how the return is used.** A signed `jg` after the call means a
   comparison (`lstrcmp`); a handle checked against zero means a `Create*` /
   `Find*`; a discarded return means a side-effecting setter.

Only once the *identity and contract* are clear do you implement it. Cross-check
the ordinal against a Win16 reference, but the call site is the ground truth.

## 3. Implement the observed contract

Add the handler in the right `win16/api/<module>.py` with `@api.register`, give
it the argument spec the pushes implied, and return exactly what the game reads.
Model the behaviour the game exercises — not the whole MSDN surface. Add the
ordinal to `win16/api/ordinals.py` so the call log reads
`USER.50:FindWindow(...)` (the log format matters: tests and probes match on
the named form).

Honest minimal answers are often correct: `FindWindow` returns 0 (no prior
instance exists in a single-app model); `Escape(QUERYESCSUPPORT)` returns 0 (we
model no device escapes); `GlobalFlags` returns 0 (our blocks are always fixed
and resident). These are not fakes — they are the true behaviour of our model,
and the game takes its standard path.

When it is genuinely a new *mechanism* (programmatic menus, 4bpp DIBs,
selector-heap `GlobalReAlloc`), implement the mechanism in the shared layer.

## 4. Re-probe and repeat

Run `boot.py` again; the frontier has moved. Keep going until the game boots
through startup and paints. SimAnt took ~30 such steps to reach its splash.

## 5. Add a boot gate

When it renders, add a bounded boot test (`<game>/tests/test_boot.py`) that
drives to the render and asserts the **deep-startup API sequence** was called
(e.g. `CreateWindow` + `CreateMenu` + `CreateFont` + `SetDIBitsToDevice`) plus a
painted window. Gate on the API sequence, not a pixel-sum threshold or a fixed
instruction count — see [`pitfalls.md`](pitfalls.md) for why those are brittle.

## Then: recovery

Booting is the environment; recovering the game's own logic is the port. That
is the lifting loop — [`lifted_islands.md`](lifted_islands.md) and, for the full
phased method (equivalence contracts, the flip, the enhanced layer),
[`dos_re/docs/ai_porting_charter.md`](../dos_re/docs/ai_porting_charter.md).

## Where floating point shows up

Games that link `win87em` keep their x87 as INT 34h–3Dh emulator calls (the NE
OSFIXUP relocations are left unapplied). Those interrupts surface as a frontier
only when the game actually does FP — usually deep in the simulation, not on the
boot path. Build the x87 service (`win16/fpu.py`) against that real frontier,
not speculatively.
