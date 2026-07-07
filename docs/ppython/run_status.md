# Paulie Python — run status (newest on top)

## Standing mechanisms (check here before building new tooling)
- **Boot probe:** `python -m ppython.probes.boot [max_steps]` — runs from the NE entry
  point, prints the stop reason (the frontier), last trace lines, and the API call log.
- **NE inspection:** `win16/ne.py` parses everything (segments, relocs, entry table,
  resources); `NEExecutable.find_resources("BITMAP")` etc.
- **API surface:** `win16/api/core.py` `ApiRegistry` — register handlers with
  `@api.register(mod, ordinal, args="word str long", ret="word|long|void")`;
  unregistered imports fail loud (`Win16ApiGap`) naming MODULE.ord:Name + call site.

## 2026-07-07 — the MSC C startup chain is complete; frontier is inside WinMain
- Implemented, one observed call at a time (each verified in the boot trace):
  `InitTask` (full register contract: AX=1 BX=81 CX=stack DX=nCmdShow SI=hPrev
  DI=hInst ES=PSP; instance-data stack words in DGROUP), `WaitEvent`,
  `GetVersion` (0x05000A03), `DOS3Call` AH=30h/35h/25h (version + Python-side
  interrupt-vector table), `InitApp`, `__fpMath` BX=0/2/3 (install/deinstall/
  set-error-handler — handler seg1:8310 recorded), `LockSegment`/`UnlockSegment`
  (identity in the flat mapping), `LocalAlloc`/`LocalFree`/`LocalSize` over a real
  first-fit DGROUP heap allocator (`win16/api/localheap.py`),
  `GetModuleFileName` (virtual DOS path C:\PYTHON.EXE), `GetDOSEnvironment`
  (PATH= block + WORD 1 + exe path).
- **545 instructions of crt0 run clean; WinMain = seg1:5EB0** (near-called from
  the seg1:0033 thunk). Frontier: USER.173:LoadCursor from seg1:5EF9 — the app's
  window-class setup. Next: the USER windowing model (class/window objects,
  message queue, WndProc far-callbacks into VM code), then CreateWindow →
  message loop → first paintable frame.
- Suite: 13 passed.

## 2026-07-07 — bring-up: NE loader boots PYTHON.EXE to the first API frontier
- Target identified: **Paulie Python 1.0** (Way Out West-ware), Win 3.x NE app.
  2 segments (CODE 0x8C91 @seg1, DATA/DGROUP 0x5940 @seg2, stack 0x1400 heap 0x1000),
  entry seg1:61EA, 105 unique imports by ordinal from KERNEL/USER/GDI/SOUND/win87em,
  25 DIB bitmap resources, 1 menu, 6 dialogs, 1 accel table. Level data in
  WAYOUT0..7.PPS (10080 bytes each), settings/scores in WAYOUT.SET.
- **Architecture decided:** dos_re VM (8086 core, hooks, snapshots) + new game-agnostic
  `win16/` layer: NE parser + loader (real-mode-style flat segment mapping; selector ==
  paragraph base), import thunk segment 0x0060 with one hooked slot per (module,
  ordinal) — **the Windows OS itself is the first Python hook layer**. The game's own
  code runs 100% interpreted.
- **FP model:** OSFIXUP relocations (82 sites) deliberately unapplied → the CD 34..3D
  (INT 34h–3Dh) win87em emulator forms stay live; `__WINFLAGS` equate = 0x0013
  (PMODE|CPU286|STANDARD, **no WF_80x87**). INT 34h–3Dh will be serviced in Python.
- **Boot evidence:** entry runs `xor bp,bp; push bp; call far KERNEL.91:InitTask` —
  the classic MSC Win16 C startup — and fails loud at the InitTask thunk. Relocations
  verified: all 100+ far-call import sites point into the thunk segment; internal
  SEGMENT16/OFFSET16 fixups + the equate applied; chained fixups handled.
- Suite: 10 passed. Next: implement the startup API chain (InitTask → __fpMath init →
  InitApp → WinMain) one observed call at a time.
