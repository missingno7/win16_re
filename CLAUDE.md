# CLAUDE.md — win16_re: oracle-driven reverse-engineering framework for 16-bit Windows games

A **16-bit Windows 3.x (NE)** reverse-engineering framework, applying the oracle-driven
method proven on DOS games by [`dos_re`](dos_re) (Prehistorik 2, Overkill). `win16/` is
the **game-agnostic** layer (NE loader + the Windows API surface + memory model +
rendering + compositing + demos/snapshots/audio) — it is the Win16 analogue of dos_re
itself, and a candidate for eventual promotion into dos_re once proven on more than one
game. Read [`AGENTS.md`](AGENTS.md) and [`docs/README.md`](docs/README.md) first.

**This repo carries no game.** It never learns a specific game's addresses, filenames, or
formats — game knowledge lives in a separate *game-port project* that vendors `win16_re`
as a git submodule (mirroring how `win16_re` itself vendors `dos_re`). The current
consumer is **`simant_port`** (Maxis SimAnt) — a sibling project, not part of this repo.
This framework was first hardened bringing up several games (Paulie Python, MicroMan,
SimAnt, a few more); their game-specific code lives in their own projects now, not here.

**The method is dos_re's** — read [`dos_re/docs/ai_porting_charter.md`](dos_re/docs/ai_porting_charter.md)
for the full phased method. dos_re is a **git submodule** of this repo, pinned at
`dos_re/` (https://github.com/missingno7/dos_re.git) — `git clone --recurse-submodules`
(or `git submodule update --init`) is all a fresh checkout needs. `win16/_env.py` (imported
by `win16/__init__.py`) puts it on `sys.path`, so anything that imports `win16` gets
`dos_re` importable too — a consuming game-port project never needs to know dos_re is
nested underneath. `DOS_RE_PATH` is a deliberate opt-in escape hatch for co-developing
dos_re itself against a separate working checkout.

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
  _env.py           puts the dos_re submodule on sys.path (imported by __init__.py)
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
dos_re/           the DOS CPU/memory VM this layer runs on top of (git submodule)
docs/             the method (docs/README.md is the index) — game-agnostic
tests/            this framework's own pytest suite (no game package here)
```

**This is an AI-operated harness.** A game-port project drives this framework the same
way it drives dos_re: VM stops and gaps go to the **console** (stderr) with CS:IP +
instruction count + traceback + trace tail + API log — never trapped in a GUI. Evidence
tooling (demos, snapshots) is the deterministic verification baseline; the concrete
scripts (`play.py`, `boot.py`, `replay.py`) live in the consuming game-port project, not
here — this repo has no `assets/`, no game EXE, and nothing to boot on its own.

## Non-negotiables (inherited from dos_re — enforced, not aspirational)

- Never commit red: `python -m pytest -q` green before every commit; one verified
  slice = one focused commit.
- Never weaken an oracle/test to make a slice pass. Blocked ⇒ revert; the ledger for
  *why* lives in the consuming game-port project (this repo has no game-specific journal).
- Fail loud, never fake: an unimplemented API/opcode/format raises; no silent
  plausible fallbacks. Implement observed behaviour, not datasheet generality.
- **`win16/` never learns any specific game** (no game addresses, filenames, or format
  knowledge) — that is the one rule this repo exists to enforce. A new API / mechanism is
  added only when a concrete game's *actual call site* proves it needs it, with the
  observed argument/return contract documented — but the implementation itself must stay
  general enough to not encode that game's specifics.
