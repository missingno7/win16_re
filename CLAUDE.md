# CLAUDE.md — win16_re: oracle-driven Win16 recovery (target: Paulie Python)

A **16-bit Windows 3.x (NE)** recovery framework, applying the oracle-driven method
proven on DOS games by [`dos_re`](D:/Games/DOS/dos_re) (Prehistorik 2, Overkill).
`win16/` is the **game-agnostic** layer (NE loader + the Windows API surface + demos/
snapshots/audio); it is the Win16 analogue of dos_re itself.

**The RE target is Paulie Python 1.0** (Way Out West-ware, `assets/PPYTHON/PYTHON.EXE`);
its adapter is `ppython/`. Other games under `assets/` (MICROMAN, BANGBANG, KYE,
SKIFREE) are **test fixtures only** — we boot them through the shared `win16/` layer
(`scripts/boot.py <game>`) to exercise and harden it, never to fully recover them.
Stay focused on PPYTHON for recovery; use the others to find gaps in `win16/`.
(Repo dir is still `ppython_re/`; rename to `win16_re/` is cosmetic and pending.)

**The method is dos_re's** — read `D:/Games/DOS/dos_re/START_HERE.md` and
`docs/ai_porting_charter.md` there. This repo is a game-port repo that *uses* the
framework from its sibling checkout at `D:\Games\DOS\dos_re` (added to `sys.path` by
`conftest.py` / `_env.py`; nothing is vendored).

## What is different from a DOS port

- **Loader:** NE (New Executable), not MZ. `win16/ne.py` parses it; `win16/loader.py`
  maps segments into VM memory and applies relocations.
- **The OS is the first hook layer.** A Win16 app calls KERNEL/USER/GDI/SOUND through
  the NE import table. There is no "run the whole original under DOS" baseline — the
  Windows API surface is implemented **in Python from day one** (`win16/api/`), each
  call servicing exactly the behaviour the game proves it needs. The game's own code
  runs 100% in the VM interpreter; recovery then proceeds routine-by-routine exactly
  as in dos_re.
- **Floating point:** the EXE links `win87em` and uses INT 34h–3Dh FP-emulator
  interrupts (OSFIXUP relocations left unapplied = no x87 assumed). These interrupts
  are serviced by a Python x87 model (`win16/fpu.py`).
- **Frame boundary:** a message-pump game — the boundary is the message loop
  (`GetMessage`/`PeekMessage`) + the timer (`SetTimer`/`WM_TIMER`), not PIT/retrace.

## Layout

```
win16/            game-agnostic Win16 layer (candidate for promotion into dos_re):
  ne.py             NE file parser (pure, stdlib)
  loader.py         segment mapping + relocations into the dos_re VM
  api/              KERNEL/USER/GDI/SOUND + dialogs, implemented as Python services
  dialog.py/menu.py DLGTEMPLATE + MENU resource parsers
  dib.py/png.py/font8x8.py   graphics helpers
  interactive.py    real-time driver (wall-clock message pacing, pause-at-boundary)
  demo.py           record/replay the GetMessage+dialog stream (the RE baseline)
  vmsnap.py         full-machine snapshots + game-observable digest
ppython/          the game adapter (addresses, formats, recovered logic)
  recovered/        pure recovered game logic — never imports dos_re or win16 VM bits
  bridge/           typed views over VM memory (the ONE place offsets live)
  codecs/           native decoders for game asset formats (.PPS levels, .SET)
  probes/           throwaway observation scripts
scripts/          play.py (interactive; --record, F9 snapshot), replay.py (headless)
docs/ppython/     ledgers: run_status.md (journal), symbol_ledger.md, blockers.md
tests/            pytest; every test using assets/ must skip when assets/ is missing
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
