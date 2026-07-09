# AGENTS.md — win16_re

These instructions apply to the whole repository. They are written for AI
agents and humans working in this repo. Start with [`CLAUDE.md`](CLAUDE.md)
(the operational brief) and [`docs/README.md`](docs/README.md) (the method).

## What this repository is

An **oracle-driven reverse-engineering framework for 16-bit Windows (NE)
games** — the [`dos_re`](dos_re) method (proven on DOS: Prehistorik 2,
Overkill) carried onto Windows 3.x. A Win16 game runs inside the `dos_re`
8086/80186 VM; the operating system it calls (KERNEL / USER / GDI / SOUND /
MMSYSTEM) is a **Python hook layer**, and individual hot ASM routines are
replaced with verified Python reimplementations. The original binary stays the
oracle: a hooked run is accepted only when it reproduces the original's
behaviour byte-for-byte.

The framework (`win16/`) uses the `dos_re` VM as a **git submodule** pinned at
`dos_re/` (https://github.com/missingno7/dos_re.git; put on `sys.path` by
`conftest.py` / each package's `_env.py`). `git submodule update --init` after
cloning; `DOS_RE_PATH` overrides to a separate checkout when actively
co-developing dos_re itself.

## Working principles

Correctness beats speed. Traceability beats cleverness. Small verified progress
beats large intuitive rewrites.

- **`win16/` stays game-agnostic — this repo carries no game at all.** No game
  addresses, filenames, formats, or per-title behaviour anywhere here. Game
  knowledge lives in a separate game-port project that vendors this repo as a
  git submodule (currently `simant_port`, a sibling project — not part of this
  repo). If you find yourself wanting to write anything game-specific, it
  belongs in that project, not here.
- **Do not make the OS layer more general than a real game requires.** A new
  API / DOS service / opcode is added only when a concrete program calls it,
  identified from its *actual call site* (not guessed), with the observed
  argument/return contract documented. Datasheet completeness is scope creep.
- **Fail loud, never fake.** An unimplemented API / opcode / DOS service raises
  with precise context (`Win16ApiGap` / `NotImplementedError` with CS:IP). It
  does not return a plausible stub to "keep things moving". The honest frontier
  is the value — see [`docs/bringing_up_a_game.md`](docs/bringing_up_a_game.md).
- **Behaviour changes need tests, and never commit red.** `python -m pytest -q`
  is green before every commit; one verified slice = one focused commit.
- **Never weaken an oracle/test to make a slice pass.** A lifted hook is
  accepted only when an A/B run (original ASM vs. the Python replacement) is
  pixel- and state-identical. Blocked ⇒ revert, and record the repro.
- **Determinism is a feature.** The deterministic paths (headless replay, no
  wall clock) stay deterministic; time-driven behaviour (the interactive
  driver, `GetTickCount`'s instruction-derived clock) is deterministic or
  clearly opt-in.

## Where things live

```text
win16/            the game-agnostic Win16 layer (see docs/win16_layer.md):
  _env.py           puts the dos_re submodule on sys.path (imported by __init__.py)
  ne.py             NE (New Executable) parser
  loader.py         segment mapping + relocations into the dos_re VM; INT dispatch
  hugeheap.py       the selector-based global heap (static single-app protected mode)
  api/              KERNEL / USER / GDI / SOUND / MMSYSTEM + dialogs, as Python services
  api/objects.py    the OS object graph (Window, DC, Menu, Font, Brush, Palette, ...)
  compositor.py     child-window compositing for presentation
  msgbox.py         MessageBox button sets + return codes
  interactive.py    real-time driver (wall-clock message pacing)
  demo.py, vmsnap.py  record/replay + full-machine snapshots (the verification baseline)
  audio.py          host audio backend (square-wave + WAV)
dos_re/           the DOS CPU/memory VM this layer runs on top of (git submodule)
docs/             the method; docs/README.md is the index — game-agnostic
tests/            this framework's own pytest suite — no game package lives here
```
This repo has **no game package, no `scripts/`, no `assets/`**. A game-port project
(currently `simant_port`, a sibling project) vendors this repo as a git submodule and
supplies its own game package (`runtime.py` exposing `create_machine`, `assets_present`,
`GAME_NAME`, optional `install_hooks`), its own `play.py`/`boot.py`/`replay.py`, and its
own `assets/`. `win16/` never imports from a consumer.

## Standard commands

```bash
python -m pytest -q     # this framework's own suite — green before every commit
```
A game-port project runs its own suite (which exercises this framework plus its game
package) the same way — see that project's own `AGENTS.md` for its `play.py`/`boot.py`.

## Things not to do

- Do not let `win16/` learn anything about a specific game — that includes not adding
  a `scripts/` or `assets/` back into this repo; those belong to the consuming project.
- Do not return guessed stub values to get past a fail-loud frontier — identify
  the call from its site first, then implement the observed contract.
- Do not "clean up" original-behaviour quirks (flag shapes, wrap semantics,
  return codes) without oracle evidence — they are load-bearing.
- Do not trust a probe's negative result until you've checked the code path
  actually consults the probe (see [`docs/pitfalls.md`](docs/pitfalls.md)).
- Do not treat performance, or a window merely being non-blank, as proof of
  correctness.
