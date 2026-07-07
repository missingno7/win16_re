# CLAUDE.md — win16_re: oracle-driven reverse-engineering for 16-bit Windows games

A **16-bit Windows 3.x (NE)** reverse-engineering framework, applying the oracle-driven
method proven on DOS games by [`dos_re`](D:/Games/DOS/dos_re) (Prehistorik 2, Overkill).
`win16/` is the **game-agnostic** layer (NE loader + the Windows API surface + memory
model + rendering + compositing + demos/snapshots/audio); it is the Win16 analogue of
dos_re itself. Read [`AGENTS.md`](AGENTS.md) and [`docs/README.md`](docs/README.md) first.

**The RE target is Paulie Python 1.0** (`assets/PPYTHON/PYTHON.EXE`, adapter `ppython/`) —
the byte-exact recovery focus. The other games are their own packages:
- **`microman/`** (MicroMan, a WAP demo) — a fixture that hardens `win16/` and carries the
  lifted-island method (`microman/hooks.py`; see [`docs/lifted_islands.md`](docs/lifted_islands.md)).
- **`simant/`** (Maxis SimAnt, `assets/ANTWIN/SIMANTW.EXE`) — the **big stress target**: a
  full commercial Win16 app (6 code segs, KEYBOARD+WIN87EM, raw INT 21h I/O, programmatic
  menus, 16-colour DIBs, **child windows within a window**). It boots and paints its
  splash; bringing it further is what drives `win16/` toward completeness.
- BANGBANG / KYE / SKIFREE are registered but not yet brought up.

Boot any game with `scripts/boot.py <game>` to find the next `win16/` gap.

**The method is dos_re's** — read `D:/Games/DOS/dos_re/docs/ai_porting_charter.md` there
for the full phased method. This repo *uses* the framework from its sibling checkout at
`D:\Games\DOS\dos_re` (added to `sys.path` by `conftest.py` / each package's `_env.py`;
nothing is vendored).

## What is different from a DOS port

- **Loader:** NE (New Executable), not MZ. `win16/ne.py` parses it; `win16/loader.py`
  maps segments into VM memory and applies relocations.
- **The OS is the first hook layer.** A Win16 app calls KERNEL/USER/GDI/SOUND through
  the NE import table. There is no "run the whole original under DOS" baseline — the
  Windows API surface is implemented **in Python from day one** (`win16/api/`), each
  call servicing exactly the behaviour the game proves it needs. The game's own code
  runs 100% in the VM interpreter; recovery then proceeds routine-by-routine exactly
  as in dos_re.
- **Floating point:** an EXE that links `win87em` keeps its x87 as INT 34h–3Dh
  emulator interrupts (OSFIXUP relocations left unapplied = no x87 assumed). These are
  serviced by a Python x87 model in `win16/fpu.py` — **to be built against the first real
  FP frontier** (deep in a game's simulation), not speculatively. No game has hit it yet.
- **Frame boundary:** a message-pump game — the boundary is the message loop
  (`GetMessage`/`PeekMessage`) + the timer (`SetTimer`/`WM_TIMER`), not PIT/retrace.
- **Windows within a window:** apps draw into a tree of top-level + `WS_CHILD` windows;
  the display is the composite. See [`docs/win16_layer.md`](docs/win16_layer.md).

## Layout

```
win16/            game-agnostic Win16 layer (candidate for promotion into dos_re):
  ne.py             NE file parser (pure, stdlib)
  loader.py         segment mapping + relocations + INT dispatch into the dos_re VM
  hugeheap.py       selector-based global heap (static single-app protected mode, 4MB)
  api/              KERNEL/USER/GDI/SOUND/MMSYSTEM + dialogs, as Python services
  api/objects.py    the OS object graph (Window, DC, Menu, Font, Brush, Palette, ...)
  compositor.py     child-window compositing (windows within a window)
  msgbox.py         MessageBox button sets + return codes
  dialog.py/menu.py DLGTEMPLATE + MENU resource parsers
  dib.py/png.py/font8x8.py   graphics helpers
  interactive.py    real-time driver (wall-clock message pacing, pause-at-boundary)
  demo.py           record/replay the GetMessage+dialog stream (the RE baseline)
  vmsnap.py         full-machine snapshots + game-observable digest
  audio.py          host audio backend (square-wave + WAV, incl. SND_MEMORY)
ppython/          the RE target adapter (addresses, formats, recovered logic)
  recovered/        pure recovered game logic — never imports dos_re or win16 VM bits
  bridge/           typed views over VM memory (the ONE place offsets live)
  codecs/           native decoders for game asset formats
  probes/           throwaway observation scripts
microman/         WAP fixture: runtime.py + hooks.py (lifted islands) + tests/
simant/           SimAnt stress target: runtime.py + tests/ (boots + paints splash)
scripts/          play.py (interactive; --record, --resume, F9), boot.py (frontier probe),
                  replay.py (headless), games.py (the game registry + hook loader)
docs/             the method (docs/README.md is the index); docs/ppython/ the ledgers
tests/            shared-layer pytest; game-specific tests live under <game>/tests/
assets/           the original game files (gitignored, never committed)
```

**This is an AI-operated harness.** Only a human is needed to *play* (generate input);
everything else is for the agent. VM stops and gaps go to the **console** (stderr) with
CS:IP + instruction count + traceback + trace tail + API log — never trapped in the GUI.
Evidence tooling mirrors dos_re: demos (`scripts/replay.py`) and snapshots are the
deterministic verification baseline.

## Non-negotiables (inherited from dos_re — enforced, not aspirational)

- Never commit red: `python -m pytest -q` green before every commit; one verified
  slice = one focused commit.
- Never weaken an oracle/test to make a slice pass. Blocked ⇒ revert + entry in
  `docs/ppython/blockers.md`.
- Fail loud, never fake: an unimplemented API/opcode/format raises; no silent
  plausible fallbacks. Implement observed behaviour, not datasheet generality.
- `win16/` never learns this game (no PYTHON.EXE addresses/format knowledge);
  `ppython/recovered/` never imports the VM.
- Update `docs/ppython/run_status.md` (newest on top) as you go; the next session
  resumes from git + the ledgers alone.
