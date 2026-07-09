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

- **`win16/` stays game-agnostic.** No game addresses, filenames, formats, or
  per-title behaviour in the shared layer. Game knowledge lives in the game
  package (`simant/` — the sole target). `simant/recovered/` logic never
  imports the VM.
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
simant/           SimAnt — the byte-exact RE target and SOLE focus: adapter +
                  recovered/ + hooks.py (lifted islands) + probes/ (profiler,
                  SIMANTW.SYM lookup) + tests/ (island A/B oracles)
scripts/          play.py (interactive), boot.py (frontier probe), games.py (registry)
docs/             the method; docs/README.md is the index; docs/simant/run_status.md the journal
tests/            shared win16/-layer pytest
assets/           original game files (gitignored, never committed) — ANTWIN/ = SimAnt
```
(Other games this framework was hardened on — Paulie Python, MicroMan, and a few
more — have been moved out to a separate project; SimAnt is the only game here.)

The game package `simant/` exposes `runtime.py` (`create_machine`,
`assets_present`, `GAME_NAME`, optional `install_hooks`). `win16/` never imports
from it. `scripts/games.py` is the registry the launcher/probe use.

## Standard commands

```bash
python -m pytest -q                       # the suite — green before every commit
python scripts/boot.py <game> [max_steps] # bring-up frontier probe (honest report)
python scripts/play.py --game <game>      # play interactively (real window, input, audio)
python scripts/play.py --resume <snapdir> # resume from an F9 snapshot
```

## Things not to do

- Do not let `win16/` learn anything about a specific game.
- Do not return guessed stub values to get past a fail-loud frontier — identify
  the call from its site first, then implement the observed contract.
- Do not "clean up" original-behaviour quirks (flag shapes, wrap semantics,
  return codes) without oracle evidence — they are load-bearing.
- Do not trust a probe's negative result until you've checked the code path
  actually consults the probe (see [`docs/pitfalls.md`](docs/pitfalls.md)).
- Do not treat performance, or a window merely being non-blank, as proof of
  correctness.
