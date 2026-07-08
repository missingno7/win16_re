"""USER/GDI object model: handles, window classes, windows, DCs, bitmaps.

Pixels live in Python-side surfaces (GDI is ours, there is no VGA in a Win16
world) — `Surface` is a bare bytearray of 8-bit pixels for now, grown as the
observed GDI usage demands.  All coordinates are 16-bit signed.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def _signed(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


class HandleTable:
    """Word handles -> Python objects.  Handles are even, never 0; freed
    handles are recycled (DC churn would exhaust 16 bits otherwise)."""

    def __init__(self, first: int = 0x0100) -> None:
        self._next = first
        self._objects: dict[int, object] = {}
        self._free: list[int] = []

    def add(self, obj) -> int:
        h = self._free.pop() if self._free else self._next
        if h == self._next:
            self._next += 2
            if self._next > 0xFFFE:
                raise RuntimeError("handle table exhausted")
        self._objects[h] = obj
        obj.handle = h
        return h

    def get(self, handle: int):
        return self._objects.get(handle & 0xFFFF)

    def require(self, handle: int, kind: type):
        obj = self._objects.get(handle & 0xFFFF)
        if not isinstance(obj, kind):
            raise KeyError(f"handle {handle:04X} is {type(obj).__name__}, wanted {kind.__name__}")
        return obj

    def remove(self, handle: int) -> None:
        if self._objects.pop(handle & 0xFFFF, None) is not None:
            self._free.append(handle & 0xFFFF)


@dataclass
class WndClass:
    name: str
    style: int
    wndproc: tuple[int, int]        # (seg, off)
    cls_extra: int
    wnd_extra: int
    h_instance: int
    h_icon: int
    h_cursor: int
    h_background: int
    menu_name: str | int | None
    handle: int = 0


@dataclass
class Window:
    wndclass: WndClass
    title: str
    style: int
    x: int
    y: int
    w: int
    h: int
    parent: int
    menu: int
    visible: bool = False
    dirty: bool = False             # True iff update_rect is not None
    # The accumulated update region as its bounding rect (client coords), per
    # real USER: InvalidateRect unions rects in; BeginPaint validates (clears).
    # WAP games (SimAnt) invalidate each object's own small rect and read the
    # region back via GetUpdateRgn/GetRgnBox — the rects must round-trip.
    update_rect: tuple | None = None            # (l, t, r, b) or None = clean
    update_erase: bool = False                  # an RDW_ERASE is pending
    maximized: bool = False                     # SW_SHOWMAXIMIZED applied
    restore_rect: tuple | None = None           # (x, y, w, h) before maximize
    extra: bytearray = field(default_factory=bytearray)
    scroll: dict[int, tuple[int, int, int]] = field(default_factory=dict)
    #        bar -> (min, max, pos);  bar: 0=SB_HORZ, 1=SB_VERT
    props: dict[str, int] = field(default_factory=dict)   # SetProp/GetProp store
    menu_obj: Menu | None = None
    sysmenu_obj: Menu | None = None     # window system menu (GetSystemMenu)
    _surface: "Surface" = None
    handle: int = 0

    @property
    def client_size(self) -> tuple[int, int]:
        # Non-client metrics (caption, borders, menu) are not subtracted yet;
        # grow this when GetClientRect evidence demands real values.
        return self.w, self.h

    def geom_px(self) -> tuple[int, int, int, int]:
        """(screen x, screen y, width, height) — the window-like contract
        shared with dialogs/controls so geometry APIs treat them uniformly."""
        return self.x, self.y, self.w, self.h

    @property
    def surface(self) -> "Surface":
        if self._surface is None:
            w, h = self.client_size
            self._surface = Surface(max(w, 1), max(h, 1))
        return self._surface


@dataclass
class MenuItem:
    """One entry in a programmatically built menu (CreateMenu/AppendMenu)."""
    flags: int                      # MF_* (POPUP / SEPARATOR / string)
    id: int                         # command id, or submenu handle if POPUP
    text: str = ""
    submenu: "Menu | None" = None


@dataclass
class Menu:
    name: int | str | None
    item_flags: dict[int, int] = field(default_factory=dict)   # id -> MF_* state
    item_bitmaps: dict[int, int] = field(default_factory=dict)  # id -> bitmap handle
    items: list = field(default_factory=list)                  # ordered MenuItem list
    handle: int = 0


@dataclass
class AccelTable:
    entries: list[tuple[int, int, int]]     # (flags, event, id); bit7 of flags = last
    handle: int = 0


@dataclass
class Cursor:
    name: int | str
    handle: int = 0


@dataclass
class Icon:
    name: int | str
    handle: int = 0


@dataclass
class Surface:
    """Top-down RGB pixel buffer, 3 bytes per pixel.

    `version` increments on every mutation — hosts use it to redraw only
    when pixels actually changed (flicker-free presentation)."""
    w: int
    h: int
    pixels: bytearray = field(default_factory=bytearray)
    version: int = 0

    def __post_init__(self) -> None:
        if not self.pixels:
            self.pixels = bytearray(self.w * self.h * 3)

    def touch(self) -> None:
        self.version += 1

    def fill(self, rgb: tuple[int, int, int]) -> None:
        self.pixels[:] = bytes(rgb) * (self.w * self.h)
        self.touch()


@dataclass
class Bitmap:
    surface: Surface
    handle: int = 0


@dataclass
class Font:
    """A logical font from CreateFont.  The text renderer maps every font onto
    the fixed 8x13 cell, so only the height/face are kept for metrics; `kind`
    steers GetTextMetrics to the fixed-cell numbers."""
    height: int
    facename: str = ""
    kind: str = "SYSTEM_FIXED_FONT"
    handle: int = 0


@dataclass
class Brush:
    color: int                      # COLORREF
    stock: str | None = None
    handle: int = 0


@dataclass
class Region:
    """A GDI region — currently a single bounding rectangle (SimAnt uses
    rectangular regions for clip/invalidation).  Non-rectangular combines
    degrade to their bounding box until a case needs true region algebra."""
    x1: int = 0
    y1: int = 0
    x2: int = 0
    y2: int = 0
    handle: int = 0

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        return self.x1, self.y1, self.x2, self.y2

    def is_empty(self) -> bool:
        return self.x2 <= self.x1 or self.y2 <= self.y1


@dataclass
class Palette:
    entries: list[tuple[int, int, int]] = field(default_factory=list)  # RGB
    handle: int = 0

    def nearest(self, r: int, g: int, b: int) -> int:
        best, best_d = 0, 1 << 30
        for i, (pr, pg, pb) in enumerate(self.entries):
            d = (pr - r) ** 2 + (pg - g) ** 2 + (pb - b) ** 2
            if d < best_d:
                best, best_d = i, d
        return best


@dataclass
class StockObject:
    kind: str                       # e.g. "WHITE_BRUSH", "SYSTEM_FONT"
    handle: int = 0


def blit(dst: Surface, dx: int, dy: int, src: Surface, sx: int, sy: int,
         w: int, h: int, rop: int) -> None:
    """Rectangle transfer with clipping.  ROPs implemented as real usage
    appears; RGB byte-wise AND/OR/XOR matches GDI semantics for the classic
    monochrome-mask sprite pattern."""
    # Clip against both surfaces.
    if dx < 0:
        sx -= dx; w += dx; dx = 0
    if dy < 0:
        sy -= dy; h += dy; dy = 0
    if sx < 0:
        dx -= sx; w += sx; sx = 0
    if sy < 0:
        dy -= sy; h += sy; sy = 0
    w = min(w, dst.w - dx, src.w - sx)
    h = min(h, dst.h - dy, src.h - sy)
    if w <= 0 or h <= 0:
        return
    dst.touch()
    for row in range(h):
        soff = ((sy + row) * src.w + sx) * 3
        doff = ((dy + row) * dst.w + dx) * 3
        n = w * 3
        chunk = src.pixels[soff:soff + n]
        if rop == 0x00CC0020:                   # SRCCOPY
            dst.pixels[doff:doff + n] = chunk
        elif rop == 0x008800C6:                 # SRCAND
            for i in range(n):
                dst.pixels[doff + i] &= chunk[i]
        elif rop == 0x00EE0086:                 # SRCPAINT (OR)
            for i in range(n):
                dst.pixels[doff + i] |= chunk[i]
        elif rop == 0x00660046:                 # SRCINVERT (XOR)
            for i in range(n):
                dst.pixels[doff + i] ^= chunk[i]
        elif rop == 0x00330008:                 # NOTSRCCOPY
            dst.pixels[doff:doff + n] = bytes(b ^ 0xFF for b in chunk)
        else:
            raise NotImplementedError(f"BitBlt rop {rop:#010x}")


@dataclass
class DC:
    """Device context.  window is a Window for GetDC/BeginPaint DCs; memory
    DCs target a selected Bitmap instead."""
    window: object = None
    bitmap: Bitmap | None = None
    is_memory: bool = False
    text_color: int = 0
    bk_color: int = 0xFFFFFF
    bk_mode: int = 2                # OPAQUE
    stretch_mode: int = 1
    selected: dict[str, object] = field(default_factory=dict)
    palette: object = None          # selected logical Palette (None = default)
    clip_rect: tuple | None = None  # (l,t,r,b) intersect-clip; None = unclipped
    save_stack: list = field(default_factory=list)   # SaveDC/RestoreDC states
    handle: int = 0
