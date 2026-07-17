"""USER services — windowing, messages, dialogs. Implemented per observed call."""
from __future__ import annotations

from .core import ApiRegistry, CallContext
from .objects import Cursor, Icon, Menu, Region, Window, WndClass, _signed
from .system import INSTR_PER_MS, Win16System

WM_CREATE = 0x0001
WM_DESTROY = 0x0002
WM_MOVE = 0x0003
WM_SIZE = 0x0005

# Win16 CW_USEDEFAULT
CW_USEDEFAULT = 0x8000
WS_VISIBLE = 0x10000000


def _resource_name(ctx: CallContext, segptr: int) -> int | str:
    """A Win16 resource-name argument: integer atom when the high word is 0,
    otherwise a far pointer to an ASCIIZ name."""
    if (segptr >> 16) & 0xFFFF == 0:
        return segptr & 0xFFFF
    return ctx.read_string(segptr).decode("latin-1")


def _sys(ctx: CallContext) -> Win16System:
    return ctx.registry.services["system"]


class AtomTable:
    """The system-wide string atom table (GlobalAddAtom / RegisterWindowMessage).

    Win16's contract, and every part of it is load-bearing somewhere:
      * one atom per DISTINCT string, compared case-insensitively;
      * atoms are ref-counted — n adds need n deletes before the atom dies;
      * string atoms live in 0xC000..0xFFFF.  RegisterWindowMessage mints from
        this same space, which is what puts a registered message above every
        WM_USER-relative control message and lets a wndproc tell them apart.
    Integer atoms (the "#1234" form / MAKEINTATOM) are a separate space that no
    caller has needed yet: they raise rather than silently colliding with the
    string space.
    """

    FIRST, LAST = 0xC000, 0xFFFF

    def __init__(self) -> None:
        self.by_name: dict[str, int] = {}
        self.names: dict[int, str] = {}
        self.refs: dict[int, int] = {}

    def add(self, name: bytes | str) -> int:
        if isinstance(name, bytes):
            name = name.decode("latin-1")
        if name.startswith("#"):
            raise NotImplementedError(
                f"integer atom {name!r} — only string atoms are implemented")
        key = name.upper()
        atom = self.by_name.get(key)
        if atom is None:
            atom = self.FIRST + len(self.by_name)
            if atom > self.LAST:
                return 0                        # table full — real USER's answer
            self.by_name[key] = atom
            self.names[atom] = name
            self.refs[atom] = 0
        self.refs[atom] += 1
        return atom

    def delete(self, atom: int) -> int:
        """-> 0 when the reference is dropped, else the atom (USER's failure)."""
        if atom not in self.refs:
            return atom & 0xFFFF
        self.refs[atom] -= 1
        if self.refs[atom] <= 0:
            del self.by_name[self.names.pop(atom).upper()]
            del self.refs[atom]
        return 0

    def find(self, name: bytes | str) -> int:
        if isinstance(name, bytes):
            name = name.decode("latin-1")
        return self.by_name.get(name.upper(), 0)


def _atoms(sysobj: Win16System) -> AtomTable:
    services = sysobj.machine.api.services
    table = services.get("atoms")
    if table is None:
        table = services["atoms"] = AtomTable()
    return table


def _desktop_window(sys: Win16System) -> Window:
    """The screen/desktop pseudo-window (GetDesktopWindow / GetDC(NULL))."""
    win = sys.machine.api.services.get("desktop_window")
    if win is None:
        cls = WndClass(name="#desktop", style=0, wndproc=(0, 0), cls_extra=0,
                       wnd_extra=0, h_instance=0, h_icon=0, h_cursor=0,
                       h_background=0, menu_name=None)
        sys.handles.add(cls)
        win = Window(wndclass=cls, title="", style=0, x=0, y=0,
                     w=SYSTEM_METRICS[0], h=SYSTEM_METRICS[1], parent=0, menu=0)
        sys.handles.add(win)
        sys.machine.api.services["desktop_window"] = win
    return win


def _z_children(sysobj, parent_handle: int) -> list:
    """A parent's child windows in TOP-to-bottom Z-order.  Our window list is
    draw order (last drawn = on top), so top-to-bottom is the reverse."""
    kids = [w for w in sysobj.windows
            if isinstance(w, Window) and w.parent == parent_handle]
    kids.reverse()
    return kids


# GetWindow / GetNextWindow command codes.
_GW_HWNDFIRST, _GW_HWNDLAST, _GW_HWNDNEXT, _GW_HWNDPREV, _GW_OWNER, _GW_CHILD = \
    0, 1, 2, 3, 4, 5


def _get_window(sysobj, hwnd: int, cmd: int) -> int:
    """USER GetWindow/GetNextWindow: the window related to hwnd by `cmd`, or 0.
    Siblings share a parent and are ordered top-to-bottom in Z-order."""
    win = sysobj.handles.get(hwnd)
    if not isinstance(win, Window):
        return 0
    if cmd == _GW_CHILD:
        kids = _z_children(sysobj, hwnd)
        return kids[0].handle if kids else 0
    if cmd == _GW_OWNER:
        return 0                                # owner (not parent) — untracked
    sibs = [w.handle for w in _z_children(sysobj, win.parent)]
    if win.handle not in sibs:
        return 0
    i = sibs.index(win.handle)
    if cmd == _GW_HWNDFIRST:
        return sibs[0]
    if cmd == _GW_HWNDLAST:
        return sibs[-1]
    if cmd == _GW_HWNDNEXT:
        return sibs[i + 1] if i + 1 < len(sibs) else 0
    if cmd == _GW_HWNDPREV:
        return sibs[i - 1] if i > 0 else 0
    return 0


def _abs_origin(sysobj, win) -> tuple[int, int]:
    """A window's absolute (screen) top-left in the app's logical coordinate
    space — its own (x,y) plus every ancestor's, walking the parent chain.
    Child positions are relative to the parent's client area (no non-client
    insets are modelled)."""
    x = y = 0
    w = win
    while isinstance(w, Window):
        x += w.x
        y += w.y
        w = sysobj.handles.get(w.parent) if w.parent else None
    return x, y


def _map_point(sysobj, ctx, sign: int) -> int:
    """ClientToScreen (sign +1) / ScreenToClient (sign -1): translate the POINT
    at lpPoint by the window's absolute origin, in place."""
    hwnd, pt_ptr = ctx.args
    win = sysobj.handles.get(hwnd)
    if not isinstance(win, Window):
        return 0
    ox, oy = _abs_origin(sysobj, win)
    seg, off = (pt_ptr >> 16) & 0xFFFF, pt_ptr & 0xFFFF
    px = _signed(ctx.mem.rw(seg, off)) + sign * ox
    py = _signed(ctx.mem.rw(seg, (off + 2) & 0xFFFF)) + sign * oy
    ctx.mem.ww(seg, off, px & 0xFFFF)
    ctx.mem.ww(seg, (off + 2) & 0xFFFF, py & 0xFFFF)
    return 1


def _invalidate(win, rect=None, erase: bool = False) -> None:
    """Real-USER invalidation: union `rect` (client coords; None = whole
    client) into the window's update region, remembering a pending erase.
    BeginPaint validates (clears) it; GetUpdateRgn copies it out.  WAP games
    invalidate each object's OWN rect and read it back through the region —
    collapsing this to a whole-client bool destroyed those rects (SimAnt's
    ribbon buttons / logo halves all redrew at the region box's 0,0).

    The region is a true rect LIST (win.update_rects) with its bounding box
    mirrored in win.update_rect: ValidateRgn must SUBTRACT — SimAnt's map
    scroll validates the scroll-exposed strip out of the pending region so
    the pre-array-shift UpdateWindow doesn't repaint stale tiles over the
    just-scrolled pixels (the 16x16 tile-ghosting bug)."""
    cw, ch = win.client_size
    r = (0, 0, cw, ch) if rect is None else rect
    r = (max(r[0], 0), max(r[1], 0), min(r[2], cw), min(r[3], ch))
    if r[2] <= r[0] or r[3] <= r[1]:
        return
    rects = getattr(win, "update_rects", None)
    if rects is None:                       # incl. windows from old snapshots
        rects = win.update_rects = [win.update_rect] if win.update_rect else []
    rects.append(r)
    win.update_rect = _rects_bbox(rects)
    win.update_erase = win.update_erase or erase
    win.dirty = True


def _validate(win) -> None:
    """Clear the update region (what BeginPaint does)."""
    win.update_rects = []
    win.update_rect = None
    win.update_erase = False
    win.dirty = False


def _validate_rect(win, rect) -> None:
    """Subtract `rect` from the update region (ValidateRgn with a region).
    Each stored rect loses its intersection with `rect`, splitting into up to
    four remainder strips — real USER region subtraction, which SimAnt's map
    scroll relies on to keep the scrolled pixels out of the next repaint."""
    rects = getattr(win, "update_rects", None)
    if rects is None:
        rects = [win.update_rect] if win.update_rect else []
    sl, st, sr, sb = rect
    out = []
    for (l, t, r, b) in rects:
        il, it = max(l, sl), max(t, st)
        ir, ib = min(r, sr), min(b, sb)
        if ir <= il or ib <= it:            # no overlap — keep whole
            out.append((l, t, r, b))
            continue
        if t < it:                          # strip above the hole
            out.append((l, t, r, it))
        if ib < b:                          # strip below
            out.append((l, ib, r, b))
        if l < il:                          # strip left (between it..ib)
            out.append((l, it, il, ib))
        if ir < r:                          # strip right
            out.append((ir, it, r, ib))
    win.update_rects = out
    win.update_rect = _rects_bbox(out)
    if not out:
        win.update_erase = False
        win.dirty = False


def _rects_bbox(rects) -> tuple | None:
    if not rects:
        return None
    return (min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects))


def _apply_paint_clip(surface, rects, before: bytes) -> None:
    """Enforce the BeginPaint clip at EndPaint: keep the freshly-painted pixels
    inside the update `rects`, restore everything outside from the pre-paint
    snapshot `before`.

    No-op if `before` no longer matches the surface's shape — the wndproc can
    resize the client surface between BeginPaint and EndPaint, and the snapshot
    of the old shape cannot be re-laid onto a differently-sized buffer.  In that
    case the wndproc has already repainted the resized surface, so it is left
    as-is (the correct fallback; never crash on a mid-paint resize).

    Copy shape (measured on a SimAnt replay, ~830 calls of avg 25 rects/613
    rows): writable ``frombuffer`` VIEWS over the live bytearrays — no
    ``bytes()`` round-trips — one 2D numpy copy per rect, then one buffer
    write-back.  The original did four full-frame copies (2.0 ms/call under
    PyPy); a pure-bytearray per-row loop pays ~2 us per row slice there
    (1.46 ms/call); this is 2 memcpys + ~25 vectorized rect copies."""
    w, h = surface.w, surface.h
    if len(before) != h * w * 3:
        return
    import numpy as np
    out = bytearray(before)                      # base: the pre-paint frame
    o3 = np.frombuffer(out, dtype=np.uint8).reshape(h, w, 3)          # writable view
    cur = np.frombuffer(surface.pixels, dtype=np.uint8).reshape(h, w, 3)
    for (l, t, r, b) in rects:
        l, t = max(l, 0), max(t, 0)
        r, b = min(r, w), min(b, h)
        if r > l and b > t:
            o3[t:b, l:r] = cur[t:b, l:r]         # keep the painted region
    surface.pixels[:] = out
    surface.touch()


def _fill_window_bg(sysobj, win) -> None:
    """Paint a window's surface with its class background brush.  Applied on
    creation and after every resize (a resize rebuilds the surface as black),
    so areas the app never draws show the class's grey/white — not an
    unpainted black — exactly as the hbrBackground brush would on real USER."""
    from .gdi import class_background_rgb
    rgb = class_background_rgb(sysobj, win.wndclass.h_background)
    if rgb is not None:
        win.surface.fill(rgb)


def _geom(sysobj, handle: int) -> tuple[int, int, int, int]:
    """Resolve any window-like handle (Window, Dialog, control) to its
    (x, y, w, h) geometry — they are all windows in Win16."""
    obj = sysobj.handles.get(handle)
    getter = getattr(obj, "geom_px", None)
    if getter is None:
        raise KeyError(f"handle {handle:04X} is not window-like ({type(obj).__name__})")
    return getter()


def _vk_to_char(vk: int) -> int | None:
    """ASCII character a virtual key produces with no shift state, or None.
    (A minimal US-layout subset — enough for typed input; grows if needed.)"""
    if 0x41 <= vk <= 0x5A:              # 'A'-'Z' VKs -> lowercase
        return vk + 0x20
    if 0x30 <= vk <= 0x39:              # '0'-'9'
        return vk
    return {0x20: 0x20, 0x0D: 0x0D, 0x1B: 0x1B, 0x08: 0x08, 0x09: 0x09}.get(vk)


# Windows 3.1 on standard VGA 640x480: the documented metric values.
# Filled per observed index — extend from the same reference when a new
# index is requested (fail loud otherwise).
# INSTR_PER_MS (the GetTickCount instruction-clock rate) now lives in system.py
# so the timer pump shares it — imported above.

SYSTEM_METRICS = {
    0: 640,     # SM_CXSCREEN
    1: 480,     # SM_CYSCREEN
    2: 16,      # SM_CXVSCROLL
    3: 16,      # SM_CYHSCROLL
    4: 20,      # SM_CYCAPTION (3.1: 19 + 1 border)
    5: 1,       # SM_CXBORDER
    6: 1,       # SM_CYBORDER
    7: 4,       # SM_CXDLGFRAME
    8: 4,       # SM_CYDLGFRAME
    9: 16,      # SM_CYVTHUMB
    10: 16,     # SM_CXHTHUMB
    11: 32,     # SM_CXICON
    12: 32,     # SM_CYICON
    13: 32,     # SM_CXCURSOR
    14: 32,     # SM_CYCURSOR
    15: 18,     # SM_CYMENU
    16: 640,    # SM_CXFULLSCREEN
    17: 460,    # SM_CYFULLSCREEN
    18: 18,     # SM_CYKANJIWINDOW
    19: 0,      # SM_MOUSEPRESENT (set below to 1)
    20: 16,     # SM_CYVSCROLL
    21: 16,     # SM_CXHSCROLL
    22: 0,      # SM_DEBUG
    23: 0,      # SM_SWAPBUTTON
    30: 8,      # SM_CXMINTRACK -> use small defaults
    31: 8,      # SM_CYMINTRACK
    32: 4,      # SM_CXFRAME (sizing border)
    33: 4,      # SM_CYFRAME
    34: 640,    # SM_CXSCREEN (unused dup guard)
    36: 32,     # SM_CXDOUBLECLK
    37: 32,     # SM_CYDOUBLECLK
    38: 8,      # SM_CXICONSPACING
    39: 8,      # SM_CYICONSPACING
    40: 0,      # SM_MENUDROPALIGNMENT
    41: 0,      # SM_PENWINDOWS
    42: 0,      # SM_DBCSENABLED
    43: 3,      # SM_CMOUSEBUTTONS
}
SYSTEM_METRICS[19] = 1      # SM_MOUSEPRESENT — a mouse is present


def _wsprintf_format(ctx: CallContext, fmt: bytes, next_word) -> bytes:
    """The Win16 wsprintf subset: %[-][0][width][l]{d,i,u,x,X,c,s,%}."""
    out = bytearray()
    i = 0
    while i < len(fmt):
        ch = fmt[i]
        if ch != 0x25:                  # '%'
            out.append(ch)
            i += 1
            continue
        i += 1
        spec = ""
        while i < len(fmt) and chr(fmt[i]) in "-0123456789":
            spec += chr(fmt[i])
            i += 1
        long_arg = False
        if i < len(fmt) and fmt[i] in (0x6C, 0x4C):     # 'l'/'L'
            long_arg = True
            i += 1
        conv = chr(fmt[i])
        i += 1
        if conv == "%":
            out.append(0x25)
            continue
        if conv in "di":
            v = next_word() | (next_word() << 16) if long_arg else next_word()
            bits = 32 if long_arg else 16
            if v & (1 << (bits - 1)):
                v -= 1 << bits
            text = str(v)
        elif conv == "u":
            v = next_word() | (next_word() << 16) if long_arg else next_word()
            text = str(v)
        elif conv in "xX":
            v = next_word() | (next_word() << 16) if long_arg else next_word()
            text = format(v, conv)
        elif conv == "c":
            text = chr(next_word() & 0xFF)
        elif conv == "s":
            ptr = next_word() | (next_word() << 16)
            text = ctx.read_string(ptr).decode("latin-1")
        else:
            raise NotImplementedError(f"wsprintf conversion %{spec}{conv}")
        pad_zero = spec.startswith("0")
        left = spec.startswith("-")
        width = int(spec.lstrip("-0") or 0)
        if len(text) < width:
            fill = "0" if pad_zero and not left else " "
            text = text.ljust(width) if left else text.rjust(width, fill)
        out += text.encode("latin-1")
    return bytes(out)


def _write_msg(ctx: CallContext, lpmsg: int, msg: tuple) -> None:
    """MSG (18 bytes): hwnd, message, wParam, lParam, time, pt."""
    seg, off = (lpmsg >> 16) & 0xFFFF, lpmsg & 0xFFFF
    hwnd, message, wparam, lparam, time, pt = msg
    mem = ctx.mem
    mem.ww(seg, off, hwnd)
    mem.ww(seg, off + 2, message)
    mem.ww(seg, off + 4, wparam & 0xFFFF)
    mem.ww(seg, off + 6, lparam & 0xFFFF)
    mem.ww(seg, off + 8, (lparam >> 16) & 0xFFFF)
    mem.ww(seg, off + 10, time & 0xFFFF)
    mem.ww(seg, off + 12, (time >> 16) & 0xFFFF)
    mem.ww(seg, off + 14, pt & 0xFFFF)
    mem.ww(seg, off + 16, (pt >> 16) & 0xFFFF)


def _read_msg(ctx: CallContext, lpmsg: int) -> tuple:
    seg, off = (lpmsg >> 16) & 0xFFFF, lpmsg & 0xFFFF
    mem = ctx.mem
    return (mem.rw(seg, off), mem.rw(seg, off + 2), mem.rw(seg, off + 4),
            mem.rw(seg, off + 6) | (mem.rw(seg, off + 8) << 16),
            mem.rw(seg, off + 10) | (mem.rw(seg, off + 12) << 16),
            mem.rw(seg, off + 14) | (mem.rw(seg, off + 16) << 16))


def _build_createstruct(ctx: CallContext, sys: Win16System, win: Window,
                        lp_param: int, cls_ptr: int, title_ptr: int) -> int:
    """Write a Win16 CREATESTRUCT into a scratch block; returns its far ptr.

    Layout (34 bytes): lpCreateParams(4) hInstance(2) hMenu(2) hwndParent(2)
    cy(2) cx(2) y(2) x(2) style(4) lpszName(4) lpszClass(4) dwExStyle(4).
    """
    seg = sys.machine.alloc_paragraphs(4)
    mem = ctx.mem
    def wl(off, v):
        mem.ww(seg, off, v & 0xFFFF)
        mem.ww(seg, off + 2, (v >> 16) & 0xFFFF)
    wl(0, lp_param)
    mem.ww(seg, 4, win.wndclass.h_instance)
    mem.ww(seg, 6, win.menu)
    mem.ww(seg, 8, win.parent)
    mem.ww(seg, 10, win.h & 0xFFFF)
    mem.ww(seg, 12, win.w & 0xFFFF)
    mem.ww(seg, 14, win.y & 0xFFFF)
    mem.ww(seg, 16, win.x & 0xFFFF)
    wl(18, win.style)
    wl(22, title_ptr)
    wl(26, cls_ptr)
    wl(30, 0)                       # dwExStyle
    return seg << 16


def install(api: ApiRegistry) -> None:
    @api.register("USER", 5, args="word")               # InitApp(hInstance)
    def InitApp(ctx: CallContext) -> int:
        # Creates the task's message queue in real USER; queue state lives in
        # the Python system object here.
        return 1

    @api.register("USER", 179, args="s_word")           # GetSystemMetrics(index)
    def GetSystemMetrics(ctx: CallContext) -> int:
        idx = ctx.args[0]
        if idx not in SYSTEM_METRICS:
            raise NotImplementedError(f"GetSystemMetrics({idx}) — index not in table")
        return SYSTEM_METRICS[idx]

    @api.register("USER", 57, args="ptr")               # RegisterClass(lpWndClass)
    def RegisterClass(ctx: CallContext) -> int:
        sys = _sys(ctx)
        seg, off = (ctx.args[0] >> 16) & 0xFFFF, ctx.args[0] & 0xFFFF
        rw = lambda o: ctx.mem.rw(seg, (off + o) & 0xFFFF)
        rl = lambda o: rw(o) | (rw(o + 2) << 16)
        menu_ptr, name_ptr = rl(18), rl(22)
        name = _resource_name(ctx, name_ptr)
        if not isinstance(name, str):
            raise NotImplementedError("RegisterClass with atom class name")
        cls = WndClass(
            name=name, style=rw(0),
            wndproc=((rl(2) >> 16) & 0xFFFF, rl(2) & 0xFFFF),
            cls_extra=rw(6), wnd_extra=rw(8), h_instance=rw(10),
            h_icon=rw(12), h_cursor=rw(14), h_background=rw(16),
            menu_name=_resource_name(ctx, menu_ptr) if menu_ptr else None,
            class_extra=bytearray(rw(6)),
        )
        sys.classes[name] = cls
        sys.handles.add(cls)
        return 1                    # nonzero = registered (real USER returns an atom)

    # Window properties: a per-window string->handle store (USER 24/25/26).
    # The name is a far-pointer string (atoms — segment 0 — not seen yet).
    def _prop_name(ctx: CallContext, ptr: int) -> str | None:
        if (ptr >> 16) & 0xFFFF == 0:       # atom, not a far string pointer
            return f"#atom{ptr & 0xFFFF}"
        return ctx.read_string(ptr).decode("latin-1")

    @api.register("USER", 26, args="word ptr word")     # SetProp(hwnd, name, hData)
    def SetProp(ctx: CallContext) -> int:
        win = _sys(ctx).handles.get(ctx.args[0])
        if not isinstance(win, Window):
            return 0
        win.props[_prop_name(ctx, ctx.args[1])] = ctx.args[2] & 0xFFFF
        return 1                            # nonzero = added

    @api.register("USER", 25, args="word ptr")          # GetProp(hwnd, name)
    def GetProp(ctx: CallContext) -> int:
        win = _sys(ctx).handles.get(ctx.args[0])
        if not isinstance(win, Window):
            return 0
        return win.props.get(_prop_name(ctx, ctx.args[1]), 0)

    @api.register("USER", 24, args="word ptr")          # RemoveProp(hwnd, name)
    def RemoveProp(ctx: CallContext) -> int:
        win = _sys(ctx).handles.get(ctx.args[0])
        if not isinstance(win, Window):
            return 0
        return win.props.pop(_prop_name(ctx, ctx.args[1]), 0)

    @api.register("USER", 50, args="str str")           # FindWindow(class, title)
    def FindWindow(ctx: CallContext) -> int:
        """Locate a top-level window by class name and/or title (NULL = any).
        Apps use it as a single-instance guard right after InitApp; in the
        static single-app model there is never a prior instance, so an honest
        scan of our own window list returns 0 at startup."""
        sys = _sys(ctx)
        cls_ptr, title_ptr = ctx.args
        want_cls = ctx.read_string(cls_ptr).decode("latin-1") if cls_ptr else None
        want_title = ctx.read_string(title_ptr).decode("latin-1") if title_ptr else None
        for win in sys.windows:
            if want_cls is not None and win.wndclass.name != want_cls:
                continue
            if want_title is not None and win.title != want_title:
                continue
            return win.handle
        return 0

    @api.register("USER", 41,                           # CreateWindow(...)
                  args="str str long s_word s_word s_word s_word word word word segptr")
    def CreateWindow(ctx: CallContext) -> int:
        sys = _sys(ctx)
        (cls_ptr, title_ptr, style, x, y, w, h,
         parent, menu, _hinst, lp_param) = ctx.args
        cls_name = _resource_name(ctx, cls_ptr)
        if not isinstance(cls_name, str):
            raise NotImplementedError("CreateWindow with atom class")
        wndclass = sys.classes.get(cls_name)
        if wndclass is None:
            raise NotImplementedError(f"CreateWindow for unregistered class {cls_name!r} "
                                      "(built-in control classes not modelled yet)")
        title = ctx.read_string(title_ptr).decode("latin-1") if title_ptr else ""
        # CW_USEDEFAULT placement: top-left, sized later by the app (evidence
        # will say if the game relies on real shell placement).
        if (x & 0xFFFF) == CW_USEDEFAULT:
            x, y = 0, 0
        if (w & 0xFFFF) == CW_USEDEFAULT:
            w, h = 640, 480
        win = Window(wndclass=wndclass, title=title, style=style & 0xFFFFFFFF,
                     x=_signed(x), y=_signed(y), w=_signed(w), h=_signed(h),
                     parent=parent, menu=menu,
                     extra=bytearray(wndclass.wnd_extra))
        hwnd = sys.handles.add(win)
        sys.windows.append(win)
        # Paint the initial background with the class brush so areas the app
        # never draws show its intended grey/white, not an unpainted black.
        _fill_window_bg(sys, win)
        cs_ptr = _build_createstruct(ctx, sys, win, lp_param, cls_ptr, title_ptr)
        if sys.call_wndproc(win, WM_CREATE, 0, cs_ptr) & 0xFFFF == 0xFFFF:
            sys.windows.remove(win)
            sys.handles.remove(hwnd)
            return 0
        if win.style & WS_VISIBLE:
            win.visible = True
            _invalidate(win, erase=True)
        return hwnd

    @api.register("USER", 64, args="word word s_word s_word word")
    def SetScrollRange(ctx: CallContext) -> int:        # (hwnd, bar, min, max, redraw)
        sys = _sys(ctx)
        hwnd, bar, lo, hi, _redraw = ctx.args
        win = sys.handles.require(hwnd, Window)
        _, _, pos = win.scroll.get(bar, (0, 0, 0))
        win.scroll[bar] = (_signed(lo), _signed(hi), pos)
        return 1

    @api.register("USER", 65, args="word word ptr ptr")  # GetScrollRange
    def GetScrollRange(ctx: CallContext) -> int:         # (hwnd, bar, lpMin, lpMax)
        sys = _sys(ctx)
        hwnd, bar, lp_min, lp_max = ctx.args
        win = sys.handles.get(hwnd)
        lo, hi, _pos = (win.scroll.get(bar, (0, 0, 0))
                        if isinstance(win, Window) else (0, 0, 0))
        for lp, val in ((lp_min, lo), (lp_max, hi)):
            ctx.mem.ww((lp >> 16) & 0xFFFF, lp & 0xFFFF, val & 0xFFFF)
        return 1

    @api.register("USER", 63, args="word word")         # GetScrollPos(hwnd, bar)
    def GetScrollPos(ctx: CallContext) -> int:
        win = _sys(ctx).handles.get(ctx.args[0])
        if not isinstance(win, Window):
            return 0
        return win.scroll.get(ctx.args[1], (0, 0, 0))[2] & 0xFFFF

    @api.register("USER", 62, args="word word s_word word")
    def SetScrollPos(ctx: CallContext) -> int:          # (hwnd, bar, pos, redraw)
        sys = _sys(ctx)
        hwnd, bar, pos, _redraw = ctx.args
        win = sys.handles.require(hwnd, Window)
        lo, hi, old = win.scroll.get(bar, (0, 0, 0))
        win.scroll[bar] = (lo, hi, _signed(pos))
        return old & 0xFFFF

    @api.register("USER", 29, args="word ptr")          # ScreenToClient(hwnd, pt)
    def ScreenToClient(ctx: CallContext) -> int:
        return _map_point(_sys(ctx), ctx, -1)

    @api.register("USER", 30, args="long")              # WindowFromPoint(POINT)
    def WindowFromPoint(ctx: CallContext) -> int:
        # The (screen-space) POINT is passed BY VALUE: x = LOWORD, y = HIWORD.
        # Return the deepest visible window whose absolute rect contains it
        # (a child sits inside its parent, so the most-nested match is what a
        # click hit-tests to); ties broken toward topmost Z (later in the list).
        sys = _sys(ctx)
        pt = ctx.args[0]
        x, y = _signed(pt & 0xFFFF), _signed((pt >> 16) & 0xFFFF)
        best, best_depth = 0, -1
        for w in sys.windows:
            if not isinstance(w, Window) or not w.visible:
                continue
            ox, oy = sys._window_origin(w.handle)
            if not (ox <= x < ox + w.w and oy <= y < oy + w.h):
                continue
            depth, p = 0, w
            while getattr(p, "parent", 0):
                p = sys.handles.get(p.parent)
                if not isinstance(p, Window):
                    break
                depth += 1
            if depth >= best_depth:              # >= so topmost Z wins ties
                best, best_depth = w.handle, depth
        return best

    @api.register("USER", 33, args="word ptr")          # GetClientRect(hwnd, rect)
    def GetClientRect(ctx: CallContext) -> int:
        _x, _y, w, h = _geom(_sys(ctx), ctx.args[0])
        seg, off = (ctx.args[1] >> 16) & 0xFFFF, ctx.args[1] & 0xFFFF
        for i, v in enumerate((0, 0, w, h)):
            ctx.mem.ww(seg, (off + 2 * i) & 0xFFFF, v & 0xFFFF)
        return 1

    @api.register("USER", 32, args="word ptr")          # GetWindowRect(hwnd, rect)
    def GetWindowRect(ctx: CallContext) -> int:
        x, y, w, h = _geom(_sys(ctx), ctx.args[0])
        seg, off = (ctx.args[1] >> 16) & 0xFFFF, ctx.args[1] & 0xFFFF
        for i, v in enumerate((x, y, x + w, y + h)):
            ctx.mem.ww(seg, (off + 2 * i) & 0xFFFF, v & 0xFFFF)
        return 1

    @api.register("USER", 160, args="word")             # DrawMenuBar(hwnd)
    def DrawMenuBar(ctx: CallContext) -> int:
        # Menu rendering is host-side UI; the state store is already current.
        return 1

    @api.register("USER", 157, args="word")             # GetMenu(hwnd)
    def GetMenu(ctx: CallContext) -> int:
        sys = _sys(ctx)
        win = sys.handles.require(ctx.args[0], Window)
        if win.menu_obj is None:
            win.menu_obj = Menu(win.wndclass.menu_name)
            sys.handles.add(win.menu_obj)
        return win.menu_obj.handle

    # -- programmatic menu construction (SimAnt builds its menus in code) -----
    MF_GRAYED, MF_DISABLED, MF_CHECKED = 0x0001, 0x0002, 0x0008
    MF_POPUP, MF_SEPARATOR, MF_BYPOSITION = 0x0010, 0x0800, 0x0400

    def _menu_item(ctx, sys, flags, id_new, content_ptr):
        from .objects import MenuItem
        if flags & MF_SEPARATOR:
            return MenuItem(flags=flags, id=0)
        if flags & MF_POPUP:
            sub = sys.handles.get(id_new)
            text = ctx.read_string(content_ptr).decode("latin-1") if content_ptr else ""
            return MenuItem(flags=flags, id=id_new, text=text,
                            submenu=sub if isinstance(sub, Menu) else None)
        text = ctx.read_string(content_ptr).decode("latin-1") if content_ptr else ""
        item = MenuItem(flags=flags, id=id_new, text=text)
        return item

    @api.register("USER", 151)                          # CreateMenu()
    def CreateMenu(ctx: CallContext) -> int:
        sys = _sys(ctx)
        menu = Menu(None)
        sys.handles.add(menu)
        return menu.handle

    @api.register("USER", 415)                          # CreatePopupMenu()
    def CreatePopupMenu(ctx: CallContext) -> int:
        # An empty menu meant to be attached with AppendMenu(MF_POPUP) or shown
        # by TrackPopupMenu.  Real USER distinguishes a popup from a menu bar by
        # a style bit it uses when it DRAWS; our Menu is a pure item tree and
        # whoever renders it (the host chrome) derives bar-vs-popup from where
        # it is attached, so this is CreateMenu's twin rather than a new object.
        #
        # Observed contract (SimAnt's _InitMenu): create, fill with AppendMenu,
        # then AppendMenu(hBar, MF_POPUP, hPopup, title) — the submenu path of
        # its programmatic menu builder.
        sys = _sys(ctx)
        menu = Menu(None)
        sys.handles.add(menu)
        return menu.handle

    @api.register("USER", 152, args="word")             # DestroyMenu(hMenu)
    def DestroyMenu(ctx: CallContext) -> int:
        sys = _sys(ctx)
        if isinstance(sys.handles.get(ctx.args[0]), Menu):
            sys.handles.remove(ctx.args[0])
        return 1

    @api.register("USER", 411, args="word word word ptr")  # AppendMenu
    def AppendMenu(ctx: CallContext) -> int:            # (hMenu, flags, id, content)
        sys = _sys(ctx)
        menu = sys.handles.require(ctx.args[0], Menu)
        item = _menu_item(ctx, sys, ctx.args[1], ctx.args[2], ctx.args[3])
        menu.items.append(item)
        if not (item.flags & (MF_POPUP | MF_SEPARATOR)):
            menu.item_flags[item.id] = item.flags & (MF_GRAYED | MF_DISABLED | MF_CHECKED)
        return 1

    @api.register("USER", 410, args="word word word word ptr")  # InsertMenu
    def InsertMenu(ctx: CallContext) -> int:            # (hMenu, pos, flags, id, content)
        sys = _sys(ctx)
        menu = sys.handles.require(ctx.args[0], Menu)
        pos, flags = ctx.args[1], ctx.args[2]
        item = _menu_item(ctx, sys, flags, ctx.args[3], ctx.args[4])
        if flags & MF_BYPOSITION:
            index = pos if 0 <= pos <= len(menu.items) else len(menu.items)
        else:                                           # insert before the item with this id
            index = next((i for i, it in enumerate(menu.items) if it.id == pos),
                         len(menu.items))
        menu.items.insert(index, item)
        if not (item.flags & (MF_POPUP | MF_SEPARATOR)):
            menu.item_flags[item.id] = item.flags & (MF_GRAYED | MF_DISABLED | MF_CHECKED)
        return 1

    @api.register("USER", 159, args="word word")        # GetSubMenu(hMenu, pos)
    def GetSubMenu(ctx: CallContext) -> int:
        sys = _sys(ctx)
        menu = sys.handles.require(ctx.args[0], Menu)
        pos = ctx.args[1]
        if 0 <= pos < len(menu.items) and menu.items[pos].submenu is not None:
            return menu.items[pos].submenu.handle
        return 0

    @api.register("USER", 126, args="word word word")   # InvalidateRgn(hwnd, hrgn, erase)
    def InvalidateRgn(ctx: CallContext) -> int:
        sys = _sys(ctx)
        win = sys.handles.get(ctx.args[0])
        if not isinstance(win, Window):
            return 0
        hrgn, erase = ctx.args[1], bool(ctx.args[2])
        if hrgn == 0:
            _invalidate(win, None, erase=erase)         # whole client
        else:
            rgn = sys.handles.get(hrgn)
            if isinstance(rgn, Region):
                _invalidate(win, (rgn.x1, rgn.y1, rgn.x2, rgn.y2), erase=erase)
        return 1

    @api.register("USER", 128, args="word word")        # ValidateRgn(hwnd, hrgn)
    def ValidateRgn(ctx: CallContext) -> int:
        sys = _sys(ctx)
        win = sys.handles.get(ctx.args[0])
        if not isinstance(win, Window):
            return 0
        hrgn = ctx.args[1]
        if hrgn == 0:                                   # NULL region = whole window
            _validate(win)
            return 1
        rgn = sys.handles.get(hrgn)
        if isinstance(rgn, Region):
            # Real region subtraction.  SimAnt's map scroll validates the
            # scroll-exposed strip out of the pending region, so the
            # UpdateWindow it issues BEFORE shifting its screen-tile arrays
            # repaints only the other pending rects — subtract-as-covers-all
            # (the old single-bbox approximation) left the whole union
            # pending and stale tiles got stamped over the scrolled pixels.
            _validate_rect(win, (rgn.x1, rgn.y1, rgn.x2, rgn.y2))
        return 1

    @api.register("USER", 61, args="word s_word s_word ptr ptr")  # ScrollWindow
    def ScrollWindow(ctx: CallContext) -> int:          # (hwnd, dx, dy, rc, clip)
        # Shift the client pixels by (dx, dy) and invalidate the exposed strips
        # for repaint — the real USER behaviour SimAnt's map-scroll relies on.
        # The optional scroll/clip rects are treated as the whole client (the
        # game scrolls the full map window); refine if a caller needs sub-rects.
        import numpy as np
        sys = _sys(ctx)
        win = sys.handles.get(ctx.args[0])
        if not isinstance(win, Window):
            return 0
        dx, dy = _signed(ctx.args[1]), _signed(ctx.args[2])
        surf = win.surface
        w, h = surf.w, surf.h
        cw, ch = w - abs(dx), h - abs(dy)
        if cw > 0 and ch > 0 and (dx or dy):
            arr = np.frombuffer(surf.pixels, dtype=np.uint8).reshape(h, w, 3)
            sx, sy = max(0, -dx), max(0, -dy)
            dxo, dyo = max(0, dx), max(0, dy)
            moved = arr[sy:sy + ch, sx:sx + cw].copy()
            arr[dyo:dyo + ch, dxo:dxo + cw] = moved
            surf.touch()
        if dy > 0:
            _invalidate(win, (0, 0, w, dy), erase=True)
        elif dy < 0:
            _invalidate(win, (0, h + dy, w, h), erase=True)
        if dx > 0:
            _invalidate(win, (0, 0, dx, h), erase=True)
        elif dx < 0:
            _invalidate(win, (w + dx, 0, w, h), erase=True)
        return 1

    @api.register("USER", 244, args="ptr ptr")          # EqualRect(lprc1, lprc2)
    def EqualRect(ctx: CallContext) -> int:
        def rd(p):
            seg, off = (p >> 16) & 0xFFFF, p & 0xFFFF
            return tuple(_signed(ctx.mem.rw(seg, (off + 2 * i) & 0xFFFF))
                         for i in range(4))
        return 1 if rd(ctx.args[0]) == rd(ctx.args[1]) else 0

    @api.register("USER", 37, args="word ptr")          # SetWindowText(hwnd, lpsz)
    def SetWindowText(ctx: CallContext) -> int:
        sys = _sys(ctx)
        win = sys.handles.get(ctx.args[0])
        if isinstance(win, Window):
            ptr = ctx.args[1]
            win.title = ctx.read_string(ptr).decode("latin-1") if ptr else ""
        return 1

    @api.register("USER", 263, args="word")             # GetMenuItemCount(hMenu)
    def GetMenuItemCount(ctx: CallContext) -> int:
        menu = _sys(ctx).handles.get(ctx.args[0])
        return len(menu.items) if isinstance(menu, Menu) else 0xFFFF   # -1 err

    def _remove_menu_item(sys, hmenu, item, flags) -> int:
        # Shared by RemoveMenu/DeleteMenu: drop the item at position `item`
        # (MF_BYPOSITION) or with command id `item` (MF_BYCOMMAND, the default).
        menu = sys.handles.get(hmenu)
        if not isinstance(menu, Menu):
            return 0
        MF_BYPOSITION = 0x0400
        if flags & MF_BYPOSITION:
            if 0 <= item < len(menu.items):
                menu.items.pop(item)
                return 1
            return 0
        for i, it in enumerate(menu.items):
            if it.id == item:
                menu.items.pop(i)
                return 1
        return 0

    @api.register("USER", 412, args="word word word")   # RemoveMenu(hMenu, item, flags)
    def RemoveMenu(ctx: CallContext) -> int:
        # SimAnt strips SC_* items from a game window's system menu (via
        # GetSystemMenu) to make it non-resizable.  We don't render the system
        # menu, so removes on our (empty) copy are harmless no-ops.
        return _remove_menu_item(_sys(ctx), ctx.args[0], ctx.args[1], ctx.args[2])

    @api.register("USER", 413, args="word word word")   # DeleteMenu(hMenu, item, flags)
    def DeleteMenu(ctx: CallContext) -> int:
        # Like RemoveMenu but also destroys a popup submenu; for our flat model
        # the drop is identical (the submenu Menu is left for GC).
        return _remove_menu_item(_sys(ctx), ctx.args[0], ctx.args[1], ctx.args[2])

    @api.register("USER", 156, args="word word")        # GetSystemMenu(hwnd, bRevert)
    def GetSystemMenu(ctx: CallContext) -> int:
        # Returns a handle to the window's system menu for modification (the
        # title-bar/close menu).  SimAnt calls GetSystemMenu(gameWindow, FALSE)
        # on entering a game to customise it, then AppendMenu/EnableMenuItem on
        # the result — so we hand back a real, lazily-created Menu it can edit
        # (we don't render the system menu, but the game must be able to build
        # it).  bRevert=TRUE resets to the default and returns NULL, per USER.
        # (Ordinal 156 is GetSystemMenu, NOT GetSubMenu — that is 159; the swap
        # fail-loud-crashed the game the instant Quick Game started.)
        sys = _sys(ctx)
        win = sys.handles.get(ctx.args[0])
        if not isinstance(win, Window):
            return 0
        if ctx.args[1]:                                 # bRevert: reset to default
            if win.sysmenu_obj is not None:
                sys.handles.remove(win.sysmenu_obj.handle)
                win.sysmenu_obj = None
            return 0
        if win.sysmenu_obj is None:
            win.sysmenu_obj = Menu(None)
            sys.handles.add(win.sysmenu_obj)
        return win.sysmenu_obj.handle

    @api.register("USER", 158, args="word word")        # SetMenu(hwnd, hMenu)
    def SetMenu(ctx: CallContext) -> int:
        sys = _sys(ctx)
        win = sys.handles.require(ctx.args[0], Window)
        menu = sys.handles.get(ctx.args[1])
        win.menu_obj = menu if isinstance(menu, Menu) else None
        _invalidate(win, erase=True)
        return 1

    @api.register("USER", 154, args="word word word")   # CheckMenuItem(menu, id, flags)
    def CheckMenuItem(ctx: CallContext) -> int:
        sys = _sys(ctx)
        menu = sys.handles.require(ctx.args[0], Menu)
        item, flags = ctx.args[1], ctx.args[2]
        old = menu.item_flags.get(item, 0)
        menu.item_flags[item] = (old & ~0x0008) | (flags & 0x0008)  # MF_CHECKED
        return old & 0x0008

    @api.register("USER", 155, args="word word word")   # EnableMenuItem(menu, id, flags)
    def EnableMenuItem(ctx: CallContext) -> int:
        sys = _sys(ctx)
        menu = sys.handles.require(ctx.args[0], Menu)
        item, flags = ctx.args[1], ctx.args[2]
        old = menu.item_flags.get(item, 0)
        menu.item_flags[item] = (old & ~0x000B) | (flags & 0x0003)  # MF_GRAYED|DISABLED
        return old & 0x0003

    @api.register("USER", 414, args="word word word word segptr")
    def ModifyMenu(ctx: CallContext) -> int:            # (menu, pos, flags, id, content)
        sys = _sys(ctx)
        menu = sys.handles.require(ctx.args[0], Menu)
        _pos, flags, new_id, content = ctx.args[1:]
        menu.item_flags[new_id] = flags & 0x000B
        # MF_BITMAP (0x0004): content is a bitmap handle in the low word — the
        # game replaces a text item with a bitmap (the ScreenSculptor Shape
        # menu).  Record it so the host can render the real icon.
        if flags & 0x0004:
            menu.item_bitmaps[new_id] = content & 0xFFFF
        else:
            menu.item_bitmaps.pop(new_id, None)
        return 1

    @api.register("USER", 250, args="word word word")   # GetMenuState(menu, id, flags)
    def GetMenuState(ctx: CallContext) -> int:
        sys = _sys(ctx)
        menu = sys.handles.require(ctx.args[0], Menu)
        return menu.item_flags.get(ctx.args[1], 0)

    @api.register("USER", 10, args="word word word segptr")
    def SetTimer(ctx: CallContext) -> int:              # (hwnd, id, ms, proc)
        sys = _sys(ctx)
        hwnd, timer_id, ms, proc = ctx.args
        key = (hwnd, timer_id)
        sys.timers[key] = max(ms, 1)
        sys.timer_due[key] = sys.clock_ms + max(ms, 1)
        # A TimerProc (far callback) is delivered by DispatchMessage calling the
        # proc, not the wndproc — its WM_TIMER carries the proc in lParam.
        if proc:
            sys.timer_procs[key] = proc
        else:
            sys.timer_procs.pop(key, None)
        return timer_id

    @api.register("USER", 12, args="word word")         # KillTimer(hwnd, id)
    def KillTimer(ctx: CallContext) -> int:
        sys = _sys(ctx)
        key = (ctx.args[0], ctx.args[1])
        sys.timer_due.pop(key, None)
        sys.timer_procs.pop(key, None)
        return 1 if sys.timers.pop(key, None) is not None else 0

    @api.register("USER", 108, args="ptr word word word")
    def GetMessage(ctx: CallContext) -> int:            # (lpmsg, hwnd, min, max)
        sys = _sys(ctx)
        lpmsg, hwnd_filter, lo, hi = ctx.args
        if hwnd_filter or lo or hi:
            raise NotImplementedError("GetMessage with hwnd/range filter")
        msg = sys.get_message()
        if msg is None:
            _write_msg(ctx, lpmsg, (0, 0x0012, sys.quit_posted or 0, 0,
                                    sys.clock_ms, 0))    # WM_QUIT
            return 0
        _write_msg(ctx, lpmsg, msg)
        return 1

    @api.register("USER", 109, args="ptr word word word word")
    def PeekMessage(ctx: CallContext) -> int:
        # (lpMsg, hWnd, wMsgFilterMin, wMsgFilterMax, wRemoveMsg).  SimAnt's
        # main loop peeks for mouse messages (0x200..0x209) with PM_REMOVE.
        PM_REMOVE = 0x0001
        sys = _sys(ctx)
        lpmsg, hwnd_filter, lo, hi, remove = ctx.args
        msg = sys.peek_message(hwnd_filter, lo, hi, bool(remove & PM_REMOVE))
        if msg is None:
            return 0
        _write_msg(ctx, lpmsg, msg)
        return 1

    @api.register("USER", 113, args="ptr")              # TranslateMessage(lpmsg)
    def TranslateMessage(ctx: CallContext) -> int:
        # Post a WM_CHAR for a WM_KEYDOWN whose virtual key has an ASCII form
        # (real USER does this via the keyboard layout); arrows/F-keys produce
        # no character.
        sys = _sys(ctx)
        hwnd, message, wparam, lparam, _t, _pt = _read_msg(ctx, ctx.args[0])
        if message != 0x0100:                            # WM_KEYDOWN
            return 0
        ch = _vk_to_char(wparam)
        if ch is None:
            return 0
        sys.post_message(hwnd, 0x0102, ch, lparam)       # WM_CHAR
        return 1

    @api.register("USER", 178, args="word word ptr")    # TranslateAccelerator
    def TranslateAccelerator(ctx: CallContext) -> int:   # (hwnd, haccel, lpmsg)
        from .objects import AccelTable
        sys = _sys(ctx)
        hwnd, haccel, lpmsg = ctx.args
        accel = sys.handles.get(haccel)
        if not isinstance(accel, AccelTable):
            return 0
        _hmsg, message, wparam, _lp, _t, _pt = _read_msg(ctx, lpmsg)
        if message not in (0x0100, 0x0102):              # WM_KEYDOWN / WM_CHAR
            return 0
        win = sys.handles.get(hwnd)
        if not isinstance(win, Window):
            return 0
        for flags, event, cmd_id in accel.entries:
            fvirt = flags & 0x01
            want = 0x0100 if fvirt else 0x0102           # virtkey vs ASCII char
            if message != want or event != wparam:
                continue
            # WM_COMMAND from an accelerator: HIWORD(lParam)=1, LOWORD=0.
            sys.call_wndproc(win, 0x0111, cmd_id, 0x00010000)
            return 1
        return 0

    @api.register("USER", 114, args="ptr", ret="long")  # DispatchMessage(lpmsg)
    def DispatchMessage(ctx: CallContext) -> int:
        sys = _sys(ctx)
        hwnd, msg, wparam, lparam, _t, _pt = _read_msg(ctx, ctx.args[0])
        # WM_TIMER carrying a TimerProc (lParam != 0): call the proc, not the
        # wndproc — TimerProc(hwnd, WM_TIMER, idEvent, dwTime).  This is how a
        # SetTimer callback is delivered on real Windows, and SimAnt's ~59fps
        # sim tick depends on it (its wndproc hangs on WM_TIMER).
        if msg == 0x0113 and lparam:
            from win16.callback import call_far
            from win16.loader import THUNK_SEG
            dwtime = sys.tick_count()
            seg, off = (lparam >> 16) & 0xFFFF, lparam & 0xFFFF
            ax, dx = call_far(ctx.cpu, THUNK_SEG, seg, off,
                              [hwnd, msg, wparam,
                               (dwtime >> 16) & 0xFFFF, dwtime & 0xFFFF],
                              max_steps=sys.callback_max_steps,
                              yield_check=sys.yield_check)
            return (dx << 16) | ax
        win = sys.handles.get(hwnd)
        if not isinstance(win, Window):
            return 0
        return sys.call_wndproc(win, msg, wparam, lparam)

    @api.register("USER", 404, args="word segstr ptr")  # GetClassInfo
    def GetClassInfo(ctx: CallContext) -> int:          # (hInst, name, lpWndClass)
        sys = _sys(ctx)
        name = _resource_name(ctx, ctx.args[1])
        cls = sys.classes.get(name) if isinstance(name, str) else None
        if cls is None:
            return 0
        seg, off = (ctx.args[2] >> 16) & 0xFFFF, ctx.args[2] & 0xFFFF
        mem = ctx.mem
        mem.ww(seg, off, cls.style)
        mem.ww(seg, off + 2, cls.wndproc[1])
        mem.ww(seg, off + 4, cls.wndproc[0])
        for i, v in enumerate((cls.cls_extra, cls.wnd_extra, cls.h_instance,
                               cls.h_icon, cls.h_cursor, cls.h_background)):
            mem.ww(seg, off + 6 + 2 * i, v)
        mem.ww(seg, off + 18, 0)        # lpszMenuName: not read back so far —
        mem.ww(seg, off + 20, 0)        # NULL until evidence demands the ptr
        mem.ww(seg, off + 22, 0)
        mem.ww(seg, off + 24, 0)
        return 1

    # GetClassWord/SetClassWord: negative index = a WNDCLASS field (GCW_*);
    # non-negative index = a WORD in the class's cbClsExtra bytes.  SimAnt reads
    # these while hit-testing a right-click.
    _GCW = {-10: "h_background", -12: "h_cursor", -14: "h_icon",
            -16: "h_instance", -18: "wnd_extra", -20: "cls_extra", -26: "style"}

    @api.register("USER", 129, args="word s_word")      # GetClassWord(hwnd, index)
    def GetClassWord(ctx: CallContext) -> int:
        win = _sys(ctx).handles.get(ctx.args[0])
        if not isinstance(win, Window):
            return 0
        cls = win.wndclass
        idx = _signed(ctx.args[1])
        if idx in _GCW:
            return getattr(cls, _GCW[idx]) & 0xFFFF
        if idx >= 0 and idx + 2 <= len(cls.class_extra):
            return cls.class_extra[idx] | (cls.class_extra[idx + 1] << 8)
        return 0

    @api.register("USER", 130, args="word s_word word")  # SetClassWord(hwnd,index,val)
    def SetClassWord(ctx: CallContext) -> int:
        win = _sys(ctx).handles.get(ctx.args[0])
        if not isinstance(win, Window):
            return 0
        cls = win.wndclass
        idx, val = _signed(ctx.args[1]), ctx.args[2] & 0xFFFF
        if idx in _GCW:
            old = getattr(cls, _GCW[idx]) & 0xFFFF
            setattr(cls, _GCW[idx], val)
            return old
        if idx >= 0 and idx + 2 <= len(cls.class_extra):
            old = cls.class_extra[idx] | (cls.class_extra[idx + 1] << 8)
            cls.class_extra[idx] = val & 0xFF
            cls.class_extra[idx + 1] = (val >> 8) & 0xFF
            return old
        return 0

    @api.register("USER", 403, args="str word")         # UnregisterClass(name, hInst)
    def UnregisterClass(ctx: CallContext) -> int:
        sys = _sys(ctx)
        name = _resource_name(ctx, ctx.args[0])
        cls = sys.classes.pop(name, None) if isinstance(name, str) else None
        if cls is None:
            return 0
        sys.handles.remove(cls.handle)
        return 1

    @api.register("USER", 6, args="word")               # PostQuitMessage(code)
    def PostQuitMessage(ctx: CallContext) -> int:
        _sys(ctx).quit_posted = ctx.args[0]
        return 0

    @api.register("USER", 110, args="word word word long")
    def PostMessage(ctx: CallContext) -> int:           # (hwnd, msg, wp, lp)
        sys = _sys(ctx)
        sys.post_message(ctx.args[0], ctx.args[1], ctx.args[2], ctx.args[3])
        return 1

    @api.register("USER", 56, args="word word word word word word")
    def MoveWindow(ctx: CallContext) -> int:            # (hwnd, x, y, w, h, repaint)
        sys = _sys(ctx)
        hwnd, x, y, w, h, repaint = ctx.args
        obj = sys.handles.get(hwnd)
        if not isinstance(obj, Window):
            # A dialog (or control): record the requested position; final
            # placement is host-managed (dialogs are centered by the host).
            if obj is not None and hasattr(obj, "x"):
                obj.x, obj.y = _signed(x), _signed(y)
            return 1
        win = obj
        win.x, win.y = _signed(x), _signed(y)
        resized = (win.w, win.h) != (_signed(w), _signed(h))
        win.w, win.h = _signed(w), _signed(h)
        if resized:
            win._surface = None                          # client surface rebuilds
            _fill_window_bg(sys, win)
            cw, ch = win.client_size
            sys.call_wndproc(win, WM_SIZE, 0, ((ch & 0xFFFF) << 16) | (cw & 0xFFFF))
        sys.call_wndproc(win, WM_MOVE, 0,
                         ((win.y & 0xFFFF) << 16) | (win.x & 0xFFFF))
        if repaint:
            _invalidate(win, erase=True)
        return 1

    @api.register("USER", 232,
                  args="word word word word word word word")
    def SetWindowPos(ctx: CallContext) -> int:
        # (hwnd, hwndInsertAfter, x, y, cx, cy, flags).  SimAnt sizes its child
        # panels (RibbonWindow etc.) inside the main frame with this — the
        # "windows within a window" layout.  Z-order (hwndInsertAfter) is
        # host-managed; we honour the geometry + NOMOVE/NOSIZE/SHOW/HIDE flags.
        SWP_NOSIZE, SWP_NOMOVE = 0x0001, 0x0002
        SWP_NOREDRAW, SWP_SHOWWINDOW, SWP_HIDEWINDOW = 0x0008, 0x0040, 0x0080
        sys = _sys(ctx)
        hwnd, _after, x, y, cx, cy, flags = ctx.args
        win = sys.handles.get(hwnd)
        if not isinstance(win, Window):
            if win is not None and hasattr(win, "x") and not (flags & SWP_NOMOVE):
                win.x, win.y = _signed(x), _signed(y)
            return 1
        resized = False
        if not (flags & SWP_NOSIZE):
            resized = (win.w, win.h) != (_signed(cx), _signed(cy))
            win.w, win.h = _signed(cx), _signed(cy)
        moved = False
        if not (flags & SWP_NOMOVE):
            moved = (win.x, win.y) != (_signed(x), _signed(y))
            win.x, win.y = _signed(x), _signed(y)
        if flags & SWP_SHOWWINDOW:
            win.visible = True
        elif flags & SWP_HIDEWINDOW:
            win.visible = False
        if resized:
            win._surface = None                          # client surface rebuilds
            _fill_window_bg(sys, win)
            cw, ch = win.client_size
            sys.call_wndproc(win, WM_SIZE, 0, ((ch & 0xFFFF) << 16) | (cw & 0xFFFF))
        if moved:
            sys.call_wndproc(win, WM_MOVE, 0,
                             ((win.y & 0xFFFF) << 16) | (win.x & 0xFFFF))
        if (resized or moved) and not (flags & SWP_NOREDRAW):
            _invalidate(win, erase=True)
        return 1

    @api.register("USER", 237, args="word word word")   # GetUpdateRgn
    def GetUpdateRgn(ctx: CallContext) -> int:           # (hwnd, hrgn, bErase)
        # Copy the window's ACCUMULATED update region (the union of invalidated
        # rects, in client coords) into hrgn, returning the region type.  Does
        # NOT validate (only BeginPaint does).  SimAnt's WAP engine invalidates
        # each object's own rect and reads it back through this region — the
        # rects must round-trip, not collapse to the whole client.
        NULLREGION, SIMPLEREGION = 1, 2
        sys = _sys(ctx)
        win = sys.handles.get(ctx.args[0])
        rgn = sys.handles.get(ctx.args[1])
        if not isinstance(rgn, Region):
            return 0                                     # ERROR (bad region)
        if isinstance(win, Window) and win.update_rect is not None:
            rgn.x1, rgn.y1, rgn.x2, rgn.y2 = win.update_rect
            return SIMPLEREGION
        rgn.x1 = rgn.y1 = rgn.x2 = rgn.y2 = 0
        return NULLREGION

    @api.register("USER", 229, args="word")             # GetTopWindow(hwnd)
    def GetTopWindow(ctx: CallContext) -> int:
        # The child window at the TOP of the parent's Z-order, or 0 if it has no
        # children.  SimAnt wraps this as _MyGetTopWindow (per SIMANTW.SYM).  Our
        # window list is draw order (last = drawn last = topmost), so top-to-
        # bottom Z-order is the reversed list; the top child is its first entry.
        sys = _sys(ctx)
        kids = _z_children(sys, ctx.args[0])
        return kids[0].handle if kids else 0

    @api.register("USER", 262, args="word word")        # GetWindow(hwnd, cmd)
    def GetWindow(ctx: CallContext) -> int:
        return _get_window(_sys(ctx), ctx.args[0], ctx.args[1])

    @api.register("USER", 230, args="word word")        # GetNextWindow(hwnd,flag)
    def GetNextWindow(ctx: CallContext) -> int:
        # GetNextWindow is GetWindow restricted to GW_HWNDNEXT(2)/GW_HWNDPREV(3);
        # SimAnt walks a parent's children with it (after GetTopWindow).
        return _get_window(_sys(ctx), ctx.args[0], ctx.args[1])

    @api.register("USER", 55, args="word ptr long")     # EnumChildWindows(hwnd, proc, lp)
    def EnumChildWindows(ctx: CallContext) -> int:
        # Call lpEnumFunc(hwndChild, lParam) for each child of hWndParent in
        # top-to-bottom Z-order, stopping early if the callback returns FALSE.
        # The callback is FAR PASCAL EnumChildProc(HWND, LPARAM) — the same
        # convention DispatchMessage uses to deliver a TimerProc.  Our window
        # tree is flat (SimAnt's children are leaf control windows), so this
        # enumerates immediate children; a nested child would need recursion.
        sys = _sys(ctx)
        hwnd, proc, lparam = ctx.args
        from win16.callback import call_far
        from win16.loader import THUNK_SEG
        seg, off = (proc >> 16) & 0xFFFF, proc & 0xFFFF
        for child in _z_children(sys, hwnd):
            ax, _dx = call_far(ctx.cpu, THUNK_SEG, seg, off,
                               [child.handle, (lparam >> 16) & 0xFFFF,
                                lparam & 0xFFFF],
                               max_steps=sys.callback_max_steps,
                               yield_check=sys.yield_check)
            if (ax & 0xFFFF) == 0:              # callback returned FALSE -> stop
                break
        return 1                                # documented non-zero success

    @api.register("USER", 45, args="word")              # BringWindowToTop(hwnd)
    def BringWindowToTop(ctx: CallContext) -> int:
        # Raise the window to the top of the z-order.  Draw order is the window
        # list order (later = on top), so move it to the end.
        sys = _sys(ctx)
        win = sys.handles.get(ctx.args[0])
        if not isinstance(win, Window):
            return 0
        if win in sys.windows:
            sys.windows.remove(win)
            sys.windows.append(win)
        return 1

    @api.register("USER", 49, args="word")              # IsWindowVisible(hwnd)
    def IsWindowVisible(ctx: CallContext) -> int:
        win = _sys(ctx).handles.get(ctx.args[0])
        return 1 if isinstance(win, Window) and win.visible else 0

    @api.register("USER", 42, args="word word")         # ShowWindow(hwnd, cmd)
    def ShowWindow(ctx: CallContext) -> int:
        sys = _sys(ctx)
        win = sys.handles.require(ctx.args[0], Window)
        cmd = ctx.args[1]
        was = win.visible
        win.visible = cmd != 0                          # 0 = SW_HIDE
        # SW_SHOWMAXIMIZED / SW_MAXIMIZE (3): grow the frame to the whole screen
        # and re-fire WM_SIZE so the app re-lays-out to the MAXIMIZED client.
        # SimAnt is resolution-adaptive — it maximizes its top-level frame on
        # show and sizes RibbonWindow / the root panel from the resulting client
        # rect.  Ignoring the command left it laid out to the un-maximized create
        # size (627 wide) instead of the full screen (why it looked smaller than
        # otvdm, which honours the maximize).  Only a real top-level frame
        # maximizes; child windows keep their given rect.
        if cmd == 3 and win.parent == 0 and not win.maximized:
            win.restore_rect = (win.x, win.y, win.w, win.h)
            win.maximized = True
            win.x, win.y = 0, 0
            win.w, win.h = SYSTEM_METRICS[0], SYSTEM_METRICS[1]
            win._surface = None                          # client surface rebuilds
            _fill_window_bg(sys, win)
            SIZE_MAXIMIZED = 2
            cw, ch = win.client_size
            sys.call_wndproc(win, WM_SIZE, SIZE_MAXIMIZED,
                             ((ch & 0xFFFF) << 16) | (cw & 0xFFFF))
        if win.visible and not was:
            _invalidate(win, erase=True)
        return 1 if was else 0

    @api.register("USER", 81, args="word ptr word")     # FillRect(hdc, lpRect, hBrush)
    def FillRect(ctx: CallContext) -> int:
        from .gdi import (_dc_surface, _fill_rect, brush_object_rgb,
                          dc_palette_entries)
        sys = _sys(ctx)
        hdc, rc_ptr, hbrush = ctx.args
        dst = _dc_surface(sys, hdc)
        if dst is None:
            return 0
        seg, off = (rc_ptr >> 16) & 0xFFFF, rc_ptr & 0xFFFF
        r = [_signed(ctx.mem.rw(seg, (off + 2 * i) & 0xFFFF)) for i in range(4)]
        pal = dc_palette_entries(sys, sys.handles.get(hdc))
        rgb = brush_object_rgb(sys.handles.get(hbrush), pal)
        if rgb is not None:                             # hollow brush = no-op
            _fill_rect(dst, r[0], r[1], r[2] - r[0], r[3] - r[1], rgb)
        return 1

    @api.register("USER", 82, args="word ptr")          # InvertRect(hdc, lpRect)
    def InvertRect(ctx: CallContext) -> int:
        # DSTINVERT in the 16-colour device's palette-index domain (idx ^ 0xF),
        # NOT a per-channel RGB invert: on the original 4-bit device inverting
        # grey (128,128,128) yields light grey (192,192,192) — a per-channel
        # invert yields the invisible (127,127,127).  See invert_rect_16color.
        from .gdi import _dc_surface, invert_rect_16color
        sys = _sys(ctx)
        hdc, rc_ptr = ctx.args
        dst = _dc_surface(sys, hdc)
        if dst is None:
            return 0
        seg, off = (rc_ptr >> 16) & 0xFFFF, rc_ptr & 0xFFFF
        l, t, r, b = (_signed(ctx.mem.rw(seg, (off + 2 * i) & 0xFFFF))
                      for i in range(4))
        invert_rect_16color(dst, l, t, r, b)
        return 1

    @api.register("USER", 124, args="word")             # UpdateWindow(hwnd)
    def UpdateWindow(ctx: CallContext) -> int:
        """Flush a pending update: if the window has an invalid region, send
        WM_PAINT to its proc synchronously (BeginPaint validates it)."""
        sys = _sys(ctx)
        win = sys.handles.get(ctx.args[0])
        if isinstance(win, Window) and win.visible and win.dirty:
            sys.call_wndproc(win, 0x000F, 0, 0)         # WM_PAINT
        return 1

    @api.register_raw("USER", 420)                      # wsprintf — CDECL varargs
    def wsprintf(ctx: CallContext) -> None:
        from .core import ret_far
        cpu = ctx.cpu
        ss, sp = cpu.s.ss & 0xFFFF, cpu.s.sp & 0xFFFF
        cursor = sp + 4                                 # above the far return
        def next_word():
            nonlocal cursor
            v = cpu.mem.rw(ss, cursor & 0xFFFF)
            cursor += 2
            return v
        out_ptr = next_word() | (next_word() << 16)
        fmt_ptr = next_word() | (next_word() << 16)
        fmt = ctx.read_string(fmt_ptr)
        text = _wsprintf_format(ctx, fmt, next_word)
        ctx.mem.load((out_ptr >> 16) & 0xFFFF, out_ptr & 0xFFFF, text + b"\x00")
        ret_far(cpu, 0, ax=len(text))                   # CDECL: caller pops args

    @api.register("USER", 79, args="ptr ptr ptr")       # IntersectRect(dst, a, b)
    def IntersectRect(ctx: CallContext) -> int:
        mem = ctx.mem
        def read_rect(p):
            seg, off = (p >> 16) & 0xFFFF, p & 0xFFFF
            return [_signed(mem.rw(seg, (off + 2 * i) & 0xFFFF)) for i in range(4)]
        a, b = read_rect(ctx.args[1]), read_rect(ctx.args[2])
        left, top = max(a[0], b[0]), max(a[1], b[1])
        right, bottom = min(a[2], b[2]), min(a[3], b[3])
        empty = left >= right or top >= bottom
        out = (0, 0, 0, 0) if empty else (left, top, right, bottom)
        seg, off = (ctx.args[0] >> 16) & 0xFFFF, ctx.args[0] & 0xFFFF
        for i, v in enumerate(out):
            mem.ww(seg, (off + 2 * i) & 0xFFFF, v & 0xFFFF)
        return 0 if empty else 1

    @api.register("USER", 107, args="word word word long", ret="long")
    def DefWindowProc(ctx: CallContext) -> int:         # (hwnd, msg, wp, lp)
        sys = _sys(ctx)
        hwnd, msg, _wp, _lp = ctx.args
        win = sys.handles.get(hwnd)
        if msg == 0x000F and isinstance(win, Window):    # WM_PAINT: validate
            win.dirty = False
        # Default processing for everything the game forwards is "do nothing,
        # return 0" until observed behaviour demands more (WM_CLOSE etc.).
        return 0

    @api.register("USER", 53, args="word")              # DestroyWindow(hwnd)
    def DestroyWindow(ctx: CallContext) -> int:
        sys = _sys(ctx)
        win = sys.handles.get(ctx.args[0])
        if not isinstance(win, Window):
            return 0
        sys.call_wndproc(win, WM_DESTROY, 0, 0)
        for key in [k for k in sys.timers if k[0] == win.handle]:
            sys.timers.pop(key, None)
            sys.timer_due.pop(key, None)
        sys.msg_queue = type(sys.msg_queue)(
            m for m in sys.msg_queue if m[0] != win.handle)
        sys.windows.remove(win)
        sys.handles.remove(win.handle)
        return 1

    @api.register("USER", 421, args="ptr str ptr")      # wvsprintf
    def wvsprintf(ctx: CallContext) -> int:             # (lpOutput, lpFmt, lpArglist)
        """Format `lpFmt` into `lpOutput`, pulling arguments sequentially from
        the caller's varargs block at `lpArglist`.  Returns the character count
        written, excluding the terminating NUL (the Win16 contract)."""
        from win16.wsprintf import format_win16
        out_ptr, fmt_ptr, arg_ptr = ctx.args
        mem = ctx.mem
        arg_seg, arg_off = (arg_ptr >> 16) & 0xFFFF, arg_ptr & 0xFFFF
        cursor = [arg_off]

        def next_word() -> int:
            v = mem.rw(arg_seg, cursor[0] & 0xFFFF)
            cursor[0] = (cursor[0] + 2) & 0xFFFF
            return v

        def next_dword() -> int:
            lo, hi = next_word(), next_word()
            return lo | (hi << 16)

        def read_far_string(seg: int, off: int) -> bytes:
            return ctx.read_string(((seg & 0xFFFF) << 16) | (off & 0xFFFF))

        text = format_win16(ctx.read_string(fmt_ptr), next_word, next_dword,
                            read_far_string, ctx.cpu.s.ds & 0xFFFF)
        out_seg, out_off = (out_ptr >> 16) & 0xFFFF, out_ptr & 0xFFFF
        for i, b in enumerate(text + b"\0"):
            mem.wb(out_seg, (out_off + i) & 0xFFFF, b)
        return len(text)

    @api.register("USER", 1, args="word str str word")  # MessageBox
    def MessageBox(ctx: CallContext) -> int:            # (hwnd, text, caption, type)
        sys = _sys(ctx)
        text = ctx.read_string(ctx.args[1]).decode("latin-1") if ctx.args[1] else ""
        caption = ctx.read_string(ctx.args[2]).decode("latin-1") if ctx.args[2] else ""
        mtype = ctx.args[3]
        ctx.registry.services.setdefault("messagebox_log", []).append(
            (sys.clock_ms, caption, text, mtype))
        from win16.msgbox import default_result
        host = ctx.registry.services.get("messagebox_host")
        if host is None:
            # Headless: auto-dismiss on the DEFAULT button (IDOK / IDYES / ...)
            # so the game takes the affirmative path a bare "OK" stub used to
            # deny (e.g. microman's "Start a new game?" MB_YESNO).
            return default_result(mtype)
        # Present the box (non-blocking) and run a real modal loop: keep
        # pumping WM_PAINT to the game windows so a frame drawn offscreen just
        # before this box (e.g. the crashed-snake frame) is shown while the box
        # is up — exactly what the Windows MessageBox modal loop does.
        box = host.present_box(caption, text, mtype)
        while not box.done.is_set():
            if not sys.pump_modal(paint=True):
                box.done.wait(0.01)
        return box.result & 0xFFFF

    @api.register("USER", 171, args="word str word long")
    def WinHelp(ctx: CallContext) -> int:                # (hwnd, file, cmd, data)
        # The WinHelp engine (.HLP rendering) is its own future slice.  Until
        # then this behaves like help being unavailable — visibly: the host's
        # modal box explains, matching real Windows' "cannot start help" box.
        helpfile = ctx.read_string(ctx.args[1]).decode("latin-1") if ctx.args[1] else ""
        ui = ctx.registry.services.get("messagebox_ui")
        if ui is not None:
            ui("Help", f"Cannot display help ({helpfile}) — the WinHelp "
                       "engine is not implemented yet.", 0x30)
        return 0

    @api.register("USER", 69, args="word")              # SetCursor(hcursor)
    def SetCursor(ctx: CallContext) -> int:
        sys = _sys(ctx)
        prev = sys.machine.api.services.get("cursor", 0)
        sys.machine.api.services["cursor"] = ctx.args[0]
        return prev

    @api.register("USER", 22, args="word")              # SetFocus(hwnd)
    def SetFocus(ctx: CallContext) -> int:
        # Accepts any window-like handle (dialog controls included); focus is
        # host-managed, so this only records the previous focus.
        sys = _sys(ctx)
        prev = sys.machine.api.services.get("focus", 0)
        sys.machine.api.services["focus"] = ctx.args[0]
        return prev

    @api.register("USER", 18, args="word")              # SetCapture(hwnd)
    def SetCapture(ctx: CallContext) -> int:
        sys = _sys(ctx)
        prev = sys.machine.api.services.get("capture", 0)
        sys.machine.api.services["capture"] = ctx.args[0]
        return prev

    @api.register("USER", 19)                           # ReleaseCapture()
    def ReleaseCapture(ctx: CallContext) -> int:
        _sys(ctx).machine.api.services["capture"] = 0
        return 1

    @api.register("USER", 236)                          # GetCapture()
    def GetCapture(ctx: CallContext) -> int:
        return _sys(ctx).machine.api.services.get("capture", 0)

    @api.register("USER", 34, args="word word")         # EnableWindow(hwnd, enable)
    def EnableWindow(ctx: CallContext) -> int:
        # Window or dialog control; enable/disable is host-managed here.
        if _sys(ctx).handles.get(ctx.args[0]) is None:
            return 0
        return 0                    # was not previously disabled

    @api.register("USER", 28, args="word ptr")          # ClientToScreen(hwnd, pt)
    def ClientToScreen(ctx: CallContext) -> int:
        return _map_point(_sys(ctx), ctx, +1)

    @api.register("USER", 31, args="word")              # IsIconic(hwnd)
    def IsIconic(ctx: CallContext) -> int:
        _sys(ctx).handles.require(ctx.args[0], Window)
        return 0                    # minimization is host-side UI; never iconic

    @api.register("USER", 272, args="word")             # IsZoomed(hwnd)
    def IsZoomed(ctx: CallContext) -> int:
        win = _sys(ctx).handles.get(ctx.args[0])
        return 1 if isinstance(win, Window) and win.maximized else 0

    @api.register("USER", 104, args="word")             # MessageBeep(uType)
    def MessageBeep(ctx: CallContext) -> int:
        return 1                    # a UI cue; the host beep is not modelled

    @api.register("USER", 286)                          # GetDesktopWindow()
    def GetDesktopWindow(ctx: CallContext) -> int:
        return _desktop_window(_sys(ctx)).handle

    @api.register("USER", 282, args="word word word")   # SelectPalette(hdc, hpal, bg)
    def SelectPalette(ctx: CallContext) -> int:
        from .objects import DC, Palette
        sys = _sys(ctx)
        dc = sys.handles.get(ctx.args[0])
        pal = sys.handles.get(ctx.args[1])
        if not isinstance(dc, DC):
            return 0
        # A fresh DC has the stock DEFAULT_PALETTE selected, so a successful
        # SelectPalette NEVER returns 0 — programs (microman's WAP LoadPage)
        # treat 0 as failure and abort.  Report the stock handle as "previous"
        # when no logical palette was explicitly selected yet, and accept the
        # stock handle back as a valid restore target.
        prev = dc.palette
        prev_handle = prev.handle if prev is not None else sys.stock_object(15)
        if isinstance(pal, Palette):
            dc.palette = pal
        elif ctx.args[1] == sys.stock_object(15):
            dc.palette = None                   # restored to the default palette
        else:
            return 0
        return prev_handle

    @api.register("USER", 283, args="word")             # RealizePalette(hdc)
    def RealizePalette(ctx: CallContext) -> int:
        from .objects import DC
        sys = _sys(ctx)
        dc = sys.handles.get(ctx.args[0])
        if not isinstance(dc, DC) or dc.palette is None:
            return 0
        # Static single-app model: the realized logical palette BECOMES the
        # system palette (no other app competes for slots).  Programs that
        # then read GetSystemPaletteEntries to build an index remap (microman's
        # WAP identity-palette dance) see their own colours back, so the remap
        # is the identity instead of collapsing to the old grayscale stub.
        entries = list(dc.palette.entries[:256])
        pal = entries + [(0, 0, 0)] * (256 - len(entries))
        changed = sum(1 for a, b in zip(pal, sys.system_palette) if a != b)
        sys.system_palette = pal
        return changed

    @api.register("USER", 13, ret="long")               # GetTickCount()
    def GetTickCount(ctx: CallContext) -> int:
        # Elapsed-time clock.  It must keep advancing even when the program
        # BUSY-WAITS on it without pumping messages (SimAnt times its splash
        # with `while GetTickCount()-t0 < delay`), so the message clock alone
        # (which only ticks at message boundaries) would freeze it.  Use an
        # instruction-derived floor: monotonic, deterministic (oracle-safe),
        # and driven purely by progress.  Message-timed games keep their
        # larger clock_ms unchanged.
        sys = _sys(ctx)
        v = sys.tick_count()
        if sys.tick_recorder is not None:       # clock sideband (tick demos)
            sys.tick_recorder.clock(v)
        return v

    @api.register("USER", 17, args="ptr")               # GetCursorPos(lpPoint)
    def GetCursorPos(ctx: CallContext) -> int:
        sys = _sys(ctx)
        sys.refresh_polled_input()                      # live: see the latest cursor
        x, y = sys.machine.api.services.get("cursor_pos", (0, 0))
        seg, off = (ctx.args[0] >> 16) & 0xFFFF, ctx.args[0] & 0xFFFF
        ctx.mem.ww(seg, off, x & 0xFFFF)
        ctx.mem.ww(seg, (off + 2) & 0xFFFF, y & 0xFFFF)
        return 1

    @api.register("USER", 186, args="word")             # SwapMouseButton(fSwap)
    def SwapMouseButton(ctx: CallContext) -> int:
        # Identified via winevdm's user.exe16.spec (the ordinal-name oracle);
        # an earlier placeholder returned constant TRUE, which told SimAnt's
        # _StillDown the mouse was LEFT-HANDED and swapped its button polling.
        # Real semantics: set the swap state, return the PREVIOUS one.
        services = _sys(ctx).machine.api.services
        prev = services.get("mouse_buttons_swapped", 0)
        services["mouse_buttons_swapped"] = 1 if ctx.args[0] else 0
        return prev

    @api.register("USER", 106, args="word")             # GetKeyState(vk)
    def GetKeyState(ctx: CallContext) -> int:
        # State of a key AT THE LAST message (vs GetAsyncKeyState's live poll).
        # We derive both from the same message-fed key set, so bit 15 = down.
        # (Toggle bit 0 for lock keys is not tracked until a game needs it.)
        sys = _sys(ctx)
        sys.refresh_polled_input()
        services = sys.machine.api.services
        return 0x8000 if ctx.args[0] in services.get("async_keys", set()) else 0

    @api.register("USER", 249, args="word")             # GetAsyncKeyState(vk)
    def GetAsyncKeyState(ctx: CallContext) -> int:
        # Bit 15: key is down NOW.  Bit 0: key went down since the last call
        # for this vk (the latch that catches a tap shorter than one poll
        # interval).  Live poll: drain host input first so a non-pumping spin
        # (SimAnt's caste drag) sees button releases; headless/replay derives it
        # from the consumed message stream instead (identical, deterministic).
        sys = _sys(ctx)
        sys.refresh_polled_input()
        services = sys.machine.api.services
        vk = ctx.args[0]
        result = 0x8000 if vk in services.get("async_keys", set()) else 0
        tapped = services.get("async_keys_tapped", set())
        if vk in tapped:
            tapped.discard(vk)
            result |= 0x0001
        return result

    @api.register("USER", 72, args="ptr s_word s_word s_word s_word")
    def SetRect(ctx: CallContext) -> int:               # (rc, l, t, r, b)
        seg, off = (ctx.args[0] >> 16) & 0xFFFF, ctx.args[0] & 0xFFFF
        for i, v in enumerate(ctx.args[1:]):
            ctx.mem.ww(seg, (off + 2 * i) & 0xFFFF, v & 0xFFFF)
        return 1

    @api.register("USER", 180, args="word", ret="long")  # GetSysColor(nIndex)
    def GetSysColor(ctx: CallContext) -> int:
        # -> the COLORREF (0x00BBGGRR) of a COLOR_* system colour.  Win 3.1
        # defines 0..18; an out-of-range index gives black, as USER does.
        from .gdi import sys_colors
        r, g, b = sys_colors(_sys(ctx)).get(ctx.args[0], (0, 0, 0))
        return (b << 16) | (g << 8) | r

    @api.register("USER", 181, args="word ptr ptr")     # SetSysColors
    def SetSysColors(ctx: CallContext) -> int:          # (n, lpIndices, lpColors)
        # Replace n system colours: lpIndices is an array of n COLOR_* WORDs,
        # lpColors the matching array of n COLORREF DWORDs.  Real USER then
        # broadcasts WM_SYSCOLORCHANGE and repaints every window; we have no
        # second application to notify, and our own readers resolve through the
        # live table at PAINT time (gdi.sys_colors), so the new colours land on
        # the next repaint with nothing to broadcast.
        #
        # Observed contract (SimAnt's _SetUpPalette): GetSysColor all 19 into a
        # save array, SetSysColors(19, ...) its own scheme on the way in, and
        # SetSysColors(19, ..., saved) to put Windows back on the way out.  An
        # index it never touches must keep its default — hence a targeted
        # update, not a wholesale table replace.
        from .gdi import sys_colors
        table = sys_colors(_sys(ctx))
        n, idx_ptr, col_ptr = ctx.args
        iseg, ioff = (idx_ptr >> 16) & 0xFFFF, idx_ptr & 0xFFFF
        cseg, coff = (col_ptr >> 16) & 0xFFFF, col_ptr & 0xFFFF
        for i in range(n):
            index = ctx.mem.rw(iseg, (ioff + 2 * i) & 0xFFFF)
            lo = ctx.mem.rw(cseg, (coff + 4 * i) & 0xFFFF)
            hi = ctx.mem.rw(cseg, (coff + 4 * i + 2) & 0xFFFF)
            colorref = lo | (hi << 16)
            table[index] = (colorref & 0xFF, (colorref >> 8) & 0xFF,
                            (colorref >> 16) & 0xFF)
        return 1

    @api.register("USER", 76, args="ptr long")          # PtInRect(lprc, pt)
    def PtInRect(ctx: CallContext) -> int:
        # The POINT arrives as one DWORD: x in the low word, y in the high.
        # Half-open on right/bottom, exactly as USER: a point ON the right or
        # bottom edge is OUTSIDE.  Both the rect and the point are signed.
        seg, off = (ctx.args[0] >> 16) & 0xFFFF, ctx.args[0] & 0xFFFF
        left, top, right, bottom = (
            _signed(ctx.mem.rw(seg, (off + 2 * i) & 0xFFFF)) for i in range(4))
        x, y = _signed(ctx.args[1] & 0xFFFF), _signed((ctx.args[1] >> 16) & 0xFFFF)
        return 1 if left <= x < right and top <= y < bottom else 0

    @api.register("USER", 473, args="str segptr", ret="long")
    def AnsiPrev(ctx: CallContext) -> int:              # (lpszStart, lpszCurrent)
        # -> a far pointer to the character BEFORE lpszCurrent, clamped to
        # lpszStart.  Win 3.1 defines this for DBCS (where "the previous
        # character" cannot be found by subtracting one, so the scan must start
        # from lpszStart); on a single-byte code page it is lpszCurrent - 1.
        # We are single-byte throughout (latin-1 everywhere in this layer), so
        # the DBCS scan collapses to the decrement — implemented as the clamped
        # decrement rather than a fake lead-byte walk.
        #
        # Observed contract (SimAnt's file dialogs — OPENDLG/SAVEASDLG/
        # _ChangeDirectory/_SeparateFile all share one idiom): start at the
        # string's NUL and step back looking for ':' or '\\', i.e. find the last
        # path separator.  The caller's own `cmp ax,start / jbe done` guard
        # means it never asks past the start, and it reads the result as DX:AX
        # (`mov es,dx / mov bx,ax`) — so the far pointer must keep the segment.
        start, current = ctx.args
        seg = (current >> 16) & 0xFFFF
        off = current & 0xFFFF
        if (start >> 16) & 0xFFFF == seg and off > (start & 0xFFFF):
            off -= 1
        return (seg << 16) | off

    @api.register("USER", 36, args="word segptr word")
    def GetWindowText(ctx: CallContext) -> int:         # (hwnd, lpsz, cch)
        # -> the count copied, NOT counting the NUL; the buffer is always
        # terminated, and cch counts the NUL (so at most cch-1 characters land).
        # SimAnt's MAINWNDPROC reads two window titles into 128-byte buffers and
        # prints them; a control/dialog handle or a bad one yields the empty
        # string, as USER does.
        win = _sys(ctx).handles.get(ctx.args[0])
        title = getattr(win, "title", "") if isinstance(win, Window) else ""
        text = title.encode("latin-1")[:max(ctx.args[2] - 1, 0)]
        buf = ctx.args[1]
        ctx.mem.load((buf >> 16) & 0xFFFF, buf & 0xFFFF, text + b"\x00")
        return len(text)

    @api.register("USER", 70, args="word word")         # SetCursorPos(x, y)
    def SetCursorPos(ctx: CallContext) -> int:
        # Warp the pointer to a SCREEN position.  Two halves, split by who owns
        # what: the guest-visible cursor state is ours (a poll of GetCursorPos
        # must observe the move immediately — that is the half every program can
        # detect), while moving the HOST's real pointer is presentation and only
        # happens if the host installed a "cursor_warp" callback.  Without one
        # the guest's model still moves and the next host mouse event resyncs
        # it, which is exactly what real Windows does when the user's hand moves
        # the mouse after a warp.
        #
        # Observed contract (SimAnt's _DoKeyDown): the arrow keys nudge the
        # pointer 8px — GetCursorPos, adjust, SetCursorPos.  The return is
        # ignored (real USER returns void).
        sys = _sys(ctx)
        x, y = _signed(ctx.args[0]), _signed(ctx.args[1])
        sys.machine.api.services["cursor_pos"] = (x & 0xFFFF, y & 0xFFFF)
        warp = sys.machine.api.services.get("cursor_warp")
        if warp is not None:
            warp(x, y)
        return 0

    @api.register("USER", 118, args="str")              # RegisterWindowMessage
    def RegisterWindowMessage(ctx: CallContext) -> int:
        # A message id unique to the string, system-wide and stable for the
        # session: the same string always maps to the same id, a different one
        # never does.  Real USER allocates from the global atom space, so the
        # ids land in 0xC000..0xFFFF — above every WM_USER-relative control
        # message, which is the property callers rely on.  We mint from the same
        # atom table for exactly that reason (see _atoms).
        #
        # Observed contract (SimAnt's _snd_Install): register one name, keep the
        # id in a global, and compare incoming message ids against it.
        return _atoms(_sys(ctx)).add(ctx.read_string(ctx.args[0]))

    @api.register("USER", 268, args="str")              # GlobalAddAtom(lpString)
    def GlobalAddAtom(ctx: CallContext) -> int:
        # -> the atom for the string (0 on failure).  Case-INSENSITIVE, and
        # ref-counted: adding the same string twice yields the same atom and two
        # deletes are needed to drop it.
        #
        # Observed contract (SimAnt's _GtInitiateDDE): add the application and
        # topic atoms, broadcast WM_DDE_INITIATE with MAKELONG(aApp, aTopic),
        # then delete both — the standard DDE client handshake.
        return _atoms(_sys(ctx)).add(ctx.read_string(ctx.args[0]))

    @api.register("USER", 269, args="word")             # GlobalDeleteAtom(atom)
    def GlobalDeleteAtom(ctx: CallContext) -> int:
        # -> 0 on success, else the atom (still referenced / not ours).  Drops
        # one reference; the atom dies when the last one goes.
        return _atoms(_sys(ctx)).delete(ctx.args[0])

    @api.register("USER", 111, args="word word word long", ret="long")
    def SendMessage(ctx: CallContext) -> int:           # (hwnd, msg, wp, lp)
        sys = _sys(ctx)
        win = sys.handles.get(ctx.args[0])
        if not isinstance(win, Window):
            return 0
        return sys.call_wndproc(win, ctx.args[1], ctx.args[2], ctx.args[3])

    @api.register("USER", 125, args="word ptr word")    # InvalidateRect(hwnd, rc, erase)
    def InvalidateRect(ctx: CallContext) -> int:
        sys = _sys(ctx)
        win = sys.handles.get(ctx.args[0])
        if not isinstance(win, Window):
            # A dialog control's HWND (e.g. SimAnt's SaveAs list box, from
            # GetDlgItem): our dialog UI paints on demand, so invalidating a
            # control is a benign no-op rather than a Window-only crash.
            return 1
        rect = None
        if ctx.args[1]:
            seg, off = (ctx.args[1] >> 16) & 0xFFFF, ctx.args[1] & 0xFFFF
            rect = tuple(_signed(ctx.mem.rw(seg, (off + 2 * i) & 0xFFFF))
                         for i in range(4))
        _invalidate(win, rect, erase=bool(ctx.args[2]))
        return 1

    @api.register("USER", 39, args="word ptr")          # BeginPaint(hwnd, lpPaint)
    def BeginPaint(ctx: CallContext) -> int:
        sys = _sys(ctx)
        win = sys.handles.require(ctx.args[0], Window)
        hdc = sys.new_dc(window=win)
        # Real USER: rcPaint = the update region's box; the background is
        # erased ONLY when an invalidation requested it (RDW_ERASE pending);
        # BeginPaint then validates (clears) the update region — and the DC is
        # CLIPPED to that region.  SimAnt's painter repaints its whole
        # changed-objects list (ants at pre-scroll positions included) and
        # RELIES on the clip discarding everything outside the region it
        # built — unclipped, each map scroll stamps stale tiles over the
        # scrolled pixels (the 16x16 tile-ghosting).  Clipping is enforced at
        # the paint-session boundary: snapshot the surface here, and at
        # EndPaint restore every pixel OUTSIDE the region (byte-equivalent to
        # per-op clipping for the surface EndPaint leaves behind).
        w, h = win.client_size
        rc = win.update_rect or (0, 0, w, h)
        rects = list(getattr(win, "update_rects", ())) or [rc]
        full = len(rects) == 1 and rects[0] == (0, 0, w, h)
        # Snapshot BEFORE the erase so a whole-surface erase fill cannot leak
        # outside the region either (real USER erases only the region).
        win._paint_clip = None if full else (rects, bytes(win.surface.pixels))
        if win.update_erase or win.update_rect is None:
            from .gdi import class_background_rgb
            rgb = class_background_rgb(sys, win.wndclass.h_background)
            if rgb is not None:
                win.surface.fill(rgb)
        _validate(win)
        seg, off = (ctx.args[1] >> 16) & 0xFFFF, ctx.args[1] & 0xFFFF
        mem = ctx.mem
        mem.ww(seg, off, hdc)                            # hdc
        mem.ww(seg, off + 2, 0)                          # fErase (already erased)
        for i, v in enumerate(rc):                       # rcPaint = update box
            mem.ww(seg, (off + 4 + 2 * i) & 0xFFFF, v & 0xFFFF)
        for i in range(10):                              # fRestore/fIncUpdate/reserved
            mem.ww(seg, (off + 12 + 2 * i) & 0xFFFF, 0)
        return hdc

    @api.register("USER", 40, args="word ptr")          # EndPaint(hwnd, lpPaint)
    def EndPaint(ctx: CallContext) -> int:
        sys = _sys(ctx)
        seg, off = (ctx.args[1] >> 16) & 0xFFFF, ctx.args[1] & 0xFFFF
        sys.handles.remove(ctx.mem.rw(seg, off))         # the BeginPaint DC
        win = sys.handles.get(ctx.args[0])
        clip = getattr(win, "_paint_clip", None) if isinstance(win, Window) else None
        if clip is not None:
            # Apply the BeginPaint clip: keep the painted pixels inside the
            # update region, restore everything outside it.
            win._paint_clip = None
            rects, before = clip
            _apply_paint_clip(win.surface, rects, before)
        return 1

    @api.register("USER", 66, args="word")              # GetDC(hwnd)
    def GetDC(ctx: CallContext) -> int:
        sys = _sys(ctx)
        hwnd = ctx.args[0]
        win = _desktop_window(sys) if hwnd == 0 else sys.handles.require(hwnd, Window)
        return sys.new_dc(window=win)

    @api.register("USER", 68, args="word word")         # ReleaseDC(hwnd, hdc)
    def ReleaseDC(ctx: CallContext) -> int:
        sys = _sys(ctx)
        if sys.handles.get(ctx.args[1]) is None:
            return 0
        sys.handles.remove(ctx.args[1])
        return 1

    @api.register("USER", 177, args="word str")         # LoadAccelerators(hInst, name)
    def LoadAccelerators(ctx: CallContext) -> int:
        import struct
        from .objects import AccelTable
        sys = _sys(ctx)
        name = _resource_name(ctx, ctx.args[1])
        res = sys.machine.exe.lookup_resource("ACCELERATOR", name)
        if res is None:
            return 0
        entries = []
        off = 0
        while off + 5 <= len(res.data):
            flags, event, cmd_id = struct.unpack_from("<BHH", res.data, off)
            entries.append((flags, event, cmd_id))
            off += 5
            if flags & 0x80:
                break
        return sys.handles.add(AccelTable(entries))

    @api.register("USER", 175, args="word str")         # LoadBitmap(hInst, name)
    def LoadBitmap(ctx: CallContext) -> int:
        from win16.dib import decode_dib
        from .objects import Bitmap, Surface
        sys = _sys(ctx)
        name = _resource_name(ctx, ctx.args[1])
        res = sys.machine.exe.lookup_resource("BITMAP", name)
        if res is None:
            return 0                # not found — real API contract
        w, h, rgb = decode_dib(res.data)
        return sys.handles.add(Bitmap(Surface(w, h, rgb)))

    @api.register("USER", 173, args="word str")         # LoadCursor(hInst, name)
    def LoadCursor(ctx: CallContext) -> int:
        sys = _sys(ctx)
        return sys.handles.add(Cursor(_resource_name(ctx, ctx.args[1])))

    @api.register("USER", 174, args="word str")         # LoadIcon(hInst, name)
    def LoadIcon(ctx: CallContext) -> int:
        sys = _sys(ctx)
        return sys.handles.add(Icon(_resource_name(ctx, ctx.args[1])))
