# The Win16 layer — how a Windows port differs from a DOS one

`dos_re` runs an MZ/COM program bottom-up: the whole original executes under a
faithful DOS/hardware model, and you lift routines out of it. A Win16 game is
different in one decisive way: **the operating system is the first hook layer**.

## The OS API is the hook layer (day one)

A Win16 game does almost nothing by itself — it calls Windows. Every function
it imports (KERNEL / USER / GDI / SOUND / MMSYSTEM) is resolved through the NE
import table to a **thunk slot** in a dedicated segment (`THUNK_SEG = 0x0060`);
a replacement hook at that CS:IP services the call in Python (`win16/api/`).
So unlike a DOS port, there is no "run the whole original under DOS" baseline —
the Windows API surface is implemented in Python from the start, each call
doing exactly what the game proves it needs (`docs/bringing_up_a_game.md`).

The game's *own* code still runs 100% in the VM interpreter. Recovery of the
game's logic then proceeds routine-by-routine exactly as in dos_re (see
[`lifted_islands.md`](lifted_islands.md)); the API layer is the environment
that logic runs in.

```text
game NE segments (interpreted 8086)  ──calls──►  thunk slot 0060:xxxx
                                                        │
                                                        ▼
                                          win16/api/<module>.py  (Python service)
```

`INT 21h` is also serviced (Windows apps and their C runtime call DOS
directly): `win16/loader.py`'s interrupt handler routes it to the same
`DOS_SERVICES` table as `KERNEL.DOS3Call`.

## The frame boundary is the message loop

A DOS game's frame boundary is the PIT/retrace wait. A Windows game's is the
**message loop**: `GetMessage`/`PeekMessage` returns the next input/paint/timer
message, the app dispatches it to a window proc, and repaints. Timers
(`SetTimer` → `WM_TIMER`) and self-invalidation (`InvalidateRect` → `WM_PAINT`)
drive animation. `win16/api/system.py` owns the message queue, the timer table,
and the virtual clock; the deterministic pump (`next_message`) drives headless
replay, and `win16/interactive.py` drives real-time play.

**Timing caveat — busy-wait clocks.** Some apps time a phase by busy-waiting on
the clock *without* pumping messages (SimAnt times its splash with
`while GetTickCount() - t0 < delay`). A message-boundary clock freezes such a
loop forever. So `GetTickCount` returns `max(clock_ms, instruction_count //
INSTR_PER_MS)` — a monotonic, deterministic floor driven by execution progress.
Message-timed games keep their larger `clock_ms` unchanged.

## The memory model — static single-app protected mode

Win16 real-mode-style paragraph bases cap addressable memory at 1 MB, which
real games blow past. `win16/hugeheap.py` implements a **selector-based global
heap**: a 16-bit selector is not a paragraph base but an index into a
`sel_base` map to an arbitrary linear address, lifting the ceiling to 4 MB. The
loaded program's own segments stay real-mode-addressed low; `GlobalAlloc`
blocks live above 1 MB as selectors. Blocks larger than 64K get consecutive
selectors 8 apart (`__AHINCR`), each mapping to the next contiguous 64K, so an
app's huge-pointer walk lands on the right descriptor. The DOS path (no
`sel_base`) is byte-identical, so `dos_re` is unaffected.

This is deliberately *static* single-app protected mode — one app, no
LDT/GDT/task-switching — because that is all a single game needs, and it keeps
the model simple and inspectable.

## Rendering — DIBs, palettes, fonts

Games draw with `BitBlt`/`StretchBlt`/`TextOut` and, for images,
`SetDIBitsToDevice`. The blit supports 4bpp (16-colour) and 8bpp BI_RGB DIBs
with both `DIB_RGB_COLORS` (RGBQUAD table) and `DIB_PAL_COLORS` (WORD indices
into the DC's logical palette); it is numpy-vectorized with a cached LUT. The
palette chain is modelled as a *static single-app system palette*:
`RealizePalette` copies the realized logical palette into
`Win16System.system_palette`, which `GetSystemPaletteEntries` reports (games
nearest-match against it). Text uses a fixed 8×13 cell — a presentation
approximation; `CreateFont` returns a `Font` whose metrics map to that cell.

Rendering is **pixel-exact but mechanism-flexible** (the dos_re contract): the
game's per-window surface must match, but *how* we rasterize is ours.

## Child windows — "windows within a window"

A Win16 app draws into a **tree** of windows, not one surface. A top-level
frame window contains `WS_CHILD` children — toolbars, status bars, and
MDI-style client/canvas areas — positioned inside the parent's client area.

SimAnt is the worked example. Its window tree at startup:

```text
AntRoot        (handle 276, top-level frame, 627x400)
├── RibbonWindow  (handle 278, WS_CHILD, the toolbar; height grows later)
└── AntRoot        (handle 280, WS_CHILD, the game canvas — this is where the
                    splash and the simulation actually render)
```

The frame's own surface stays blank; the game renders into the child canvas.
Each window keeps its **own** surface — per-window byte-exact verification is
unaffected, and that is the level the oracle checks. But for *presentation* the
tree must be flattened: `win16/compositor.py` blits each visible child onto a
copy of its parent at the child's `(x, y)`, recursively, clipped to the parent
bounds, producing one image.

Rules that keep this correct:

- **Only top-level windows are "real" to a host.** `play.py` gives an OS window
  to each `compositor.top_level_windows(sys)` entry; `WS_CHILD` windows do
  **not** get their own OS window — they composite into their parent's view.
- **Composite is a presentation copy.** `composite()` never mutates a game
  surface; the oracle still sees each window's own pixels.
- **Redraw on any descendant change.** `compositor.tree_version(sys, win)` sums
  the surface versions over the window and its descendants, so a child
  repainting triggers the parent's redraw.
- **Child position is parent-client-relative.** A child at `(0,0)` sits at the
  parent's client origin; the compositor clips children to the parent bounds.

If you add a game whose display looks "detached" (pieces in separate windows),
the cause is almost always child windows being treated as top-level — route
them through the compositor.

## What is game-specific vs. shared

Shared (`win16/`): the NE loader, the API surface, the memory model, rendering,
compositing, demos/snapshots, audio. Game-specific (a game package): the EXE
path and boot flags (`runtime.py`), any lifted hot-path hooks (`hooks.py`), and
— for the RE target — the recovered game logic (`recovered/`) and its typed
memory views. The framework gives you the machine, the proof engines, and the
method; the knowledge of *your* game is earned from *your* oracle.
