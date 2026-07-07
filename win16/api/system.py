"""Win16System — the OS-side state behind the API handlers.

Owns task identity (hInstance == DGROUP selector, Win16 convention), the
PSP-style command-line block, and grows handle tables as the API surface
grows.  Game-agnostic: configured entirely from the loaded machine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

SW_SHOWNORMAL = 1


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

    def __post_init__(self) -> None:
        from collections import deque
        from .objects import HandleTable
        self.machine.api.services["system"] = self
        if not self.module_dos_path:
            self.module_dos_path = "C:\\" + self.machine.exe.path.name.upper()
        self.handles = HandleTable()
        self.msg_queue = deque()

    def call_wndproc(self, window, msg: int, wparam: int, lparam: int) -> int:
        """Send a message straight to the window's proc (SendMessage path)."""
        from win16.callback import call_far
        from win16.loader import THUNK_SEG
        seg, off = window.wndclass.wndproc
        ax, dx = call_far(self.machine.cpu, THUNK_SEG, seg, off,
                          [window.handle, msg,
                           wparam & 0xFFFF,
                           (lparam >> 16) & 0xFFFF, lparam & 0xFFFF])
        return (dx << 16) | ax

    def post_message(self, hwnd: int, msg: int, wparam: int, lparam: int) -> None:
        self.msg_queue.append((hwnd, msg, wparam, lparam, self.clock_ms, 0))

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
        if timers and self.timer_due:
            key, due = min(self.timer_due.items(), key=lambda kv: kv[1])
            if self.clock_ms >= due:
                self.timer_due[key] = self.clock_ms + self.timers[key]
                win = self.handles.get(key[0])
                if win is not None:
                    self.call_wndproc(win, 0x0113, key[1], 0)  # WM_TIMER
                    return True
        return False

    def get_message(self):
        """What GetMessage returns: delegate to an interactive driver when one
        is installed, else the deterministic pump.  Every returned message
        passes through the demo tap when recording."""
        if self.message_source is not None:
            msg = self.message_source(self)
        else:
            msg = self.next_message()
        recorder = self.machine.api.services.get("demo_recorder")
        if recorder is not None:
            recorder.message(msg)
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
        if self.timers:
            key, due = min(self.timer_due.items(), key=lambda kv: kv[1])
            self.clock_ms = max(self.clock_ms, due)
            self.timer_due[key] = self.clock_ms + self.timers[key]
            hwnd, timer_id = key
            return (hwnd, 0x0113, timer_id, 0, self.clock_ms, 0)      # WM_TIMER
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

    global_blocks: dict[int, int] = field(default_factory=dict)   # seg -> size

    def global_alloc(self, size: int, *, zero: bool = False) -> int:
        """Allocate a global block; the segment value IS the handle (flat model:
        selectors are paragraph bases).  Returns 0 on failure."""
        paras = max((size + 15) >> 4, 1)
        try:
            seg = self.machine.alloc_paragraphs(paras)
        except Exception:  # noqa: BLE001 — out of VM memory -> API failure
            return 0
        self.global_blocks[seg] = size
        if zero:
            for i in range(size):
                self.machine.mem.wb(seg, i, 0)
        return seg

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
