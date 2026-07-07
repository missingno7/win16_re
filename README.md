# win16_re

An **oracle-driven reverse-engineering framework for 16-bit Windows (NE) games.**

A Win16 game runs inside a software 8086/80186 VM where the operating system is
a *Python hook layer*: every Windows API the program imports (KERNEL / USER /
GDI / SOUND / MMSYSTEM / …) resolves to a hooked thunk serviced in Python, and
individual hot ASM routines can be replaced with verified Python
reimplementations. The original binary stays the source of truth — a hooked run
is only accepted when it reproduces the original's behaviour **byte-for-byte**.

This is the [`dos_re`](../DOS/dos_re) method (proven on DOS games) carried onto
the Windows 3.x New-Executable format.

## The layers

| Layer | What it is |
|-------|-----------|
| `win16/` | The **game-agnostic** framework: NE loader, the selector-based memory model (static single-app protected mode, 4 MB), the full Win16 API surface, windowing, dialogs, menus, palette/DIB rendering, audio, demos, snapshots. Knows about *no specific game*. |
| `ppython/` | A game package: **Paulie Python**, the byte-exact RE target. |
| `microman/` | A game package: **MicroMan** (a WAP demo) — a fixture that hardens `win16/` and carries the lifted-island method (per-game hot-path hooks) on a real graphics engine. Holds its own `runtime`, `hooks`, and `tests/`. |
| `scripts/` | `play.py` (play a game interactively — real window, keyboard, mouse, audio, F9 snapshots, `--resume`), `boot.py` (bring-up frontier probe), `games.py` (the game registry). |

Each new game is its own package (`<game>/runtime.py` = EXE path, boot flags,
`create_machine`, optional `install_hooks`). The `win16/` layer never imports
from a game package.

## Running a game

```
python scripts/play.py --game microman --scale 2      # play it
python scripts/play.py --resume artifacts/snapshots/<snap>   # resume a snapshot
python scripts/boot.py <game> [max_steps]             # bring-up frontier report
```

`play.py` mirrors each Win16 window as a real OS window and reports every error
to the console (the game itself only needs the user to provide input).

## Working principles

- **Fail loud, never fake.** An unimplemented API / opcode / DOS service stops
  with a named frontier rather than guessing — the honest bring-up report.
- **Never weaken an oracle to make a slice pass.** The byte-exact proof is the
  value. A lifted hook is only accepted when an A/B run (original ASM vs. Python
  replacement) is pixel- and state-identical.
- **`domain`/game logic stays VM-free**; the VM/hook machinery stays in `win16/`.

## Status

Live bring-up notes and the standing-mechanisms registry are in
[`docs/ppython/run_status.md`](docs/ppython/run_status.md). The test suite is the
gate — run `python -m pytest -q` before any commit; never commit red.
