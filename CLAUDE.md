# CLAUDE.md — win16_re: oracle-driven reverse-engineering for 16-bit Windows games

A **16-bit Windows 3.x (NE)** reverse-engineering framework, applying the oracle-driven
method proven on DOS games by [`dos_re`](dos_re) (Prehistorik 2, Overkill).
`win16/` is the **game-agnostic** layer (NE loader + the Windows API surface + memory
model + rendering + compositing + demos/snapshots/audio); it is the Win16 analogue of
dos_re itself. Read [`AGENTS.md`](AGENTS.md) and [`docs/README.md`](docs/README.md) first.

**The RE target is Maxis SimAnt** (`assets/ANTWIN/SIMANTW.EXE`, adapter `simant/`) — the
**sole target** of this project.  A full commercial Win16 app (6 code segs,
KEYBOARD+WIN87EM, native inline x87, raw INT 21h I/O, programmatic menus, 16-colour DIBs,
**child windows within a window**, huge-pointer tile renderers).  It boots, runs in-game,
and its source is being recovered routine-by-routine — clean, readable, byte-exact Python
in [`simant/recovered/`](simant/recovered) with hot-loop islands in
[`simant/hooks.py`](simant/hooks.py) (see [`docs/lifted_islands.md`](docs/lifted_islands.md)),
each gated byte-exact by an A/B oracle.

Boot it with `scripts/boot.py simant` to find the next `win16/` gap.

> This framework was first hardened on other games (Paulie Python, MicroMan, a few more);
> those have been **moved out** to a separate project.  SimAnt is the only game here now —
> if any doc still centres another game, it is stale.

**The method is dos_re's** — read [`dos_re/docs/ai_porting_charter.md`](dos_re/docs/ai_porting_charter.md)
for the full phased method. dos_re is a **git submodule** of this repo, pinned at
`dos_re/` (https://github.com/missingno7/dos_re.git) — `git clone --recurse-submodules`
(or `git submodule update --init`) is all a fresh checkout needs. Each package's
`_env.py` (+ `conftest.py`) puts it on `sys.path`; `DOS_RE_PATH` is a deliberate opt-in
escape hatch for co-developing dos_re itself against a separate working checkout.

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
simant/           the RE TARGET: runtime.py + _env.py (dos_re on sys.path)
  recovered/        pure recovered SimAnt logic — never imports dos_re or win16 VM bits
  hooks.py          lifted islands (hot ASM routines reimplemented, byte-exact)
  probes/           profile.py (PC-sampler) + symbols.py (SIMANTW.SYM name lookup)
  tests/            island A/B oracles + boot/splash
scripts/          play.py (interactive; --record, --resume, F9), boot.py (frontier probe),
                  replay.py (headless), games.py (the game registry + hook loader)
docs/             the method (docs/README.md is the index); docs/simant/ the ledgers
tests/            shared win16/-layer pytest
assets/           the original game files (gitignored, never committed) — ANTWIN/ = SimAnt
```

**This is an AI-operated harness.** Only a human is needed to *play* (generate input);
everything else is for the agent. VM stops and gaps go to the **console** (stderr) with
CS:IP + instruction count + traceback + trace tail + API log — never trapped in the GUI.
Evidence tooling mirrors dos_re: demos (`scripts/replay.py`) and snapshots are the
deterministic verification baseline.

## Non-negotiables (inherited from dos_re — enforced, not aspirational)

- Never commit red: `python -m pytest -q` green before every commit; one verified
  slice = one focused commit.
- Never weaken an oracle/test to make a slice pass. Blocked ⇒ revert + note it in
  `docs/simant/` (the ledgers).
- Fail loud, never fake: an unimplemented API/opcode/format raises; no silent
  plausible fallbacks. Implement observed behaviour, not datasheet generality.
- `win16/` never learns this game (no SIMANTW.EXE addresses/format knowledge);
  `simant/recovered/` never imports the VM.
- Update `docs/simant/run_status.md` (newest on top) as you go; the next session
  resumes from git + the ledgers alone.
