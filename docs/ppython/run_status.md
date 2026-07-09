# Paulie Python — run status (newest on top)

## 2026-07-09 — dos_re is now a real git submodule (was an undeclared sibling checkout)
- **Owner caught it:** win16_re silently required `D:\Games\DOS\dos_re` to exist on
  disk (hardcoded default in each package's `_env.py`) — an unversioned, invisible-
  to-git dependency.  Checking it, the local sibling checkout had actually drifted 3
  commits behind `origin/main` with nobody noticing (no data loss though: dos_re's
  PUSHA/POPA + interpreter-speedup work are confirmed ancestors of origin/main).
- **Fixed:** `git submodule add https://github.com/missingno7/dos_re.git dos_re`,
  pinned at `9c9247b`.  `ppython/_env.py` / `simant/_env.py` / `microman/_env.py` now
  default to the vendored path (`Path(__file__).parent.parent / "dos_re"`);
  `DOS_RE_PATH` is kept as an explicit opt-in escape hatch for co-developing dos_re
  against a separate working checkout (not the default).  `git clone
  --recurse-submodules` is now sufficient for a fresh checkout — verified end-to-end
  (fresh clone, gate green, only the expected assets-not-present skips, zero
  reference to the old sibling path).
- **Noticed but out of scope:** three doc cross-references (`docs/methodology.md`,
  `docs/pitfalls.md`, `docs/bringing_up_a_game.md`) point at `dos_re/docs/
  methodology.md` / `ai_porting_charter.md` / `pitfalls.md`, none of which exist in
  the current dos_re docs layout (it was reorganized into `architecture.md`,
  `hooks_and_verification.md`, `demos_and_snapshots.md`, `state_mirrors.md`,
  `glossary.md` — pre-existing drift, not introduced by the submodule move). Fixed
  the relative path prefix (`../../DOS/dos_re` → `../dos_re`) but left the target
  filenames as-is; a follow-up should re-map them to dos_re's current doc set.

## Standing mechanisms (check here before building new tooling)
- **Memory model: selector translation (4MB).** win16 lifts the 1MB real-mode ceiling
  via `dos_re` Memory's optional `sel_base` (selector→linear-base dict). The loaded
  program stays real-mode in low memory; GlobalAlloc blocks are selectors mapping into
  [0x140000, 4MB) managed by `win16/hugeheap.py` (small=1 selector; >64K=consecutive
  selectors 8 apart → contiguous 64K so `__AHINCR=8` huge-pointer walking is correct;
  linear+selector reclamation). `mem._xlat(seg,off)` resolves any far pointer (used by
  SetDIBitsToDevice/_lread for huge buffers). DOS path (sel_base None) is byte-identical
  — dos_re suite stays 116 green. To grow past 4MB, bump WIN16_MEM_SIZE in loader.py.
  Verified: microman now boots THROUGH startup (no more `LoadPage Error = 9` memory-
  exhaustion box) into its WAP title animation and paints a real frame; ppython
  (the RE target) unaffected. Interpreter overhead of the selector branch is ~4%.
  `test_microman_boots_and_renders` now gates on the first non-blank paint (it used to
  assume a startup crash-frontier, which the selector fix removed). Full suite 38 green.
- **win16 is now game-agnostic; multi-game testing.** `win16/app.py create_machine(exe,
  winflags)` boots ANY NE. `scripts/games.py` = the test-game registry (ppython is the
  RE target; microman/bangbang/kye/skifree are fixtures to harden win16).
  `scripts/boot.py <game> [steps]` = generic frontier probe. `ppython/runtime.py` is now
  a thin adapter. Ordinal names for KERNEL/USER/GDI/MMSYSTEM extended so ANY import
  fails loud WITH its name. **MICROMAN status:** boots ~1.7M instructions (full startup
  + file loads + game init) to the frontier GDI.360:CreatePalette (the 256-colour
  palette subsystem — CreatePalette/SelectPalette/RealizePalette/GetPaletteEntries/
  GetNearestPaletteIndex/SetDIBitsToDevice + MMSYSTEM.2 sndPlaySound are its open APIs).
- **Snapshot at an event:** `play.py --snapshot-on-box Collision` saves an INSPECTION
  snapshot whenever a MessageBox whose caption/text matches appears (the crash box is
  "Ughhh!"/"Collision!"). CPU is parked in the modal handler so memory+CPU+pixels are
  consistent (the crash frame is captured); NOT resumable (native modal stack not
  saved) — inspect with `win16.vmsnap.load_snapshot`, use demos for reproducible
  replay. F9 still takes a resumable boundary snapshot during normal play. Before any
  modal blocks the GUI, `_flush_windows` force-renders the latest frame (else the
  version-gated renderer races and drops the final pre-modal frame, e.g. the crash).
- **Demos (record/replay):** `win16/demo.py` — the frame boundary is GetMessage, so a
  demo is the exact stream of returned messages + consumed dialog events (virtual-clock
  stamped). Record: `play.py --record FILE`, or set `services["demo_recorder"]`. Replay:
  `python scripts/replay.py FILE [--png DIR] [--snapshot DIR]`, or set
  `system.message_source = player.next_message` + `services["demo_player"]`. Replay is
  bit-exact (proven) and fails loud (`DemoDivergence`/`DemoEnded`) the instant the
  machine asks for input the demo doesn't have next. THIS IS THE VERIFICATION BASELINE
  every future hook/native replacement must reproduce.
- **Snapshots:** `win16/vmsnap.py` — `save_snapshot(machine, dir)` / `load_snapshot(dir,
  create_machine)`; three files (memory.bin, state.json, system.pickle). Must be taken at
  a message boundary (refuses if a modal dialog is open). `digest(machine)` = the
  game-observable fingerprint (memory + CPU + window surfaces + clock + timer intervals;
  the pump's internal timer_due schedule is deliberately excluded). In `play.py` press
  **F9** to snapshot (pauses the CPU at its next boundary first).
- **Console-first errors:** `play.py` prints every VM stop to stderr with CS:IP,
  instruction count, traceback, last trace lines and API call log — the window only shows
  a red "see console" banner. MessageBoxes are echoed to stdout. Built for AI operation.
- **Dialog engine:** `win16/dialog.py` (DLGTEMPLATE parser) + `win16/api/dialogs.py`
  (DialogBox/EndDialog/Get-SetDlgItem*/SendDlgItemMessage/DlgDir* + Dialog/
  DialogControlState). DialogBox runs the game's real dialog proc in the VM in a
  modal loop (WM_INITDIALOG → control events → EndDialog); other windows' timers
  keep firing under it. Presentation via `services["dialog_ui"]` (the player builds
  real tkinter widgets from the template, du_to_px layout); headless leaves it None
  and auto-answers OK/Cancel. Control state (text/checked/items/sel) lives in the
  DialogControlState objects — the single source of truth the widgets mirror.
  Window-like handles (dialogs, controls) resolve through `geom_px()` so geometry
  APIs treat them uniformly.
- **Interactive player:** `python scripts/play.py [--speed N] [--scale N]` — **each
  Win16 window is its own real tkinter window** (WindowView per handle; created/
  destroyed as the game creates/destroys windows). The Paulie Python window carries
  the game's real menu bar (from the MENU resource via `win16/menu.py`), with
  **live grayed/checked sync** from the game's EnableMenuItem/CheckMenuItem state —
  disabled items are unclickable, exactly like real USER (delivering WM_COMMAND for
  a grayed item crashes the game: Pause = idiv-by-zero). Game MessageBoxes appear
  as real modal boxes (`services["messagebox_ui"]`, CPU thread blocks until
  dismissed). DialogBox/WinHelp are logged-and-skipped stopgaps
  (`services["skipped_ui"]`, shown in the status bar) until the dialog engine
  lands. VM death shows a red banner, never silence. Pacing:
  `win16/interactive.py` installed as `Win16System.message_source`; headless
  replay leaves it None (auto-OK MessageBoxes, deterministic clock).
- **Interpreter speed:** ~300k instr/s standalone; a gameplay frame is heavy, so play
  is choppy (a few fps) — cProfile confirms it's ALL VM stepping (execute_opcode/
  fetch8/rb), NOT the Python GDI. Real fix = the dos_re method (hook hot routines →
  native). Boot to windows is ~6400 instr (instant); the main window is legitimately
  blank until New Game.
- **Gameplay gate:** `tests/test_gameplay.py` — boot→idle→WM_COMMAND(1050 New Game)→
  level intro msgbox + painted playfield + SOUND notes. The msgbox/sound logs are
  `api.services["messagebox_log"|"sound_log"]` (virtual-clock-stamped evidence).
- **Menu commands** (from the MENU resource): 1050 New(F2), 1100 Sound(F3),
  1150 Pause(F4), 1175 HighScores(F5), 1200 Exit(F10); attitudes 2151-2155
  (default 2153 Diamondback); control 2201 kbd / 2202 mouse; screen-set 2051-2053.

## 2026-07-09 — Fixes: snapshot pickle crash + sim-tick overrun (GetTickCount overshoot) (8dc8b50)
- **F9 snapshot crashed** ("cannot pickle _thread.RLock"): the driver sets
  input_drainer + yield_check (bound methods holding a Condition) on sysobj, and
  save_snapshot only detached machine + message_source.  Now detaches all four.
- **"Quick Game starts but never progresses" → CallbackOverrun (20M steps).** The
  sim tick paces each frame by spinning on GetTickCount; GetTickCount used
  max(clock_ms, instr_floor), and the interpreter runs ~2× faster than
  INSTR_PER_MS, so the floor overshot real time (~41s at 21s wall).  Inside the
  nested tick the game then thought thousands of frames were due → processed them
  all → overrun.  New Win16System.tick_count(): interactive hosts track the wall
  clock (kept current inside a callback by check_pause on 8192-step chunks),
  headless keeps the instruction floor (busy-waits still elapse; demo replay
  deterministic).  USER.13 + the WM_TIMER clock + TimerProc dwtime all use it.
- **NOT verified in-game by me** (headless reach is too slow); owner must confirm
  the ants now move.  Owner's "windows still flat / not resizable" report is a
  VERSION lag — the panel/resize/scrollbar work (4c7fdec/ad4da75) is newer than
  the yield_check commit their RLock came from; needs `git pull` + `git submodule
  update --init` (the FPU fix lives in the dos_re submodule).

## 2026-07-09 — FPU: SimAnt uses native inline x87; completed the emulator (was the ant-stall)
- **Do we have FPU? Yes, but it was incomplete.** SimAnt's FP is NOT INT 34-3D —
  disassembling the 111 OSFIXUP sites shows `fwait; es: <x87>` (native inline x87,
  D8-DF), which dos_re's execute_fpu runs (segment overrides included).  0 FP hits
  during boot/title; all FP is in the sim (routines __ftol/__fassign/__STRINGTOD
  + _BuildAntListA).  Statically enumerated the ~20 distinct x87 instructions from
  the fixup sites — execute_fpu implemented only ~1/3, so the ant physics hit
  UnsupportedInstruction and stalled (almost certainly why the ants don't move).
- **Fix (dos_re 47418f1; win16_re submodule bump 0e8708f):** added the missing
  register forms (D8/DC arithmetic, FXCH, FCHS/FABS/FTST/FXAM, FLD1/FLDZ/FLDPI...,
  FSQRT/FRNDINT/FSCALE/FSIN/FCOS, FNSTSW AX) and memory forms (single-precision
  m32 FLD/FST/FSTP, integer FIST/FISTP m32 + FILD/FIST/FISTP m16, D8/DC m32/m64
  arithmetic) + _fxam class/sign bits.  Verified sqrt(2)/FCHS/FXCH/FTST/FXAM/m32
  round-trip; dos_re suite + new test_core x87 case green; win16 gate 47 green.
- **Progression: NOT yet visually confirmed by me** — reaching in-game headlessly
  keeps timing out (~150s to reach + heavy nested sim frames).  The FPU gap was
  the strong suspect for "ants not moving"; with x87 complete the sim should
  advance.  Owner to confirm in play.py (new game → do the ants move?).  If a
  further x87 opcode is hit it fails loud ("x87 opcode XX /Y at CS:IP") — easy add.
- **Note:** dos_re edits must target the SUBMODULE path (win16_re/dos_re), commit
  there + push to the dos_re remote, then bump the pointer in win16_re.

## 2026-07-09 — In-game windows are real: title/close/resize/maximize/scrollbars (play.py)
- **Captured the in-game window styles** (log via a CreateWindow hook): the panels
  are WS_CHILD|WS_CAPTION created with a NULL parent (top-level framed windows):
  "Caste Control"/"Behavior Control"/"Black Nest View" = CAPTION|SYSMENU
  (titled/closable/fixed); "SimAnt - Quick Game" = CAPTION|SYSMENU|THICKFRAME|
  MAXBOX|VSCROLL|HSCROLL (titled/closable/resizable/maximizable/scrollable) —
  exactly the owner's "some closable-only, some resizable" description.
- **compositor.top_level_windows now selects parent==0** (desktop-parented), so
  these WS_CHILD panels are presented as their OWN tkinter Toplevels instead of
  being hidden by the old `not is_child` filter.  (ad4da75 chain: 4c7fdec+ad4da75.)
- **WindowView maps Win16 styles → native tkinter chrome:** WS_THICKFRAME →
  resizable (+ WM_SIZE on drag, debounced, so the game re-lays-out; the main
  AntRoot frame = WS_OVERLAPPEDWINDOW ⇒ now resizable); WS_SYSMENU → close box
  (posts WM_CLOSE to that window; main still quits); WS_H/VSCROLL → scroll bars
  (post WM_H/VSCROLL, thumb driven from the tracked scroll range).
- **NOT visually verified by me** — headless in-game render times out (reaching
  it is ~150s and each frame does heavy nested work), so this is built from the
  captured styles + standard tkinter and boots clean (gate 47), but the owner
  must confirm the in-game look/behaviour.  Risks: WM_SIZE relayout fidelity,
  panel positioning, scroll page-size (approximated).

## 2026-07-09 — In-game unfreeze: USER.236 + callback runs via cpu.run (faster, pausable)
- **USER.236 GetCapture** (c1efb66) — the mouse-capture poll; an unimplemented gap
  stopped the VM once in-game.
- **call_far rewrite** (c1efb66).  SimAnt's entire in-game runs inside the ~59fps
  TimerProc callback; call_far drove it one instruction at a time in Python, so
  in-game was slow AND un-pausable (the worker's check_pause never ran → window
  "frozen", F9 timed out).  Now a permanent replacement-hook at the sentinel
  CS:IP raises to signal return, so the callback body runs via cpu.run()'s tight
  loop; a yield_check between 64K-step chunks (driver → check_pause) keeps it
  pausable.  Verified byte-exact (demo replay + snapshot roundtrip green).
- **Note on headless timing:** an outer cpu.run(N) step that dispatches a
  WM_TIMER runs a WHOLE nested sim frame that doesn't count toward N, so a
  head­less "chunk" post-Quick-Game does enormous work — in-game is impractical
  to measure/drive headlessly (reaching it is ~43M + huge frames).  Interactive
  (paced) play is the loop; the owner drives it.
- **Still open (owner's asks):** real resizable/closable/maximizable CHILD windows
  and main-frame resize.  Needs the in-game window styles (couldn't capture
  headlessly — too slow) + likely a separate tkinter Toplevel per captioned child
  (native chrome) OR full non-client modelling.  Get the styles from the owner /
  a winevdm run before building it.

## 2026-07-09 — GUI chrome: native dropdown menu + framed child windows (play.py)
- **Menu bar is a real native dropdown now** (a55a559).  It was a painted, dead
  in-client strip because play.py only built a tkinter menubar from a MENU
  *resource* (gated on wndclass.menu_name); SimAnt builds its menu at runtime
  (CreateMenu/AppendMenu→SetMenu into win.menu_obj).  WindowView now builds the
  native bar from menu_obj (cascades + WM_COMMAND), (re)building in sync() when
  SetMenu lands.  compositor.composite gained `menu_bar=False` so the host drops
  the painted strip; the strip stays for headless screenshots.
- **Framed child windows get a Win3.1 window frame** (4b88b18).  composite()
  paints a raised 3D frame around any child with WS_BORDER/WS_DLGFRAME/WS_CAPTION
  (verified: SELECT-A-GAME dialog now framed, borderless ribbon not).
- **ROOT GAP for the rest — no non-client area modelling.**  The owner's other
  asks (caption TITLE BARS on WS_CAPTION windows, SCROLLBARS on WS_HSCROLL/
  VSCROLL, true frame RESIZE on WS_THICKFRAME) all need client-rect ≠ window-rect
  with insets (caption/border/scrollbar/menu).  Today client==window everywhere
  (CreateWindow/GetClientRect/GetWindowRect/ClientToScreen/compositor all assume
  it), which is also why the frame above overlays the outer ~2px of client.
  Modelling non-client is the right next step but foundational + touches the
  click-coordinate path we just got working — do it WITH interactive testing,
  not blind.  SimAnt child styles seen: ribbon/root = plain WS_CHILD (borderless,
  correct); dialogs = WS_CHILD|WS_DLGFRAME (now framed).

## 2026-07-09 — SimAnt INTERACTIVE: clicks work, sim-tick timer runs, Quick Game plays in play.py
- **Clicks now register in play.py.** The WAP steers by POLLING GetCursorPos +
  GetKeyState(VK_LBUTTON), which our pump never fed from mouse messages (only
  keyboard).  `Win16System._note_input` now derives cursor_pos (client→screen via
  the parent chain) + button VKs from WM_LBUTTON*/mouse messages, on BOTH
  GetMessage and PeekMessage(PM_REMOVE).  The interactive driver exposes an
  `input_drainer` so a PeekMessage busy-poll (the menus never call GetMessage)
  sees freshly-posted input.  play.py `_on_mouse` subtracts the presentation
  menu-bar strip so coords match the game's client space.
- **SetTimer TimerProc = the ~59fps sim tick (SetTimer(0x118, 0, 17, 0100:2440
  MYTIMERFUNC)).** Was NotImplementedError ("crash on cold start" once in-game).
  Now the proc is stored; its WM_TIMER carries the proc in lParam; DispatchMessage
  calls TimerProc(hwnd,WM_TIMER,id,dwTime), NOT the wndproc (whose WM_TIMER hangs).
- **WM_TIMER discoverable by PeekMessage** — the tick paces frames by spinning on
  `PeekMessage(..,WM_TIMER,WM_TIMER,PM_REMOVE)`; we only synthesized timers in
  GetMessage, so that spin was infinite (→ callback watchdog).  peek_message now
  returns a due WM_TIMER (clock = max(message clock, instruction floor)).
  INSTR_PER_MS moved to system.py (shared by GetTickCount + the timer clock).
- **USER.30 WindowFromPoint** — deepest visible window containing a screen point;
  the click hit-test the main wndproc calls after ClientToScreen.
- **PERF FOLLOW-UP:** in-game frames run inside TimerProc → call_far's per-step
  loop (checks the return sentinel each step, ~1.5-2× slower than cpu.run), so
  gameplay is slower than boot.  Worth speeding call_far (compare a packed int
  instead of building a (cs,ip) tuple each step; or a sentinel-hook + cpu.run).
- Commit 43695f9.  Default gate 47 green; demo-replay determinism + snapshot
  roundtrip green (message-derived input state stays deterministic).

## 2026-07-08 — MILESTONE: SimAnt reaches IN-GAME (Quick Game) + snapshot-anywhere
- **In-game reached.** Start -> "Select a Game" -> Quick Game now renders live
  gameplay: the nest view with a dug ant tunnel, the surface panel, the caste
  slider ("Soldiers 40%"), live palette readouts.  ~44.8M instructions in.
- **The click recipe** (important — WAP polls the mouse, it does NOT use the
  WM_LBUTTONDOWN lParam): set `services['cursor_pos']` to the object's SCREEN
  coords and hold VK_LBUTTON (`services['async_keys']={0x01}`) across a few
  frames, then release + post WM_LBUTTONUP.  Quick Game = screen (337,186)
  (dialog 0x13c at frame (138,116) + object center (199,70)).  Open Select-a-Game
  first by clicking body window 0x118 at (250,150).  Driver: scratchpad
  `to_ingame.py`.
- **API frontier closed** (each ID confirmed against winevdm .spec): USER.156 was
  mis-registered GetSubMenu -> it is **GetSystemMenu** (GetSubMenu=159); added
  RemoveMenu(412)/DeleteMenu(413)/GetMenuItemCount(263), SetWindowText(37),
  GetScrollPos(63), ScrollWindow(61), EqualRect(244), InvalidateRgn(126)/
  ValidateRgn(128); GDI SaveDC(30)/RestoreDC(39)/IntersectClipRect(22); INT 2Fh
  serviced as unhandled-multiplex.  dos_re gained **PUSHA/POPA** (0x60/0x61).
- **Next frontier: SetTimer-with-TimerProc** (the sim-tick callback) — needed for
  the ant simulation to animate.  Requires calling a VM callback on each timer.
- **Snapshot anywhere** (owner ask): F9 used to time out on the menus/in-game
  because it only parked at a GetMessage boundary, but those loops busy-poll
  PeekMessage.  The vmsnap machinery already round-trips from ANY instruction
  boundary (proven bit-exact in-game: digest+instr+CS:IP match after restore);
  only the interactive PAUSE was the limit.  Fix: `InteractiveDriver.check_pause()`
  called between 4096-instr chunks parks the CPU thread at an instruction
  boundary too.  Modal DialogBox/MessageBox stay inspection-only (nested Python
  loop).  Commits: dos_re `c8f5cf8`, win16_re `79e0655` + `4a7f882`.

## 2026-07-08 — Performance: SimAnt is spin-bound, and a byte-exact +27% interpreter win
- **Window sizing** (owner ask): SimAnt is resolution-adaptive — it creates the
  AntRoot frame, calls `ShowWindow(SW_SHOWMAXIMIZED)`, then sizes RibbonWindow +
  the root panel from the MAXIMIZED client rect.  We ignored the maximize (only
  flipped visibility), so children were laid out to the 627-wide create rect, not
  the full 640-wide screen — why it looked smaller than otvdm (which mirrors the
  host desktop, hence its huge maximize).  Fixed: ShowWindow now grows a real
  top-level frame to SM_CXSCREEN×SM_CYSCREEN and re-fires WM_SIZE.  Host-window
  drag-resize (tkinter → WM_SIZE feedback) is a separate follow-on if wanted.
- **Where the time goes** (profiled, islands ON): NOT computation.  Over 5.8M
  steady-state instructions the game calls GetTickCount **64,890×** while the
  message clock advances **0 ms** — it is SPIN-WAITING on the 18.2 Hz frame timer
  (`_TickCount = GetTickCount()/55`).  The profiler's "hot routines" (`_win_Events`,
  `_win_IsWinOpen`, the 47xx cluster) are the pump/pacing spin.  `__aFuldiv` (the
  one pure math leaf) is already an island; there is **no clean pure-compute island
  left** to lift.  The one big game-code lever is the frame-pacing spin itself —
  which the bytecopy-island comment already flagged as deliberately left alone
  (accelerating it shifts the RNG-seeded worldgen).  Owner chose the safe lever:
- **Speed up the interpreter** (dos_re `fa7b97d`): cProfile showed ~all time in the
  CPU core (`execute_opcode`/`step`/`fetch8`/memory), only ~1.7% in our hooks.
  Four trace-off-hot-path changes, **byte-exact** (SimAnt DGROUP+regs at 9.84M
  instrs SHA-256-identical before/after; dos_re 118 + SimAnt 45 green): gate the
  debug disassembly f-strings on `trace_enabled` (never built in gameplay, ~+20%),
  inline fetch8's selector fast-path, hoist the hottest opcodes (Jcc/XCHG/MOV/INC-
  DEC) to the front of the if-ladder, and skip the hook-key tuple alloc when no
  hooks.  **249K → 317K instr/s (+27%)**, helping the spin AND all real work with
  zero worldgen risk.  Method note: a state-digest gate (hash DGROUP+regs at a
  fixed instr count) made the ladder reorder safe to verify — any slip => mismatch.

## 2026-07-08 — SOLVED: logo, ribbon buttons AND the SELECT-A-GAME dialog — two VM bugs, winevdm +relay as differential oracle
- **The winevdm oracle went from source-reading to EXECUTION.**  otvdm v0.9.0 runs on this
  Win11 box, so `WINEDEBUG=+relay otvdm SIMANTW.EXE` produced a 2.7M-line ground-truth API
  trace of the REAL game.  The trace is now a standing instrument: when an ordinal's
  identity or behaviour is in doubt, diff our call site against winevdm's relay log +
  its `.spec` files.  (Downloaded to scratchpad; the .spec ordinal maps confirmed every
  prior RE guess.)
- **BUG #1 — ordinal misidentification (GDI.181).**  We had 181 as GetRgnBox (which WRITES
  the region bbox into the caller's lpRect).  winevdm's `gdi.exe16.spec` + relay trace:
  **181 = RectInRegionOld → RectInRegion16**, a READ-ONLY hit-test; real GetRgnBox is
  GDI.134.  The bogus WRITE stamped the update-region box (0,0,W,H) over each WAP object's
  position rect every paint — SimAnt's ribbon buttons piled at (0,0); the logo's bottom
  half lost its +176 offset.  Fixed: 181 = real RectInRegion (reads the rect, returns
  intersect 0/1, never writes); added 134 = GetRgnBox alongside.  Title logo (full ant +
  complete SIMANT wordmark + all ribbon buttons) now pixel-correct.
- **BUG #2 — unsigned dest origin in SetDIBitsToDevice (GDI.443).**  Fixing 181 turned the
  SELECT-A-GAME dialog WHITE.  Not a region-cull (RectInRegion returned 1 for every dialog
  object; all 33 band blits fired) — the blits' DEST X was 0xFFFF.  The dialog paints at a
  client origin of (-1,-1); the 181-write had been silently clobbering that -1 to 0.  With
  the correct read-only 181, the real -1 reached GDI.443, whose handler sign-extended
  cx/cy/xs/ys but NOT the destination origin xd/yd — so -1 read as +65535 and every band
  landed fully off-surface.  Fixed: xd/yd are sign-extended too.  Dialog now renders fully.
- **Method note:** both bugs were latent, masked by a compensating bug.  The A/B that
  cracked it: hold everything else fixed, flip ONLY ordinal 181 between write-box and
  read-only, and diff the resulting blit DEST coords (0 vs 0xFFFF) — the discriminator was
  the coordinate, not the return value or the draw count.  Full SimAnt gate (45) + framework/
  microman (56) green; no other game regressed.

## 2026-07-08 — Real USER update-region semantics (winevdm as the API oracle)
- **Owner suggested mining winevdm (github.com/otya128/winevdm)** — the right call: it
  bundles Wine's 16-bit USER, giving authoritative semantics for exactly the APIs under
  suspicion.  Verified from its `user/window.c`: InvalidateRect16 = RedrawWindow(RDW_
  INVALIDATE [+RDW_ERASE]) — rects ACCUMULATE into an update region; GetUpdateRgn16
  copies that region out; BeginPaint validates (clears) it, erases ONLY when an erase is
  pending, and reports rcPaint = the update box.  **winevdm is now the standing API-
  semantics oracle: when a USER/GDI behaviour is in doubt, read its source, don't guess.**
- **Implemented faithfully** (win16/api/): `Window.update_rect` (accumulated union, client
  coords) + `update_erase` replace the info-destroying bool; InvalidateRect honours its
  lpRect + erase args; GetUpdateRgn copies the real region; BeginPaint erases only when
  asked, writes rcPaint = update box, and validates; every internal full-invalidate goes
  through the new `_invalidate()`.  Full suite green (incl. ppython + microman pixel A/Bs).
- **SimAnt logo/buttons: not yet healed by this** — [RESOLVED in the entry ABOVE this one].
  The "GetRgnBox dozens of times per band" observation was the tell: those calls were
  ordinal 181, which is NOT GetRgnBox — it is read-only RectInRegion.  The whole damage-
  stamp corruption was a single-ordinal misID, plus a masked signed-origin bug in GDI.443.

## 2026-07-08 — ROOT MECHANISM FOUND: WAP damage-stamp destroys object rects (logo + ribbon buttons)
- **One mechanism explains BOTH owner bugs** (logo halves overlapping at y=0 AND the ribbon
  buttons crawled into the top-left corner), exactly as the owner predicted.  Full chain,
  every step traced (not guessed):
  1. WAP object nodes (44 bytes each; rect at +0, visible flags at +0x24) are loaded RAW
     from WINGANT.DAT (one 1923-byte `_lread`) with CORRECT rects — the ribbon buttons are
     x=13/61/97/133, y=21 (a button row); the logo halves y=0..176/176..352.
  2. `_win_Recalc` (seg7:E6E2; WAP window id 0x2200 = the ribbon, per SetProp(278,·,0x2200))
     stamps 0x8000 into every rect (pass 1), then pass 2 RESTORES them correctly.  Fine.
  3. The paint path (fn returning to seg7:BC2B) creates a region, `GetUpdateRgn(hwnd,hrgn)`,
     stores hrgn in DGROUP `[CD84]`, and then — gated on `[CD84] != 0` — for EACH object
     node does `GetRgnBox(hrgn, &node.rect)`: the DAMAGE BOX (0,0,627,73 = full client)
     is written OVER the object's rect, object drawn, next node (~25K instrs apart).
  4. **The restore that must follow never happens** — watched to 7.5M instructions: the
     rects stay = damage box forever.  Objects then draw at rect.x1,y1 = (0,0): buttons
     pile at top-left; the logo bottom half loses its +176.
  5. `[CD84]` is set per paint cycle (seg2:3EE7) and cleared at cycle end (seg2:3F91) —
     the damage path is ACTIVE by design in SimAnt.  **microman (same WAP engine) renders
     correctly because it NEVER enters this path** — GetUpdateRgn/CreateRectRgn/GetRgnBox
     were first implemented today, for SimAnt; the engine gates on their availability.
- **Open question (the actual fix)**: what restores/repositions node rects after the
  damage-stamp on real Windows.  Candidates: seg7 fn 0E99:0F98 (runs only when a draw
  returns 0 — an update-queue helper?), the per-object draw fn (near call ~seg7:B6E0)
  possibly recomputing the rect from sprite strips, or a region API we answer differently
  (our GetUpdateRgn returns the FULL client rect whenever `win.dirty` — one bool — where
  real USER tracks an accumulating region that BeginPaint empties).  Next: statically
  reverse the stamping loop fn + the per-object draw path, and compare the node struct
  use against microman's working flow.
- **Real VM bug found + fixed along the way (dos_re)**: `Memory._notify_write` masked
  every watcher address with `& 0xFFFFF` (real-mode legacy), so write-watch traces of
  selector-space (>1MB) memory silently missed ALL hits — this hid the stamping writes
  for half the investigation.  Mask now applies only when `sel_base is None`; dos_re
  suite 118 green.

## 2026-07-08 — SimAnt logo: two halves overlap — deep WAP investigation (superseded above)
- **Symptom (owner):** the SIMANT title logo shows only its top half; the bottom half
  (legs + "© 1991 MAXIS") is drawn first then covered.  Confirmed via a blit trace of
  window 312: 43 SetDIBitsToDevice bands, `B166 B158 … B0` (bottom, source selector 8557)
  drawn FIRST, then `A160 … A0` (top, 854f) over it — both land in y=0..166.  Rendering
  each source buffer separately confirms 854f=top, 8557=bottom.  The bottom half's Y is
  short by exactly **176** (the top half's height): it should be at y=176..342.
- **Ruled out (each checked, not guessed):**
  - *Decompression* — the _Unpack LZSS island is byte-exact vs the ASM (136/136), so the
    asset bytes are correct.
  - *Huge-pointer / hugeheap mapping* — 854f and 8557 are SEPARATE GlobalAlloc blocks
    (8557 is a standalone 8939-byte block; they map 0x804 apart, not 64K), each rendering
    correctly on its own.  Not one >64K buffer wrapping wrong.
  - *Transparency* — the two halves occupy the SAME y band with different content, so a
    colour-key overlay would still mash them, not stack them.  They MUST be placed at
    different Y.
- **Localised to the WAP sprite-layout.** The logo is a WAP composite (`_win_DrawBitMap`
  seg7:BD5A, from `_ShowIntro`) that recursively draws ~32 child sprites, each passed
  position (0,0) (x=[bp+6], y=[bp+8] from a display-list node's `es:[si+2]`), so a
  sprite's on-screen Y is intrinsic to its own strip data; leaf blit is `0E99:10a2` ->
  the seg2 band-draw (SetDIBitsToDevice).  The bottom sprite's strips carry Y=0..166 where
  they should carry 176..342 — the +176 origin is lost in the WAP page-layout math (it did
  NOT surface as a fresh 0xA6 inside the leaf draw, so it is set further up, when the
  display list / strip Y is built).  Next lead: the WAP display-list construction.  Nothing
  committed — investigation only, tree clean.

## 2026-07-08 — Byte-copy island — load now ~35% faster; tile "blit" was a timing wait
- **Owner asked to island the "tile color blit" + tiles.**  Tracing corrected the profiler's
  (offset-based, cross-segment) symbol labels: the hot 24% at seg2:47xx labelled
  `_XferTileColor`/`_WaitedEnough` is NOT a blit — it is a **GetTickCount frame-pacing
  busy-wait** (`while (!WaitedEnough()) ;`, dividing ticks by 55 via __aFuldiv).  Left
  alone: accelerating it would shift the RNG stream (worldgen is seeded from GetTickCount),
  so it is not a clean lift.
- **The tiles ARE liftable**: seg2:3460 (`_FloorTiles` region) is a compiler-emitted far
  byte-memcpy — SI bytes, huge source ptr (@bp-8/-6) -> huge dest (@bp-12/-10), selector-
  wrapping — copying 960-byte tile rows (~9.5% of load).  New `bytecopy` island does the
  whole run as one linear block move (with an overlapping-forward smear fallback to stay
  exact).  Byte-exact unit gate (`test_bytecopy_island_matches_asm`) covers the real
  960-byte case, a 1-byte edge, dst-before-src, and an overlapping smear vs the ASM.
- **Payoff: ~35% faster to the title** (18.3s -> 11.8s) with all three islands
  (__aFuldiv + _Unpack + bytecopy); 19% from _Unpack alone.  Note this is a pure
  interpreter speedup (skipping slow *interpreted* loops) — a memcpy has nothing to
  "recover" for a native port, so it stays in hooks.py, not recovered/.
- **File I/O is NOT a bottleneck** (owner's other question): measured 0.0% of load —
  `_lread`/DOS-read are already native Python block-copies (`mem.data[lin:lin+n]=chunk`),
  not interpreted ASM.  Only interpreted CPU loops are worth islanding.

## 2026-07-08 — The LZSS decoder is now clean VM-free recovered code
- **The decompressor is lifted out of the hook into pure, VM-less recovered code**
  (`simant/recovered/lzss.py`) — the shape the source port targets: a plain
  `decompress(compressed, out_len) -> bytes` (and a resumable `decode_chunk`) with NO
  cpu / mem / hooks / offsets, behaving exactly like the original C `Unpack`.  It is
  the Okumura LZSS with its fingerprint constants named (N=4096, F=18, WINDOW_START=
  0x0FEE, THRESHOLD=2, space-filled window).
- **The island is now a thin ADAPTER** (`simant/hooks.py`): it reads the routine's
  DGROUP/stack state and drives `lzss.decode_chunk` over **memoryviews straight into VM
  memory** (source, the 4KB window, output) — zero copies — then writes back the ABI exit
  state.  Same byte-exact A/B gate (`test_unpack_island_is_byte_exact_vs_asm`, 136/136),
  same ~20% faster-to-title.  This is the standing "shadow -> verified hook -> pure system"
  progression: the interpreted game and a native port now share ONE decoder.
- **Pure unit tests** (`simant/tests/test_lzss.py`) exercise the recovered function with a
  round-trip encoder and the Okumura invariants — no VM needed, the form a native build
  uses.  Full suite green.

## 2026-07-08 — The _Unpack LZSS island lands — byte-exact, ~18% faster load
- **The asset-decompression bottleneck is now lifted.**  `simant/hooks.py` installs an
  island at seg7:A668 (`_Unpack`) that reimplements the Okumura LZSS decode in Python — a
  faithful 1:1 transliteration of the ASM (setup / literal / match / exit) so it produces
  the identical output, window, and exit state.  A mid-operation resume (entry [B7D4] != 0)
  passes through to the real routine (keeps the delicate two-sided-streaming resume path
  authoritative); every fresh call is fast-pathed.
- **Byte-exact, proven.**  The A/B gate (`test_unpack_island_is_byte_exact_vs_asm`) boots
  SimAnt with and without the island and requires the decompressed output + exit globals
  to match **call for call** — 136/136 identical in dev.  Getting there pinned three exact
  ABI details: the literal path leaves `dl` = the byte (so exit DX = last output byte); the
  `retf` does NO arg cleanup (caller does `add sp,6`); and the routine writes its stack
  frame (locals + pushed di/si/ds), which the island must replicate because SimAnt reads
  the freed scratch.  A full-memory A/B is deliberately NOT the gate: the game seeds
  `rand()` (seg4 `_rand`) from GetTickCount, which is instruction-count-based, so a faster
  load legitimately changes the RNG stream — that downstream divergence is the game's own
  timing sensitivity, not the island.
- **Payoff: ~18% faster to the title screen** (18.0s → 14.8s wall-clock to first title
  paint).  The instruction-count drop is only ~3% but wall-clock gains far exceed it: the
  island swaps thousands of *interpreted* ASM instructions per call for one native Python
  decode.  Further speedup is available by transliterating the resume path too (the ~40%
  of calls that stream mid-match still run the ASM) — logged as the next lift.

## 2026-07-08 — Load bottleneck located: the _Unpack LZSS asset decompressor
- **Owner: "loading is very slow — RE + hook the asset-loading island."**  PC-sampling
  the boot/load phase (`simant.probes.profile` with warmup=0) is unambiguous: **~90% of
  load time is one loop at seg7:A668 `_Unpack`** (the resolver mislabels it `_CenterAnt`
  — the offset-based symbol lookup collides across segments; the real routine has a
  `_Unpack` symbol at its head).  It is the **classic Okumura LZSS decompressor**:
  - 4KB sliding window (`and bx,0FFFh`), window **initialised with spaces (0x20)**, decode
    pointer **r0 = 0x0FEE = N−F = 4096−18** (the LZSS fingerprint), THRESHOLD=2, F=18.
  - Per step: `shr ax,1; test ah,1` pulls the next flag bit; bit=1 → literal (copy a
    source byte to output AND to `window[r+4]`); bit=0 → match (12-bit offset + 4-bit
    length back-reference from the window).  Flag byte reloaded as `c | 0xFF00`.
  - **Resumable/streaming**: state lives in DGROUP globals (`[B7C0]` window seg, `[B7C4:6]`
    src far ptr, `[B7C8]` input len, `[B7CA]` r, `[B7CC]` flag buffer, `[B7CE/D0]` match
    carry, `[B7D4]` mid-match flag); output far ptr + output len come on the stack
    (`[bp+6]`, `[bp+10]`).  Entry seg7:A668 (`push bp;mov bp,sp;sub sp,4;push di;push si`),
    exit `mov [B7C8],ax; pop di; pop bp; retf`.
- **A first-pass textbook Okumura decoder reproduces ~72% of the captured output** — close,
  but NOT byte-exact yet: the exact match offset/length bit-packing and the streaming
  call boundaries still need pinning (a decompressor that is 72% right silently corrupts
  every asset, so it is NOT shipped — the byte-exact bar holds).  **Next: build the island
  with an A/B gate** — run the ORIGINAL `_Unpack` and the Python island over the same
  compressed input and diff the full decompressed output + the DGROUP exit state, byte for
  byte, before trusting it (the microman/`__aFuldiv` island pattern).  Expected payoff:
  the whole load is dominated by this loop, so lifting it should cut load time sharply.

## 2026-07-08 — SimAnt hooking infrastructure + first island (__aFuldiv)
- **SimAnt is now the sole test target.**  `pytest.ini` scopes the default run to
  `simant/tests` + the game-AGNOSTIC framework tests SimAnt relies on (compositor,
  audio, hugeheap, localheap, msgbox).  ppython/microman tests are intentionally not
  collected (run `pytest microman/tests tests` for them); they may break without
  blocking SimAnt.  Default suite **35 green in <1s**.
- **Hooking infrastructure stood up, mirroring microman's** (the standing lifted-island
  method):
  - `simant/probes/profile.py` — PC-sampling profiler.  Buckets the CPU by
    (NE-segment, offset) across SimAnt's SIX code segments and names each hot bucket
    from the symbol file.  `python -m simant.probes.profile`.
  - `simant/probes/symbols.py` — reads the shipped **SIMANTW.SYM** (MAPSYM) to turn any
    `seg:offset` into the nearest routine name (flat nearest-preceding; approximate but
    dense).  This is what named `_StillDown`/`_DialogWaitInit` during USER.186 bring-up.
  - `simant/hooks.py` — signature-verified island registry + `install(machine)`; refuses
    to install on a prologue-byte mismatch.  `simant/runtime.install_hooks` wires it to
    the generic `scripts/games.install_game_hooks('simant', m)` and play.py `--hooks`.
- **First island: `__aFuldiv`** — the profiler's runaway #1 (~14% of steady-state
  samples): the Microsoft C far 32-bit UNSIGNED long-divide runtime helper, called
  constantly for the map/coordinate math.  Lifted to one exact Python `//`.  ABI nailed
  from a live trace (far, callee-cleans: `retf 8`; dividend/divisor on the stack;
  quotient in DX:AX; BX/SI/DI/BP preserved; CX clobbered).  Engages hard — **91,628
  fires over 6M steady-state steps**, game still paints, no crash.
- **The A/B oracle gate** (`simant/tests/test_hooks.py`): runs the ORIGINAL ASM routine
  and the island over 14 input pairs (both code paths) and requires an identical
  register RESULT.  Scoped to the ABI contract (result + preserved regs + `retf` unwind),
  NOT the caller-clobbered CX scratch — on the full-32-bit path the ASM leaves an
  algorithm-internal intermediate in CX that no caller observes and that only the loop
  the island skips could reproduce.  Next islands: `_CenterAnt`, the `__ftol`/`__aFldiv`
  siblings, and the `_XferTileColor`/`_FloorTiles` render loops the profiler ranks next.

## 2026-07-08 — SimAnt reaches its SELECT-A-GAME menu (title dismiss + window enum + 1bpp)
- **SimAnt now boots -> title -> (click) -> the "SELECT A GAME" menu**, fully rendered
  (Tutorial/Quick/Full/Experimental/Load Game icons + CANCEL), ribbon correct throughout,
  ~41M instructions, no gap.  Owner playtest drove past the title and hit new frontiers;
  each resolved from its call site + `SIMANTW.SYM`:
  - **USER window enumeration**: GetTopWindow(229, wrapped by the app's `_MyGetTopWindow`),
    GetNextWindow(230), GetWindow(262) — SimAnt walks a parent's children (close/redraw)
    with GetTopWindow + GW_HWNDNEXT.  Shared `_get_window`/`_z_children` helpers: our
    window list is draw order (last = topmost), so top-to-bottom Z-order is the reverse.
    Pinned by `tests/test_window_enum.py`.
  - **1bpp (monochrome) DIBs** in SetDIBitsToDevice (8 px/byte, MSB = leftmost) — the
    SELECT-A-GAME dialog's mono glyphs/masks.  Joins the existing 4/8bpp paths.
- The owner also reported the ribbon buttons "in the top-left" and the title logo drawing
  half — but the composited render (exactly what play.py shows via `compositor.composite`)
  is correct at every stage checked (ribbon buttons in place, full logo), so this looks
  already-resolved by the window/compositor work or was a transient first-frame/real-time
  artifact; flagged to re-verify in live play.  Next: wire a game-mode pick (Quick Game)
  into the actual simulation screen, then the x87 `fpu.py` the sim needs.

## 2026-07-08 — SimAnt runs its full multi-window UI (title + ribbon), no gaps
- **SimAnt now boots clean through startup into its running main loop and paints
  its "windows within a window" UI** — no API gap, no crash, for 20M+ instructions.
  Driven past the splash by the fail-loud frontier loop; each API identified from its
  call site (args sniffed off the stack, strings read from DGROUP, callers named via
  `SIMANTW.SYM`), not guessed.  Rendered: the **SIMANT title logo** (GenericWindow
  522x352 child) and the **game ribbon** (RibbonWindow 627x73: Yard/Nest/Surface tabs,
  tool buttons, bookmarks 1-7, YELLOW/BLACK/RED colony health bars) composited over the
  AntRoot frame.  APIs added (all game-agnostic, in `win16/`):
  - **USER**: SetWindowPos(232, honours SWP_NOMOVE/NOSIZE/SHOW/HIDE — sizes the child
    panels), IsWindowVisible(49), BringWindowToTop(45), PeekMessage(109, non-blocking
    filtered queue scan via new `Win16System.peek_message` — SimAnt's main loop peeks
    mouse 0x200-0x209 PM_REMOVE), GetUpdateRgn(237, fills a region with the window's
    update area).  USER.186 is an *unconfirmed* 1-word input gate at the head of the
    `_StillDown` helper (over-popping it as 2 words corrupted the return address and
    jumped into zeroed memory — the arg count matters); returns TRUE so the real
    still-down decision is delegated to GetAsyncKeyState (USER.249, already native).
  - **GDI**: CreateRectRgn(64) + GetRgnBox(181) on a new bounding-box `Region` object
    (DeleteObject frees it; non-rect combines would degrade to the bbox).
  - **KERNEL**: GetSystemDirectory(135), GetProfileInt(57)/GetProfileString(58) over
    WIN.INI (absent -> default; SimAnt reads `[SimAnt] autotrack=` at startup).
- The ordinal-neighbourhood self-checks held (confirmed USER.49=IsWindowVisible +
  USER.50=FindWindow anchor 45=BringWindowToTop; USER.249=GetAsyncKeyState anchors the
  key polling).  Full suite still green.  Next: confirm USER.186's true name; drive the
  title/ribbon into the actual game screen (menu picks, the ant map in AntRoot), and the
  x87 `fpu.py` frontier the simulation will need.

## 2026-07-08 — SimAnt boots + paints (the big stress target) + project renamed win16_re
- The repo is now **win16_re** (generic Win16 RE framework, `README.md` added); paths are
  all relative so the rename was transparent.  New `simant/` package (runtime + boot test),
  registered `simant` in `scripts/games.py`.
- **SIMANTW.EXE (Maxis SimAnt) boots through startup and paints its MAXIS splash** — a full
  commercial Win16 app (6 code segs, KEYBOARD+WIN87EM, raw INT 21h I/O, programmatic menus,
  16-colour DIBs).  Brought up by the fail-loud frontier loop; ~1k → 3.36M → running once the
  4bpp blit landed.  APIs/services added (each identified from its call site, not guessed):
  - **loader**: INT 21h now routes to the KERNEL DOS service table (apps call DOS raw).
  - **USER**: FindWindow(50, single-instance guard), Get/Set/RemoveProp(24/25/26, window
    property store on `Window.props`), UpdateWindow(124), FillRect(81), and the **programmatic
    menu builder** — CreateMenu/DestroyMenu/AppendMenu/InsertMenu/GetSubMenu/SetMenu on a new
    `Menu.items`/`MenuItem` model (SimAnt builds menus in code, not from a resource).
  - **GDI**: Escape(38, QUERYESCSUPPORT→0), CreateFont(56, new `Font` object → fixed-cell
    metrics), GetTextExtent(91), AddFontResource(119), UnrealizeObject(150), SetMapperFlags(349),
    and **4bpp (16-colour) DIBs** in SetDIBitsToDevice (nibble-unpack in the vectorized path).
  - **KERNEL**: lstrcat(89)/lstrlen(90), GlobalReAlloc(16, alloc+copy+free), GlobalFlags(22),
    GlobalCompact(25), GlobalLRUNewest/Oldest(163/164), GetFreeSpace(169) — plus huge-heap
    `free_bytes`/`largest_free_block`.
  - **DOS (INT 21h)**: get-drive(19h), create(3Ch), open(3Dh), get/set-attr(43h), IOCTL(44h,
    isatty), get-cwd(47h).
  - System-metrics table filled out (icon/cursor/scroll/dbl-click sizes).
- SimAnt more than doubled the win16 surface; every change is game-agnostic (lives in `win16/`)
  and the fixtures still pass — full suite **50 green**.  Next: drive past the splash into the
  menu/first screen (KEYBOARD imports + x87 `fpu.py` are the likely upcoming frontiers).

## 2026-07-07 — microman package + MessageBox Yes/No + snapshot game-name + 2 more islands
- **MessageBox button sets** (owner: Restart gave only OK, treated as No).  `win16/
  msgbox.py` maps `mtype & 0x0F` to the real button set + IDs (MB_OK/OKCANCEL/
  ABORTRETRYIGNORE/YESNOCANCEL/YESNO/RETRYCANCEL → IDOK..IDNO).  The API returns the
  DEFAULT (affirmative) headless (was always IDOK=1, which the game read as "not Yes");
  play.py's modal renders the actual buttons and reports the chosen ID.  microman's
  Restart is MB_YESNO|ICONQUESTION (0x24) → Yes/No returning 6/7.  Pinned:
  `tests/test_msgbox.py`.
- **microman is now its own package** (mirrors ppython/): `microman/` = `_env`,
  `runtime` (EXE path, winflags, create_machine, install_hooks, GAME_NAME), `hooks`
  (moved from gamehooks/), `recovered/`, `probes/`, `tests/`.  gamehooks/ retired; the
  generic loader is `scripts/games.install_game_hooks(name, machine)` → imports
  `<name>.runtime.install_hooks`.  Every game-specific test moved under
  `microman/tests/`.
- **Snapshots carry the game name** (format v3: `game` field).  `play.py --resume DIR`
  now works WITHOUT `--game` — it reads the game from the snapshot (falls back to
  matching the recorded EXE name for pre-v3 snapshots).  `win16.vmsnap.snapshot_game`.
- **Two more lifted islands** (owner: profile the snapshot, hook the costliest).  Fine
  PC-sampling of gameplay from snap_220905 found two unhooked huge-pointer byte loops
  the earlier fill/copy signatures missed (different frame layout, matched
  STRUCTURALLY now, reading the frame offsets from the code):
  - `wap_byte_fill` (huge-ptr memset, value/dst walk 1 byte/iter) — the hottest idle
    loop; fires ~24k times in the title alone.  **7.1 → 8.6 fps (+21%)** idle.
  - `wap_byte_copy` (huge-ptr memcpy) — the opaque sprite-row draw; ~13k fires under
    input, −26% instructions during action.
  Both verified byte-exact by the A/B pixel gate (now asserts EACH of the 5 island
  families fires).  19 islands total.  Remaining gameplay hot spot: the `6E` sprite
  decoder (per-pixel clip + transparency branches) — not a single slice, the harder
  next target.

## 2026-07-07 — snapshot resume from play.py + SND_MEMORY SFX + islands scan-all
- **Resume a session from a snapshot** (owner asked, to profile gameplay itself):
  `play.py --resume <snap_dir>` boots straight from an F9 snapshot instead of cold.
  Two selector-era fixes were needed: (1) `load_snapshot` re-wires the VM Memory's
  `sel_base`/`sel_min` to the RESTORED huge heap (the pickle copied the dict, so the
  fresh boot's empty map would leave every global selector unmapped → instant
  divergence); (2) the InteractiveDriver seeds its wall-clock epoch from the restored
  `clock_ms`, else every armed timer sits `clock_ms` ms in the future and the game
  looks frozen for ~45 s.  Snapshot format v2 also carries the polled key state
  (`async_keys`).  Gate: `tests/test_microman_snapshot.py` (bit-exact resume, plain
  AND hooked).
- **SFX now audible**: microman plays fire/hit sounds via `sndPlaySound(ptr,
  SND_MEMORY)` — a RIFF/WAV image it builds in a global buffer (NOT a disk file; only
  the looping title music is MICROMAN.WAV).  The SND_MEMORY branch was log-only; now
  `_read_wav_image` copies the blob out by its RIFF size and hands it to the backend.
  SquareWaveBackend separates looping MUSIC (replace-on-new) from one-shot SFX (mix on
  any of 16 channels, decoded-Sound cache so a rapid-fire SFX decodes once, live-ref
  ring so pygame doesn't GC a still-playing one-shot).  Pinned by
  `tests/test_sndplaysound.py`.  Owner sound bug fixed.
- **Islands scan-all**: `gamehooks/microman.py` now signature-scans the code segment
  for every clone of the WAP loop bodies (ascending fill, descending fill, dword copy)
  instead of two hand-picked addresses — 17 clones hooked.  Gameplay from the level-1
  snapshot: 4.1→7.1 fps (the fill loops appear at 8 more sites used by sprite draw).
  Remaining gameplay hot spots (post-hook resample): seg2:6Axx 18%, 6Exx 11%, 72xx
  10% — the WAP sprite compositor's per-pixel plotting; next islands.

## 2026-07-07 — GAME-SIDE HOOKS PROVEN: the WAP lifted islands (per-game, oracle-gated)
- **The dos_re method now works on win16 games.**  New `gamehooks/` package: per-game
  hook modules (`gamehooks/<name>.py`, `install(machine)`), kept OUT of the
  game-agnostic win16 layer; play.py installs them by game name (`--no-hooks` runs
  pure ASM).  Each module verifies code-byte signatures at its hook addresses and
  refuses to install on mismatch.
- `gamehooks/microman.py` lifts the two sampled WAP inner loops as ISLANDS (hook at
  the loop head, do all iterations in one Python slice op, write back the exact final
  register/flag/locals state, jump to the loop exit):
  - `wap_rle_fill` (seg2:8D70→8DB2): RLE run fill, one byte + full selector recompute
    per iteration in ASM → one descending-span slice fill.
  - `wap_huge_copy` (seg2:926C→9299): huge-pointer dword copy (selector+=8 on wrap) →
    one linear slice copy (with forward-overlap propagation preserved).
  Semantics derived from live traces (artifacts/loop_tr.txt); both fire ONLY in the
  WAP page-transition animations (boot LoadPage uses sibling loop copies — the other
  two fill-loop clones at seg2:8CC0/8D2C are future islands if they ever sample hot).
- **The gate** (`tests/test_microman_hooks.py`): a hooked and an unhooked machine run
  the same 20-batch deterministic boot; window pixels must be sha256-IDENTICAL at
  every checkpoint, and the hooked run must use materially fewer instructions.
  Result: pixel-exact, 30.1M→22.1M instructions (-26%), wall 77.5s→58.8s for the
  window covering the first transition.  42 tests green.
- **SimAnt rehearsal note**: the pipeline is now end-to-end — PC-sample (wrap
  CPU.step) → trace the hot loop live (cpu.trace at the loop head) → lift as an
  island → A/B pixel oracle.  Same steps apply to any future game's hot engine.

## 2026-07-07 — perf split VM-side/game-side; WAV out; keyboard fixed; hook targets named
- **Owner asked where the bottleneck is.**  Measured: the game requests a 40ms timer
  (25fps) but received 3.9 ticks/s — 6x slow, and the driver drops missed ticks, so
  game TIME dilates (the "386 feel").  cProfile split the cost:
  - **VM side (fixed, 2.6x)**: SetDIBitsToDevice was 63% — LUT rebuilt with 256 mem.rw
    per blit + per-pixel Python.  Now: LUT cached on (table bytes, palette identity),
    blit fully numpy-vectorized (analytic clip both axes; ~4 array ops per blit).
    1500-step window 1.457s→0.554s.
  - **Game side (the next lever, ~52% of what remains)**: PC-sampling (wrap CPU.step,
    sample CS:IP every 64 instr) found WAP's two inner loops in seg 2 (CS 0852):
    `0852:8D70-8DAF` = 37% — a huge-buffer FILL storing ONE byte per iteration with
    full selector recompute (shl dx,3 / add / mov es / stosb-like) ≈ 25 interpreted
    instr per byte; `0852:9260-929F` = 15% — the classic huge-pointer MEMCPY
    (4 bytes/iter, offset+=4 / jnc / selector+=8).  Both are single memoryview/numpy
    slice ops in our linear memory model → hook the enclosing functions (find entries,
    replace, oracle-verify frame pixels over a demo) — the dos_re method, and the
    rehearsal for the SimAnt endgame.
- **WAV audio**: sndPlaySound now plays through the host (SquareWaveBackend.play_wav
  via pygame.mixer; SND_LOOP honoured; NULL=stop; sound_log stays authoritative;
  SND_MEMORY log-only until proven).  microman's title WAV (32KB) confirmed delivered.
- **Keyboard fixed**: GetAsyncKeyState read services["async_keys"] which NOTHING fed —
  microman steers by POLLING (not WM_KEYDOWN), so arrows were dead.  Key state is now
  derived from the message stream in get_message (demo-replay identical), with the
  real API's bit-0 went-down-since-last-poll latch for taps.

## 2026-07-07 — MICROMAN pixel-correct: the palette chain root-caused (3 fixes)
- The owner's playtest still showed `LoadPage Error = 9` + wrong colours.  A full-API
  ring-buffer trace dumped at the moment the game called MessageBox found the real
  chain (three defects hiding behind one symptom):
  1. **SelectPalette returned 0 on a fresh DC** (`dc.palette is None` → "prev = 0").
     Real GDI has the stock DEFAULT_PALETTE selected, so success never returns 0 —
     WAP treats 0 as failure and aborts LoadPage with error 9, so its page BMPs
     (MICROMAN.PG1/PG2 — plain 8bpp BMP files) never loaded and every page rendered
     from an uninitialised buffer.  Fix: report/accept the stock handle.
  2. **DIB_PAL_COLORS decode**: with pages actually loading, SetDIBitsToDevice gets a
     16-bit WORD-index table into the DC's logical palette (identity 0..255), NOT an
     RGBQUAD table.  The old "RGBQUAD despite fuColorUse=1" pin was an artifact of
     observing blits only while LoadPage was failing.  Both modes now implemented +
     pinned (`test_dib_render.py`: 3 tests incl. fail-loud PAL_COLORS-without-palette).
  3. **GetSystemPaletteEntries returned a grayscale ramp** (stub).  WAP builds its blit
     table by nearest-matching the SYSTEM palette into its logical palette, so the ramp
     collapsed every page to grays.  Now RealizePalette copies the realized logical
     palette into `Win16System.system_palette` (static single-app display model — no
     other app competes for slots) and GetSystemPaletteEntries reports it (R,G,B order).
- Verified against the owner's real-Windows screenshot: the info page (gray bg, magenta
  contact text, yellow "Press SPACE-BAR to Play!", colour photo) and the DEMO playfield
  (green circuit bg, red sprite) match.  `messagebox_log` empty over 19M instr.
- **Instrument lesson**: headless MessageBox only appends to `services["messagebox_log"]`
  — `messagebox_ui` is a WinHelp-only service, so a probe lambda there never fires.
  Every earlier "boxes=0" claim came from that wrong channel; read messagebox_log.

## 2026-07-07 — MICROMAN runs: reaches its message loop + renders (palette/DIB path)
- Pushed the microman fixture from the CreatePalette frontier all the way into its
  running game: implemented the **palette subsystem** (CreatePalette/GetPaletteEntries/
  GetNearestPaletteIndex/GetSystemPaletteEntries/GetSystemPaletteUse + USER
  SelectPalette/RealizePalette; DC.palette field), **SetDIBitsToDevice** (8bpp
  BI_RGB/PAL_COLORS DIB → dest surface via a palette-resolved LUT — microman's core
  renderer), **MMSYSTEM.2 sndPlaySound** (event-logged like SOUND.DRV), and the
  **resource family** (FindResource/LoadResource/LockResource/FreeResource over the
  NE resources into global memory). Result: microman boots → GetMessage loop →
  creates its window (MicroManClass 544x390) → renders the MicroMan title via
  SetDIBitsToDevice (confirmed non-blank screenshot). dos_re unchanged this slice.
- **play.py is now game-agnostic**: `python scripts/play.py --game microman`
  (default ppython). Uses win16.app.create_machine + scripts/games; is_main =
  window-with-a-menu (already generic).
- **Colour fix (owner: it's a 16-colour game, was rendering grayscale):**
  microman's SetDIBitsToDevice passes fuColorUse=DIB_PAL_COLORS but ships an
  **RGBQUAD colour table** (the standard 16-colour VGA palette). Trusting the flag,
  we read WORD indices (garbage like 49152 ≥ 256) and fell back to gray. Fix:
  trust the DATA — treat the table as PAL_COLORS only when the words are valid
  palette indices, else RGBQUAD. Now renders in colour (blue "Micro Man", etc.).
- **Caveats (documented, not hidden):** (1) pure-Python interp is slow — microman
  runs ~10M instructions (~90s) before its first paint; (2) a later frontier is CPU
  opcode **FF /7** (undefined on 8086) reached after the headless pump spins the
  attract loop with no real input — likely a state divergence, not a missing opcode;
  may differ under interactive input. Next things to chase if we push microman
  further; ppython recovery remains the focus.
- Suite: 33 (microman test re-pinned to boots-and-renders: asserts it exercises
  _lopen/GetDeviceCaps/CreatePalette/SetDIBitsToDevice and runs >1.5M instr).

## 2026-07-07 — win16_re: game-agnostic launcher + MICROMAN as a hardening fixture
- Owner reorganized assets into per-game subfolders (assets/PPYTHON, MICROMAN,
  BANGBANG, KYE, SKIFREE) and reframed the project as win16_re: win16/ is the
  framework, ppython is the RE target, other games are test fixtures. Refactored:
  `win16/app.py` (generic create_machine for any NE), `scripts/games.py` (registry),
  `scripts/boot.py` (frontier probe); ppython/runtime.py → thin adapter (path fixed to
  assets/PPYTHON/PYTHON.EXE). CLAUDE.md reframed.
- **MICROMAN bring-up** (fixture): resolved all its new ordinal names (KERNEL/USER/GDI/
  MMSYSTEM, incl. __AHSHIFT/__AHINCR equates); added dos_re CPU **ENTER (0xC8)** frame
  op (committed there w/ test); implemented the KERNEL string/global-mem/_l* file batch
  (lstrcpy, GlobalAlloc/Lock/Unlock/Free/Size, GetWinFlags, GetWindowsDirectory,
  _lopen/_lcreat/_lclose/_lread/_lwrite/_llseek), USER batch (GetDesktopWindow +
  GetDC(NULL)=screen, GetTickCount, GetCursorPos, SetRect, SendMessage,
  GetAsyncKeyState), GDI GetDeviceCaps (VGA-256 profile) + SetMapMode + GetTextMetrics
  generalized to all stock fonts. Result: microman 433 instr → 1.7M instr, deep in its
  own code. These all live in the shared layer → they benefit ppython too.
- ppython unaffected (still boots both windows). Suite: 33 (+microman boot test).

## 2026-07-07 — bitmap menu items (ScreenSculptor ▸ Shape shows real icons)
- Owner: the Shape menu should show shape ICONS, not text names. Confirmed the game
  converts all 16 shape items (ids 3101-3116: PPMOUSE, PPWALL1-10, PPHEAD R/D/L/U,
  PPBALL) to bitmap menu items at boot via `ModifyMenu(MF_BITMAP)`, each pointing at
  the shape's loaded bitmap handle. ModifyMenu now records the handle in
  `Menu.item_bitmaps`; play.py renders those items as `add_checkbutton` with the
  decoded 16x16 bitmap image (so the selected-shape checkmark still works) instead of
  text. Verified all 16 render as image checkbuttons matching the game screenshot
  (PPWALL1 checked). Menu-state sync handles both text (✓ label) and bitmap (var).
- Suite: 32.

## 2026-07-07 — the REAL crash-frame fix: MessageBox must pump WM_PAINT
- The earlier _flush_windows fix was necessary but not sufficient. Root cause found
  by tracing blits: the game draws the crash head **PPHEADX to the OFFSCREEN
  playfield** (last blit before the box, at the head cell advanced into the wall),
  calls InvalidateRect (window goes dirty), then MessageBox — it never blits the
  viewport to the window itself. Real Windows' MessageBox runs a message loop that
  dispatches WM_PAINT to other windows, so the game repaints the crash head from
  its offscreen buffer WHILE the box is up. Ours just blocked. Proven: at the box
  the window is dirty and dispatching one WM_PAINT turns 0→73 center-red pixels (the
  crash head appears, advanced into the wall — matches the owner's screenshot).
- Fix: `Win16System.pump_modal(paint, timers)` dispatches a pending WM_PAINT/timer
  to a window's WndProc. MessageBox (user.py) now runs a real modal loop:
  present a NON-blocking box (play.py `ModalBox`+`MessageBoxView`, custom Win3.1
  box, not tk_messagebox — a native blocking box would freeze the GUI tick and
  hide the repaint) and pump WM_PAINT until the user answers. Dialog engine routed
  through the same pump_modal (paint+timers) for consistency. Paint-only for boxes
  keeps the crash frame frozen behind the box (no re-entrant snake movement).
- snapshot-on-box + F9 preserved; on_close releases parked box loops.
- Suite: 32.

## 2026-07-07 — crash-frame regression fixed + snapshot-on-event
- Owner: the crashed-snake frame stopped showing after the flicker fix. Root cause
  confirmed by instrumenting: the game DOES draw the crash frame before the
  "Collision!" box (surface version 58/114/170, non-blank pixels), but the
  version-gated renderer races — a tick can render the pre-crash frame, then the
  modal blocks before the next tick renders the crash frame. Fix: `_flush_windows`
  force-renders every window right before a MessageBox/dialog blocks (verified it
  flushes exactly versions 58/114). No re-introduction of flicker (only fires at
  modals, not per tick).
- **`--snapshot-on-box TEXT`**: answer to "snapshot right before the crash" — saves
  an inspection snapshot at the matching box (crash frame + memory), digest-verified
  on load. Mid-modal so not resumable; demos give reproducible replay.
- Suite: 32.

## 2026-07-07 — audio stereo fix + dialog fidelity (font base units, icons)
- **Audio crash fixed** (owner traceback, console-first paid off): SDL opened a
  STEREO mixer despite channels=1; a mono 1-D buffer → "Array must be
  2-dimensional". Now read `mixer.get_init()` and column-stack mono→stereo when
  the device is 2ch. Verified both mono and forced-stereo paths.
- **Dialog fidelity**: dialog-unit→pixel scaling now derives base units from the
  actual dialog FONT (avg char width, line height) exactly like Windows
  (x=du*baseX/4, y=du*baseY/8) instead of hardcoded (8,13) — About went 360→270px
  wide (base_x 8→6), matching the Helv-8/MS-Sans-Serif metrics. Every control uses
  that one font; dialog face is Win 3.1 gray (#c0c0c0); SS_CENTER honoured. "Helv"
  maps to MS Sans Serif (its modern descendant).
- **Icons**: `win16/icon.py` decodes GROUP_ICON directories + ICON DIBs (XOR image
  + AND transparency mask) → RGBA. The About/ScreenSculptor SS_ICON statics now
  show the real 32x32 Paulie head (was blank). LoadIcon path can reuse this later.
- Suite: 32 (added 3 icon tests; audio tests from prior slice).

## 2026-07-07 — DIALOG VISIBILITY FIX + PC-speaker-style audio
- **Dialogs were invisible** (owner: High Scores/About/Help "do nothing"): the
  Toplevel was transient to the WITHDRAWN root, so it never mapped — 1x1,
  unmapped, but it grab_set() input = an invisible modal freezing the game. Fixed
  in play.py DialogView: parent/centre over the visible game window, size+position,
  deiconify+lift+focus, grab only once visible. High Scores 658x172, About 360x238,
  verified mapped + closing on OK.
- **Audio**: the game's sound is SOUND.DRV notes (protected-mode Win16 can't touch
  the speaker ports, so no direct PC-speaker I/O — dos_re's port-based speaker model
  doesn't apply; reused only the square-wave idea). `win16/api/sound.py` now decodes
  note value→freq (note 1 = C3, semitone steps) and length+tempo→ms, feeds an
  optional backend. `win16/audio.py` SquareWaveBackend synthesizes square waves via
  pygame+numpy (no device → logged no-op, events still captured — no silent fake).
  Wired into play.py (`--mute` to disable). Captured the real jingle (51 notes,
  tempo 220, 9s) and rendered it to WAV — a proper melody, octave-exact.
- Suite: 29 (added 4 audio tests, device-free).

## 2026-07-07 — RE MACHINERY: demos + snapshots + console-first + clean Exit
- Built the dos_re-style evidence layer for Win16. **Demos** (`win16/demo.py`):
  record/replay the GetMessage stream + dialog events; replay proven bit-exact
  (record interactive session w/ dialog → replay headless → identical digest +
  playfield PNG) and fail-loud on divergence. **Snapshots** (`win16/vmsnap.py`):
  memory+CPU+OS-object-graph, digest-verified roundtrip, taken only at a message
  boundary; F9 in the player (pauses CPU at boundary via `driver.pause_at_boundary`).
  `scripts/replay.py` is the headless replay/evidence tool. Determinism gates added
  (3 tests): demo replay bit-exact, snapshot roundtrip bit-exact, divergence raises.
- **Console-first per the owner + dos_re doctrine**: VM stops print to stderr with
  CS:IP + instr count + traceback + trace tail + API log; window shows only a red
  "see console" banner; MessageBoxes echo to stdout; `--record` announced. This is an
  AI-operated harness — evidence goes to the console, not trapped in a GUI.
- **Exit crash fixed** (owner report "handle 0000 is NoneType, wanted DC"): GDI ops on
  a NULL hdc now return the API's documented failure (not a handle-table KeyError);
  the true fail-loud path (non-zero garbage handle = OUR bug) is preserved. The Exit
  path then needed GetClassInfo/UnregisterClass and DOS INT 21h AH=4Ch (terminate) →
  the app now exits cleanly (HaltExecution → "app exited cleanly", window closes).
- Digest excludes the pump's internal timer_due (unobservable scheduling detail) —
  found via a component-by-component record/replay diff.
- Suite: 25 (added the 3 determinism gates: demo bit-exact, snapshot roundtrip,
  divergence-fails-loud).

## 2026-07-07 — DIALOG ENGINE: the real thing, no stubs (About/High Scores/Options/…)
- Owner: menu items (About, High Scores, Options ▸ Mouse/Screen-set, Help) "did
  nothing" — they were the DialogBox skip-stub. Replaced with a real Win16 dialog
  engine: `win16/dialog.py` parses all 6 DLGTEMPLATEs (Static/Button/Edit/ComboBox/
  GroupBox, dialog-unit→px), `win16/api/dialogs.py` runs the game's own dialog proc
  in a modal loop and implements the dialog API family (Get/SetDlgItemText/Int,
  SendDlgItemMessage for Button BM_/ComboBox CB_/Edit EM_, DlgDirListComboBox for
  the screen-set picker). The interactive player renders real tkinter widgets
  (`DialogView`) laid out from the template; MessageBoxes are real modal boxes;
  WinHelp says "help unavailable" honestly. All 3 complex dialogs verified running
  their procs headless (About→IDOK, High Scores→IDOK, Screen Chooser). The game now
  runs THROUGH game-over + the high-score entry dialog with zero gaps (was the old
  frontier). Dialogs/controls are windows → uniform `geom_px()` resolver for
  GetWindowRect/GetClientRect/MoveWindow/etc.
- Suite: 22 (added dialog parse + engine tests; gameplay gates no longer pin the
  DialogBox frontier since it's implemented).
- Next frontier is now past the whole death/high-score loop — re-probe to find it.

## 2026-07-07 — PLAYER: flicker fixed by change-detection, not a new backend
- Owner reported menu flicker while the game runs. Cause was churn, not tkinter
  itself: the canvas image was rebuilt every 33 ms tick and all 44 menu entries
  were entryconfig'd every tick (reconfiguring an OPEN Windows menu redraws it
  and fights selection). Fix: `Surface.version` (bumped by every mutating GDI
  op) gates in-place canvas updates; menu states are cached and reconfigured
  only when the game changes them. Measured: 0 redraws + 0 menu reconfigs at
  idle; ~9 repaints/s per window in game (the game's own paint rate). A pygame
  presentation backend stays the fallback if tkinter still misbehaves on real
  hardware. Suite: 18 passed.

## 2026-07-07 — PLAYER v2: one real OS window per Win16 window; menu-state faithfulness
- Owner feedback: the menu belonged on the game's own window, and menu clicks died.
  Root causes found: (1) clicking DialogBox-backed items (About/High Scores) killed
  the worker silently; (2) **delivering WM_COMMAND for a GRAYED item is a real
  crash** — Pause while no game runs = idiv-by-zero at seg1:1F72; real USER blocks
  grayed items, so the UI must too. Both fixed: WindowView-per-handle architecture,
  menu on the PYTHON window with live grayed/checked sync (the game actively
  manages it: enables Pause during play, grays Options, checks Sound/attitude/
  shape), modal MessageBox bridge (player sees "Next Screen:"/"Collision!"/"GAME
  OVER!" boxes for real), DialogBox/WinHelp logged-skip stopgaps, red stop banner.
- Verified headless: Pause disabled→enabled by the game, menu-click New starts the
  game, collision box shows, High Scores skips without killing the VM, Pause during
  a game works. Suite: 18 passed (slower now — gameplay tests run their full budget
  since DialogBox no longer raises).
- The DialogBox engine (real dialogs: high scores, about, screen-set picker) is the
  next faithfulness slice; the skip-stub is temporary and loudly logged.

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
