# Paulie Python — run status (newest on top)

## Standing mechanisms (check here before building new tooling)
- **Interactive player:** `python scripts/play.py [--speed N] [--scale N]` — a real
  tkinter window (worker thread runs the VM, GUI thread renders every Win16 window
  onto a 640×480 virtual desktop + forwards keyboard/mouse). Arrows steer; F2 new
  game / F3 sound / F4 pause / F5 scores / F10 exit (via the real accelerator table).
  Runs until it hits the DialogBox frontier (shown in the status bar), not a crash.
  Pacing lives in `win16/interactive.py` (`InteractiveDriver`, game-agnostic, GUI-free)
  installed as `Win16System.message_source`; headless replay leaves it None.
- **Gameplay gate:** `tests/test_gameplay.py` — boot→idle→WM_COMMAND(1050 New Game)→
  level intro msgbox + painted playfield + SOUND notes. The msgbox/sound logs are
  `api.services["messagebox_log"|"sound_log"]` (virtual-clock-stamped evidence).
- **Menu commands** (from the MENU resource): 1050 New(F2), 1100 Sound(F3),
  1150 Pause(F4), 1175 HighScores(F5), 1200 Exit(F10); attitudes 2151-2155
  (default 2153 Diamondback); control 2201 kbd / 2202 mouse; screen-set 2051-2053.

## 2026-07-07 — INTERACTIVE: scripts/play.py — a real controllable window
- Real-time play harness: worker thread runs the CPU; a tkinter/PIL GUI renders
  the windows and forwards live input. `GetMessage` now delegates to an optional
  `message_source` (`Win16System.get_message()`); `win16/interactive.py` paces
  timers to wall-clock time (drops missed ticks, blocks the CPU thread on a
  condition until input/next-timer). `--speed` scales time; `--scale` zooms.
- Faithful input path landed: **TranslateAccelerator** (matches WM_KEYDOWN/WM_CHAR
  against the accel table → WM_COMMAND; F2→New, F3→Sound, F4→Pause, F5→Scores,
  F1→Help, F8→Radar, F10→Exit) and **TranslateMessage** (WM_KEYDOWN→WM_CHAR for
  ASCII VKs). Mouse move/click → WM_MOUSEMOVE/L/RBUTTON in client coords to the
  window under the pointer. Verified: a synthesized VK_F2 WM_KEYDOWN starts a new
  game through the accelerator (deterministic test) and the threaded harness paints
  the playfield + responds to arrow steering.
- Suite: 15 passed (added the F2-accelerator gate).
- **Next unchanged:** DialogBox (high-score/about) is still the frontier — the
  player stops there gracefully; implementing it unlocks full game-over/menus.

## 2026-07-07 — GAMEPLAY: New Game plays itself blind — level, music, collisions, game over
- x87 landed in dos_re (ESC D8-DF subset per static census: 59 FWAIT+ESC sites;
  FILD/FLD/FSTP m32/m64/m80, FADDP/FMULP/FDIV(R)P/FSUB(R)P, FCOM(P)+FNSTSW,
  FLDCW/FSTCW+RC-honouring FISTP, FINIT; doubles-for-80-bit caveat documented).
  KEY CORRECTION: the NE file carries REAL x87 opcodes; OSFIXUPs would convert
  them to emulator INTs on FPU-less machines — we run them natively like Wine
  (which ignores OSFIXUPs) and __WINFLAGS could now honestly advertise a FPU.
- Full observed lifecycle with no input: WM_COMMAND(1050) → OpenSound +
  queue(512) → level loaded (FP layout math) → "Next Screen: Portrait of a
  Python" → playfield blitted (walls/mice/Paulie visible in game0 PNG; the
  radar shows the level IS a python face) → jingle (69 SOUND events) →
  Paulie crashes unsteered: "Collision!" ×3 → "GAME OVER!" → **frontier:
  USER.87:DialogBox (high-score dialog) — the next slice** (dialog resources,
  MakeProcInstance done as identity, dialog proc callbacks).
- StretchBlt = nearest-neighbour (COLORONCOLOR); GDI default BLACKONWHITE
  caveat noted in code — check against owner playtest evidence later.
- MessageBox auto-returns IDOK and logs. Suite: 14 passed (~22 s).
- **Next:** DialogBox + dialog procs → keyboard input (WM_KEYDOWN steering,
  accel F-keys) → an interactive/live viewer → then demos + the lockstep
  verifier per the dos_re method (GetMessage is the boundary).
- **Boot probe:** `python -m ppython.probes.boot [max_steps]` — runs from the NE entry
  point, prints the stop reason (the frontier), last trace lines, and the API call log.
- **NE inspection:** `win16/ne.py` parses everything (segments, relocs, entry table,
  resources); `NEExecutable.find_resources("BITMAP")` etc.
- **API surface:** `win16/api/core.py` `ApiRegistry` — register handlers with
  `@api.register(mod, ordinal, args="word str long", ret="word|long|void")`;
  unregistered imports fail loud (`Win16ApiGap`) naming MODULE.ord:Name + call site.

## 2026-07-07 — THE GAME RUNS: full boot → intro → idle loop, Paulie-O-Meter renders
- **PYTHON.EXE now runs indefinitely in the VM with zero gaps** (5M+ steps): crt0 →
  WinMain → WM_CREATE (level file read via OpenFile+DOS handle calls, 26 LoadBitmaps,
  1344×960 playfield + 168×120 radar offscreen buffers, timers 140/250/4000 ms) →
  intro window (4 s timer) → DestroyWindow → the idle message loop with WM_TIMER +
  WM_PAINT flowing. `python -m ppython.probes.screenshot` dumps window PNGs:
  the Paulie-O-Meter shows SCORE/LIVES/BONUS/LEVEL/MICE TO GO/SCREEN SET in colour.
  Main window black = correct (no game started; needs menu WM_COMMAND input).
- **The frame boundary is `GetMessage`** (the Win16 analogue of overkill's 1010:9B2E):
  `Win16System.next_message()` is the deterministic pump — posted msgs > WM_PAINT
  (dirty windows) > WM_TIMER (virtual clock jumps to earliest due timer). Timers:
  id2 @140ms = the gameplay tick, id1 @250ms, id3 @4000ms (intro).
- USER/GDI object model landed (`win16/api/objects.py`): HandleTable (recycling —
  DC churn exhausted 16 bits once), WndClass/Window/DC/Bitmap/Surface (RGB,
  3B/px)/Menu/AccelTable; `win16/callback.py` `call_far` = nested-interpreter
  callbacks INTO VM code (WndProc); WM_CREATE/SIZE/MOVE/DESTROY/PAINT/TIMER live.
- GDI: BitBlt (SRCCOPY/AND/PAINT/INVERT + BLACK/WHITENESS), PatBlt, text pipeline
  (SetBkMode/SetTextColor/GetTextMetrics 8×13 fixed + TextOut over the embedded
  public-domain font8x8 — presentation-only approximation), CreateCompatibleDC/
  Bitmap with real GDI default-object semantics (first SelectObject returns the
  default 1×1 bitmap handle, DCs pre-seed stock brush/pen/font — the game VERIFIES
  SelectObject returns, an error path caught this).
- **NAMETABLE (resource type 15) decoded** — the game loads bitmaps by NAME
  (PPINTRO, PPWALL1..10, PPBODY, PPHEAD*, PPICON1-4, KBCURSOR); the map lives in
  `NEExecutable.resource_name_map`, consumed by `lookup_resource`. All 26
  LoadBitmaps resolve (a spy probe caught them all returning 0 before this).
- wsprintf = CDECL varargs (raw handler + Win16 %-format engine). Two real bugs
  fixed: GDI draws must NOT dirty windows (WM_PAINT storm), handle recycling.
- dos_re framework grew (separate commits there): LEAVE (0xC9), CWD (0x99),
  three-operand IMUL (0x69/0x6B) — each with focused tests, 111 passed.
- Suite here: 13 passed (boot-to-idle gate: both windows alive, timers armed,
  meter has rendered pixels, 26 bitmaps resolved).
- **Next:** input driver (post WM_COMMAND "new game" + WM_KEYDOWN steering) →
  playfield renders → then the demo/lockstep machinery per the dos_re method.

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
