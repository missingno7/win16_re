# win16_re

An **oracle-driven reverse-engineering framework for 16-bit Windows (NE) games** — the
game-agnostic layer only. This repo carries **no game**: no EXE, no assets, no game
package. It is consumed as a git submodule by separate game-port projects, the same way
it itself vendors [`dos_re`](dos_re) as a submodule.

A Win16 game runs inside a software 8086/80186 VM where the operating system is
a *Python hook layer*: every Windows API the program imports (KERNEL / USER /
GDI / SOUND / MMSYSTEM / …) resolves to a hooked thunk serviced in Python, and
individual hot ASM routines can be replaced with verified Python
reimplementations. The original binary stays the source of truth — a hooked run
is only accepted when it reproduces the original's behaviour **byte-for-byte**.

This is the [`dos_re`](../DOS/dos_re) method (proven on DOS games) carried onto
the Windows 3.x New-Executable format.

## What's here

`win16/` — NE loader, the selector-based memory model (static single-app protected mode,
4 MB), the full Win16 API surface, windowing, dialogs, menus, palette/DIB rendering,
audio, demos, snapshots. `win16/_env.py` (imported by `win16/__init__.py`) puts the
`dos_re` submodule on `sys.path`, so importing `win16` transparently makes `dos_re`
importable too — a consuming project never needs to set that up itself.

`tests/` — this framework's own pytest suite, exercised against synthetic state (no game
EXE needed).

## Consuming this framework

A game-port project vendors this repo as a git submodule (e.g. `win16_re/`) and supplies
its own game package: `runtime.py` (EXE path, boot flags, `create_machine`,
`assets_present`, optional `install_hooks`), its own `scripts/` (`play.py`, `boot.py`),
its own `assets/`, and its own recovered game logic + lifted-island hooks. `win16/` never
imports from a consumer. The current consumer is **`simant_port`** (Maxis SimAnt) — a
sibling project, not part of this repo.

## Working principles

- **Fail loud, never fake.** An unimplemented API / opcode / DOS service stops
  with a named frontier rather than guessing — the honest bring-up report.
- **Never weaken an oracle to make a slice pass.** The byte-exact proof is the
  value. A lifted hook is only accepted when an A/B run (original ASM vs. Python
  replacement) is pixel- and state-identical.
- **Game logic stays VM-free**; the VM/hook machinery stays in `win16/`.

## Status

The test suite is the gate — run `python -m pytest -q` before any commit; never commit
red. Bring-up notes and journals are per-game, so they live in the consuming project
(e.g. `simant_port`'s `docs/run_status.md`), not here.
