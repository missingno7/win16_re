"""Win16System — the OS-side state behind the API handlers.

Owns task identity (hInstance == DGROUP selector, Win16 convention), the
PSP-style command-line block, and grows handle tables as the API surface
grows.  Game-agnostic: configured entirely from the loaded machine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

SW_SHOWNORMAL = 1

# Virtual interpreted-instructions per millisecond for the GetTickCount floor
# (a mid-90s PC pace).  The single source of truth: GetTickCount (user.py) and
# the timer clock both read it, so a busy-wait's clock and its timer stay
# consistent (else a WM_TIMER never becomes "due" while code spins on it).
INSTR_PER_MS = 1000


@dataclass
class VFile:
    """An open file: reads come from disk (or the write overlay); writes stay
    in the overlay — original assets are never mutated."""
    name: str                       # canonical upper-case basename
    data: bytearray
    pos: int = 0
    writable: bool = False
    dirty: bool = False

# INSTANCEDATA offsets in DGROUP (the 16 reserved bytes at seg:0000).
INSTANCE_STACK_TOP = 0x0A     # lowest stack address (stack grows down)
INSTANCE_STACK_MIN = 0x0C     # lowest SP observed
INSTANCE_STACK_BOT = 0x0E     # initial SP (bottom of the stack)

# The Windows 3.x default system palette: the 20 static colours (10 at each
# end), everything between black until an app realizes a logical palette.
_STATIC_LOW = [(0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0),
               (0, 0, 128), (128, 0, 128), (0, 128, 128), (192, 192, 192),
               (192, 220, 192), (166, 202, 240)]
_STATIC_HIGH = [(255, 251, 240), (160, 160, 164), (128, 128, 128),
                (255, 0, 0), (0, 255, 0), (255, 255, 0), (0, 0, 255),
                (255, 0, 255), (0, 255, 255), (255, 255, 255)]


def _default_system_palette() -> list:
    return _STATIC_LOW + [(0, 0, 0)] * 236 + _STATIC_HIGH


@dataclass
class Win16System:
    machine: object                     # win16.loader.Win16Machine
    cmd_show: int = SW_SHOWNORMAL
    command_line: bytes = b""
    module_dos_path: str = ""           # virtual DOS path of the EXE
    psp_seg: int = 0
    h_prev_instance: int = 0
    booted: bool = False                # set once InitTask has run
    int_vectors: dict[int, tuple[int, int]] = field(default_factory=dict)
    env_seg: int = 0
    _local_heap: object = None
    handles: object = None              # HandleTable
    classes: dict[str, object] = field(default_factory=dict)   # name -> WndClass
    windows: list[object] = field(default_factory=list)        # creation order
    msg_queue: object = None            # deque of 6-tuples (hwnd,msg,wp,lp,time,pt)
    timers: dict[tuple[int, int], int] = field(default_factory=dict)  # (hwnd,id)->ms
    timer_due: dict[tuple[int, int], int] = field(default_factory=dict)
    timer_procs: dict[tuple[int, int], int] = field(default_factory=dict)
    #   (hwnd,id) -> TimerProc far pointer (seg<<16|off) for SetTimer with a
    #   callback.  Its WM_TIMER carries the proc in lParam; DispatchMessage
    #   calls the proc instead of the wndproc (SimAnt's ~59fps sim tick — its
    #   wndproc does NOT handle WM_TIMER and hangs if sent it).
    clock_ms: int = 0                   # virtual message-time clock
    quit_posted: int | None = None      # PostQuitMessage exit code
    file_root: Path | None = None       # where game data files live
    files: dict[int, VFile] = field(default_factory=dict)      # handle -> VFile
    overlay_files: dict[str, bytearray] = field(default_factory=dict)
    next_file_handle: int = 5
    profiles: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)
    #          file -> section -> key -> value   (all keys canonical lower-case)
    stock_handles: dict[int, int] = field(default_factory=dict)
    message_source: object = None       # optional: callable(sys) -> msg | None
    #   When set, GetMessage delegates to it (an interactive/real-time driver);
    #   otherwise the deterministic next_message() drives (headless replay).
    interactive: bool = False           # a real-time host driver is installed:
    #   GetTickCount then tracks the driver's WALL clock (clock_ms, kept current
    #   during a callback by yield_check) instead of the instruction floor.  The
    #   floor over-runs wall time (the interpreter runs faster than INSTR_PER_MS),
    #   which made SimAnt's sim-tick think thousands of frames were due and
    #   process them all in one callback (20M-step overrun).  Headless keeps the
    #   floor so busy-waits without message pumping still elapse deterministically.
    yield_check: object = None          # optional: callable() run between chunks
    #   of a long VM callback (SimAnt's sim-tick TimerProc) so the host can pause
    #   / take a snapshot / feed input instead of the UI freezing.
    callback_max_steps: object = 20_000_000     # runaway cap for a VM callback
    #   (DispatchMessage's sim-tick TimerProc).  Headless/replay keeps the cap to
    #   catch a genuine hang; an interactive driver sets None (no cap) because a
    #   live sim-tick legitimately busy-waits on the real clock and on input —
    #   the cap would kill a paused game or a "press a key" wait mid-callback.
    demo_driver: object = None          # optional: a win16.demo.DemoDriver (v4
    #   replay).  When set, GetMessage/PeekMessage inject recorded input arrivals
    #   at their instruction counts and GetTickCount reproduces the recorded
    #   (instruction -> tick) timeline — see win16/demo.py.
    tick_recorder: object = None        # optional: a win16.tick_demo.TickDemoRecorder
    #   — taps every consumed input-class message + WM_TIMER boundary (works on
    #   top of live play OR a v4 replay: the conversion path).
    tick_driver: object = None          # optional: a win16.tick_demo.TickDemoDriver
    #   — hook-config-invariant replay: input injected per TICK bucket, the
    #   boundary WM_TIMER delivered on the game's own ask, GetTickCount served
    #   from the recording.  Mutually exclusive with demo_driver.
    input_drainer: object = None        # optional: callable() moving host input
    #   into msg_queue.  An interactive driver sets this so PeekMessage (which
    #   scans msg_queue directly, not via message_source) sees freshly-posted
    #   input during a busy-poll loop that never calls GetMessage — SimAnt's
    #   menus/in-game spin on PeekMessage, so without this a click never lands.
    system_palette: list = field(default_factory=lambda: _default_system_palette())
    #   The display's hardware palette in the static single-app model:
    #   RealizePalette copies the realized logical palette here and
    #   GetSystemPaletteEntries reports it (games nearest-match against it).

    def ensure_environment(self) -> int:
        """DOS environment block: ASCIIZ vars, double zero, WORD 1, exe path."""
        if not self.env_seg:
            block = b"PATH=C:\\\x00" + b"\x00"
            block += (1).to_bytes(2, "little")
            block += self.module_dos_path.encode("ascii") + b"\x00"
            paras = (len(block) + 15) >> 4
            self.env_seg = self.machine.alloc_paragraphs(paras)
            self.machine.mem.load(self.env_seg, 0, block)
        return self.env_seg

    @property
    def local_heap(self):
        """DGROUP local heap: [static data + stack, end of DGROUP allocation)."""
        if self._local_heap is None:
            from .localheap import LocalHeap
            hdr = self.machine.exe.header
            _, sp0 = self.stack_bounds()
            self._local_heap = LocalHeap(sp0, sp0 + hdr.heap_size)
        return self._local_heap

    huge_heap: object = None

    def __post_init__(self) -> None:
        from collections import deque
        from .objects import HandleTable
        from win16.hugeheap import HugeHeap
        from win16.loader import GLOBAL_LIN_START, WIN16_MEM_SIZE
        self.machine.api.services["system"] = self
        if not self.module_dos_path:
            self.module_dos_path = "C:\\" + self.machine.exe.path.name.upper()
        self.handles = HandleTable()
        self.msg_queue = deque()
        # Global memory is selector-based over the linear space above the image.
        # Start selector VALUES above the program's low real-mode paragraph
        # bases (image + PSP/env/scratch) so the two never alias.
        first_index = ((self.machine.free_para + 0x800) >> 3) + 1
        self.huge_heap = HugeHeap(self.machine.mem.sel_base,
                                  GLOBAL_LIN_START, WIN16_MEM_SIZE,
                                  first_index=first_index)
        # Segments below the first selector skip the sel_base dict lookup (the
        # hot path stays real-mode-fast for code/stack/dgroup accesses).
        self.machine.mem.sel_min = self.huge_heap.selector_floor

    def call_wndproc(self, window, msg: int, wparam: int, lparam: int) -> int:
        """Send a message straight to the window's proc (SendMessage path)."""
        from win16.callback import call_far
        from win16.loader import THUNK_SEG
        seg, off = window.wndclass.wndproc
        ax, dx = call_far(self.machine.cpu, THUNK_SEG, seg, off,
                          [window.handle, msg,
                           wparam & 0xFFFF,
                           (lparam >> 16) & 0xFFFF, lparam & 0xFFFF],
                          yield_check=self.yield_check)
        return (dx << 16) | ax

    def post_message(self, hwnd: int, msg: int, wparam: int, lparam: int) -> None:
        self.msg_queue.append((hwnd, msg, wparam, lparam, self.clock_ms, 0))

    def peek_message(self, hwnd_filter: int, lo: int, hi: int, remove: bool):
        """Non-blocking queue scan for PeekMessage: the first posted message
        matching the hwnd filter (0 = any) and message range [lo, hi] (0,0 =
        any).  Optionally removes it.  Returns the 6-tuple, or None if the
        queue holds no matching message.  Unlike GetMessage it never blocks and
        never synthesizes paint/timer — a game's peek loop must fall through to
        its idle path (WaitMessage/GetMessage) when nothing is queued.

        Under a v4 demo driver (replay) this injects any input arrivals whose
        instruction count has been reached, then scans the real queue — a
        busy-poll simply misses until its awaited arrival's instruction; the
        game's own pump does the rest (see win16/demo.py)."""
        if self.demo_driver is not None:
            self.demo_driver.pump_peek()    # inject due arrivals into the queue
        if self.input_drainer is not None:
            self.input_drainer()            # make host input visible to the scan
        for i, m in enumerate(self.msg_queue):
            if hwnd_filter and m[0] != hwnd_filter:
                continue
            if (lo or hi) and not (lo <= m[1] <= hi):
                continue
            if remove:
                del self.msg_queue[i]
                if self.input_drainer is None:      # else noted at drain time
                    self._note_input(m)             # feed polled state (mouse/keys)
                if self.tick_recorder is not None:
                    self._tick_record(m)
            return m
        # Tick replay: the queue is empty — deliver the current bucket's next
        # recorded message on demand (consumption order; see win16/tick_demo.py).
        # Once the bucket is drained the recorded boundary WM_TIMER is offered to
        # any ask whose filter admits it — INCLUDING the (0,0) any-scan (real
        # PeekMessage(0,0) returns a due WM_TIMER; SimAnt's cold-start modal
        # spins on an any-scan peek, so gating on an explicit WM_TIMER filter
        # would stall it there forever).
        if self.tick_driver is not None:
            m = self.tick_driver.next_input(hwnd_filter, lo, hi, remove)
            if m is not None:
                return m
            if (lo == 0 and hi == 0) or lo <= 0x0113 <= hi:
                return self.tick_driver.timer_ask(hwnd_filter, remove)
            return None
        # A due WM_TIMER is discoverable by PeekMessage too, not only GetMessage.
        # SimAnt's sim tick (a SetTimer TimerProc) paces its frame by spinning on
        # PeekMessage(.., WM_TIMER, WM_TIMER, PM_REMOVE) until the next tick is
        # due — we synthesize timers lazily, so if they were only visible to
        # GetMessage that spin would never end.  Only for an explicit WM_TIMER
        # filter (not the (0,0) any-scan, which is the input drain).
        if (lo or hi) and lo <= 0x0113 <= hi:
            tm = self._due_timer(hwnd_filter, remove)
            if tm is not None:
                if remove and self.tick_recorder is not None:
                    self.tick_recorder.boundary(tm)
                return tm
        return None

    def _tick_record(self, m) -> None:
        """Tick-recorder tap for one CONSUMED message: external input is
        bucketed under the current tick; a WM_TIMER closes it (the boundary)."""
        from win16.tick_demo import is_input_message
        if m[1] == 0x0113:
            self.tick_recorder.boundary(m)
        elif is_input_message(m[1]):
            self.tick_recorder.input(m)

    def _due_timer(self, hwnd_filter: int, remove: bool):
        """The earliest armed timer that is now due (by the GetTickCount clock:
        max of the message clock and the instruction floor), as a WM_TIMER
        6-tuple, or None.  Reschedules it when `remove` (PM_REMOVE / GetMessage)."""
        if not self.timers:
            return None
        now = self.tick_count()
        best = None
        for key, due in self.timer_due.items():
            if hwnd_filter and key[0] != hwnd_filter:
                continue
            if now >= due and (best is None or due < best[1]):
                best = (key, due)
        if best is None:
            return None
        key = best[0]
        if remove:
            self.timer_due[key] = now + self.timers[key]
        hwnd, timer_id = key
        return (hwnd, 0x0113, timer_id, self.timer_procs.get(key, 0), now, 0)

    def tick_count(self) -> int:
        """The GetTickCount value, shared by USER.13 and the WM_TIMER clock so a
        busy-wait's clock and the timer it spins on stay consistent.  Interactive:
        the wall clock (clock_ms).  Headless: max(message clock, instruction
        floor) — the floor keeps a message-less busy-wait progressing.

        The floor is ANCHORED: a resumed snapshot re-bases it so it continues
        from the saved clock (vmsnap.load_snapshot sets clock_floor_anchor),
        mirroring how the interactive driver re-anchors its wall clock.
        Without this, a snapshot whose wall clock ran ahead of the instruction
        pace freezes GetTickCount on resume until the raw floor catches up —
        tens of millions of instructions of stuck busy-waits."""
        tick_driver = getattr(self, "tick_driver", None)
        if tick_driver is not None:
            # Tick replay: the current tick's recorded base + a deterministic
            # call-count escape — no instruction counts (see win16/tick_demo.py).
            return tick_driver.tick_count() & 0xFFFFFFFF
        driver = getattr(self, "demo_driver", None)
        if driver is not None:
            # Replay: reproduce the recorded wall-clock timeline from the demo's
            # (instruction -> tick) samples, so clock-driven control flow matches
            # the recording exactly (see win16/demo.py).
            return driver.tick_at(self.machine.cpu.instruction_count) & 0xFFFFFFFF
        ms = self.clock_ms
        if not self.interactive:
            base_instr, base_ms = getattr(self, "clock_floor_anchor", (0, 0))
            floor = base_ms + (
                self.machine.cpu.instruction_count - base_instr) // INSTR_PER_MS
            ms = max(ms, floor)
        return ms & 0xFFFFFFFF

    def _window_origin(self, hwnd: int) -> tuple[int, int]:
        """Absolute (screen) top-left of a window: its own (x, y) plus every
        ancestor's, walking the parent chain (no non-client insets modelled)."""
        x = y = 0
        w = self.handles.get(hwnd)
        while w is not None and hasattr(w, "x"):
            x += w.x
            y += w.y
            w = self.handles.get(w.parent) if getattr(w, "parent", 0) else None
        return x, y

    def _note_input(self, msg) -> None:
        """Derive POLLED input state from a delivered message so GetKeyState /
        GetAsyncKeyState / GetCursorPos reflect it — SimAnt's WAP engine steers
        entirely by polling those, not by handling the messages.  Called for
        every message consumed via GetMessage OR PeekMessage(PM_REMOVE) so both
        pump styles feed identical state (and demo replay stays deterministic)."""
        services = self.machine.api.services
        mtype, wparam, lparam = msg[1], msg[2], msg[3]
        if mtype == 0x0100:                              # WM_KEYDOWN
            services.setdefault("async_keys", set()).add(wparam & 0xFFFF)
            services.setdefault("async_keys_tapped", set()).add(wparam & 0xFFFF)
        elif mtype == 0x0101:                            # WM_KEYUP
            services.get("async_keys", set()).discard(wparam & 0xFFFF)
        elif 0x0200 <= mtype <= 0x0209:                  # mouse move / buttons
            ox, oy = self._window_origin(msg[0])         # client -> screen
            services["cursor_pos"] = ((ox + (lparam & 0xFFFF)) & 0xFFFF,
                                      (oy + ((lparam >> 16) & 0xFFFF)) & 0xFFFF)
            down = {0x0201: 0x01, 0x0204: 0x02, 0x0207: 0x04}   # L/R/M -> VK
            up = {0x0202: 0x01, 0x0205: 0x02, 0x0208: 0x04}
            if mtype in down:
                services.setdefault("async_keys", set()).add(down[mtype])
                services.setdefault("async_keys_tapped", set()).add(down[mtype])
            elif mtype in up:
                services.get("async_keys", set()).discard(up[mtype])

    def refresh_polled_input(self) -> None:
        """Make freshly-arrived input visible to a game that POLLS
        GetAsyncKeyState/GetKeyState/GetCursorPos without pumping the queue.
        SimAnt's caste-slider drag spins on GetAsyncKeyState(VK_LBUTTON) waiting
        for the button to release WITHOUT calling Peek/GetMessage — so without
        this the WM_LBUTTONUP never drains, the button reads down forever, and
        the game freezes.  The interactive drainer feeds polled state at arrival
        time (see the driver), so one drain here refreshes it.

        Under a v4 demo driver, injecting HERE — at the poll itself — is what
        makes polled input CONFIG-INVARIANT: the poll executes at the same
        instruction count on the interpreted oracle and on a virtual-time-
        preserving lifted graph, while the driver's other in-callback
        touchpoint (yield_check) fires per interpreter STEP, whose instruction
        coverage changes with the installed hook set.  Found live as the first
        oracle-vs-VMless-graph divergence: a GetAsyncKeyState(VK_LBUTTON) poll
        racing a recorded WM_LBUTTONDOWN arrival between two yields."""
        if self.demo_driver is not None:
            self.demo_driver.inject_due()   # arrivals due AT THIS instruction
        if self.input_drainer is not None:
            self.input_drainer()

    def pump_modal(self, *, paint: bool = True, timers: bool = False) -> bool:
        """Dispatch one pending WM_PAINT (and optionally a due WM_TIMER) to a
        window's WndProc — what a real modal loop (MessageBox/DialogBox) does so
        other windows keep repainting/animating while it is up.  Returns True if
        it dispatched anything.  Paint is what lets the game show a frame it drew
        offscreen right before the modal (e.g. the crashed-snake frame)."""
        if paint:
            for win in self.windows:
                if win.visible and win.dirty:
                    self.call_wndproc(win, 0x000F, 0, 0)     # WM_PAINT
                    return True
        if timers and self.tick_driver is not None:
            raise NotImplementedError(
                "tick replay through a modal timer pump — no recording has "
                "exercised this; implement against the first one that does")
        if timers and self.timer_due:
            key, due = min(self.timer_due.items(), key=lambda kv: kv[1])
            if self.clock_ms >= due:
                self.timer_due[key] = self.clock_ms + self.timers[key]
                win = self.handles.get(key[0])
                if win is not None:
                    if self.tick_recorder is not None:
                        self.tick_recorder.boundary(
                            (key[0], 0x0113, key[1],
                             self.timer_procs.get(key, 0), self.clock_ms, 0))
                    self.call_wndproc(win, 0x0113, key[1], 0)  # WM_TIMER
                    return True
        return False

    def get_message(self):
        """What GetMessage returns: a v4 demo driver (replay) injects recorded
        arrivals and runs the pump; else an interactive driver when installed;
        else the deterministic headless pump."""
        if self.demo_driver is not None:
            msg = self.demo_driver.pump_get()
        elif self.message_source is not None:
            msg = self.message_source(self)
        else:
            msg = self.next_message()
        if msg is not None and self.input_drainer is None and self.demo_driver is None \
                and self.tick_driver is None:
            # Polled input state (keys + mouse pos/buttons) is DERIVED from the
            # message stream, not from host polling, so GetAsyncKeyState /
            # GetKeyState / GetCursorPos see the same state on live play and demo
            # replay.  Games that poll instead of handling messages (microman
            # steers via GetAsyncKeyState; SimAnt's WAP via GetCursorPos +
            # GetKeyState) read this — see _note_input.  When an interactive
            # driver is attached it notes polled state at ARRIVAL time (so a
            # non-pumping poll loop still sees it), so we must not double-note.
            # (A tick driver also notes at its bucket injection.)
            self._note_input(msg)
        if self.tick_recorder is not None:
            if msg is None:
                self.tick_recorder.quit()
            else:
                self._tick_record(msg)
        return msg

    def next_message(self):
        """The message-pump core (GetMessage order: posted > paint > timer).

        Returns a 6-tuple message, or None for WM_QUIT.  Raises when truly
        idle — an interactive driver must feed input before that happens.
        """
        if self.quit_posted is not None:
            return None
        if self.msg_queue:
            return self.msg_queue.popleft()
        for win in self.windows:
            if win.visible and win.dirty:
                return (win.handle, 0x000F, 0, 0, self.clock_ms, 0)   # WM_PAINT
        if self.tick_driver is not None:
            # Tick replay, idle: the current bucket's next recorded message on
            # demand; once the bucket drains, the boundary — never from clock
            # comparison (see win16/tick_demo.py).
            m = self.tick_driver.next_input(0, 0, 0, True)
            if m is not None:
                return m
            if self.quit_posted is not None:    # quit landed with the bucket end
                return None
            if self.timers:
                return self.tick_driver.timer_ask(0, True)
            from win16.demo import DemoEnded
            raise DemoEnded(
                "tick replay: GetMessage wanted input with no armed timer — "
                "bucket exhausted (end of the recorded pre-tick phase?)")
        if self.timers:
            key, due = min(self.timer_due.items(), key=lambda kv: kv[1])
            self.clock_ms = max(self.clock_ms, due)
            self.timer_due[key] = self.clock_ms + self.timers[key]
            hwnd, timer_id = key
            proc = self.timer_procs.get(key, 0)                      # 0 = to wndproc
            return (hwnd, 0x0113, timer_id, proc, self.clock_ms, 0)  # WM_TIMER
        raise RuntimeError(
            "GetMessage with an empty queue, no dirty window and no armed timer "
            "— an input driver must post messages")

    # -- GDI stock objects + DC defaults -------------------------------------
    def stock_object(self, idx: int) -> int:
        from .objects import StockObject
        from .gdi import STOCK_NAMES
        if idx not in STOCK_NAMES:
            raise NotImplementedError(f"GetStockObject({idx}) — unknown stock index")
        if idx not in self.stock_handles:
            self.stock_handles[idx] = self.handles.add(StockObject(STOCK_NAMES[idx]))
        return self.stock_handles[idx]

    def new_dc(self, **kwargs) -> int:
        """Create a DC with the real-GDI default objects pre-selected."""
        from .objects import DC, Bitmap, Surface
        dc = DC(**kwargs)
        dc.selected = {
            "brush": self.handles.get(self.stock_object(0)),    # WHITE_BRUSH
            "pen": self.handles.get(self.stock_object(7)),      # BLACK_PEN
            "font": self.handles.get(self.stock_object(13)),    # SYSTEM_FONT
        }
        if dc.is_memory:
            # Real memory DCs own a default 1x1 bitmap; its handle is what
            # the first SelectObject returns (apps select it back to restore).
            bmp = Bitmap(Surface(1, 1))
            self.handles.add(bmp)
            dc.bitmap = bmp
        return self.handles.add(dc)

    # -- file service (shared by OpenFile and the DOS handle calls) ---------
    def _canonical(self, dos_path: str) -> str:
        return dos_path.replace("/", "\\").split("\\")[-1].upper()

    def file_open(self, dos_path: str, *, writable: bool = False,
                  create: bool = False) -> int:
        """Returns a handle, or -1 when the file does not exist."""
        name = self._canonical(dos_path)
        if create:
            data = bytearray()
        elif name in self.overlay_files:
            data = bytearray(self.overlay_files[name])
        elif name in self.machine.api.provided_dlls:
            # A DLL we provide as a Python API surface "exists" as a file — games
            # probe for it (e.g. SimAnt _access()es mmsystem.dll) before loading.
            data = bytearray(b"\x00")
        else:
            root = self.file_root or self.machine.exe.path.parent
            match = next((p for p in root.iterdir()
                          if p.is_file() and p.name.upper() == name), None)
            if match is None:
                return -1
            data = bytearray(match.read_bytes())
        h = self.next_file_handle
        self.next_file_handle += 1
        self.files[h] = VFile(name, data, writable=writable or create,
                              dirty=create)
        return h

    def resolve_host_path(self, dos_path: str):
        """Map a DOS path (e.g. ``C:\\sound\\gamethme.mid``) to the real file
        under the game's root, following subdirectories case-insensitively.
        Returns a Path or None.  Used to hand a real file to a host backend
        (e.g. the MIDI player) — unlike file_open, it keeps the directory parts."""
        from pathlib import Path
        root = self.file_root or self.machine.exe.path.parent
        p = dos_path.replace("/", "\\")
        if len(p) >= 2 and p[1] == ":":
            p = p[2:]
        cur = Path(root)
        for part in (x for x in p.split("\\") if x):
            if not cur.is_dir():
                return None
            nxt = next((c for c in cur.iterdir() if c.name.upper() == part.upper()), None)
            if nxt is None:
                return None
            cur = nxt
        return cur if cur.is_file() else None

    def file_close(self, handle: int) -> bool:
        vf = self.files.pop(handle, None)
        if vf is None:
            return False
        if vf.dirty:
            self.overlay_files[vf.name] = vf.data
        return True

    # -- private-profile (INI) service --------------------------------------
    def profile(self, dos_path: str) -> dict[str, dict[str, str]]:
        name = self._canonical(dos_path)
        if name not in self.profiles:
            data: dict[str, dict[str, str]] = {}
            handle = self.file_open(name)
            if handle >= 0:
                text = self.files[handle].data.decode("latin-1", "replace")
                self.file_close(handle)
                section = ""
                for line in text.splitlines():
                    line = line.strip()
                    if not line or line.startswith(";"):
                        continue
                    if line.startswith("[") and line.endswith("]"):
                        section = line[1:-1].strip().lower()
                    elif "=" in line:
                        key, _, value = line.partition("=")
                        data.setdefault(section, {})[key.strip().lower()] = value.strip()
            self.profiles[name] = data
        return self.profiles[name]

    @property
    def h_instance(self) -> int:
        return self.machine.seg_bases[self.machine.exe.header.auto_data_seg]

    def global_alloc(self, size: int, *, zero: bool = False,
                     discardable: bool = False) -> int:
        """Allocate a global block via the selector heap; the returned base
        selector IS the handle.  Returns 0 on failure (out of memory).
        `discardable` (GMEM_DISCARDABLE) is remembered for GlobalFlags so a
        discardable cache can identify evictable blocks."""
        seg = self.huge_heap.alloc(size, discardable=discardable)
        if seg and zero:
            base = self.huge_heap.linear_base(seg)
            self.machine.mem.data[base:base + size] = b"\x00" * size
        return seg

    def global_free(self, seg: int) -> bool:
        return self.huge_heap.free(seg)

    def global_size(self, seg: int) -> int:
        return self.huge_heap.size_of(seg)

    def is_global(self, seg: int) -> bool:
        return self.huge_heap.is_block(seg)

    def ensure_psp(self) -> int:
        """Allocate a PSP-style paragraph block holding the command tail."""
        if not self.psp_seg:
            tail = self.command_line[:126]
            self.psp_seg = self.machine.alloc_paragraphs(16)  # 256 bytes
            mem = self.machine.mem
            mem.wb(self.psp_seg, 0x80, len(tail))
            mem.load(self.psp_seg, 0x81, tail + b"\x0d")
        return self.psp_seg

    def stack_bounds(self) -> tuple[int, int]:
        """(lowest stack address, initial SP) within DGROUP."""
        hdr = self.machine.exe.header
        data_len = self.machine.exe.segments[hdr.auto_data_seg - 1].alloc_size
        sp0 = (data_len + hdr.stack_size) & ~1
        return data_len, sp0
