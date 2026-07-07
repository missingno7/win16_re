"""USER services — windowing, messages, dialogs. Implemented per observed call."""
from __future__ import annotations

from .core import ApiRegistry, CallContext
from .objects import Cursor, Icon, Menu, Window, WndClass, _signed
from .system import Win16System

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
SYSTEM_METRICS = {
    0: 640,     # SM_CXSCREEN
    1: 480,     # SM_CYSCREEN
    4: 20,      # SM_CYCAPTION (3.1: 19 + 1 border)
    5: 1,       # SM_CXBORDER
    6: 1,       # SM_CYBORDER
    7: 4,       # SM_CXDLGFRAME
    8: 4,       # SM_CYDLGFRAME
    15: 18,     # SM_CYMENU
    16: 640,    # SM_CXFULLSCREEN
    17: 460,    # SM_CYFULLSCREEN
    32: 4,      # SM_CXFRAME (sizing border)
    33: 4,      # SM_CYFRAME
}


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
        )
        sys.classes[name] = cls
        sys.handles.add(cls)
        return 1                    # nonzero = registered (real USER returns an atom)

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
        cs_ptr = _build_createstruct(ctx, sys, win, lp_param, cls_ptr, title_ptr)
        if sys.call_wndproc(win, WM_CREATE, 0, cs_ptr) & 0xFFFF == 0xFFFF:
            sys.windows.remove(win)
            sys.handles.remove(hwnd)
            return 0
        if win.style & WS_VISIBLE:
            win.visible = True
            win.dirty = True
        return hwnd

    @api.register("USER", 64, args="word word s_word s_word word")
    def SetScrollRange(ctx: CallContext) -> int:        # (hwnd, bar, min, max, redraw)
        sys = _sys(ctx)
        hwnd, bar, lo, hi, _redraw = ctx.args
        win = sys.handles.require(hwnd, Window)
        _, _, pos = win.scroll.get(bar, (0, 0, 0))
        win.scroll[bar] = (_signed(lo), _signed(hi), pos)
        return 1

    @api.register("USER", 62, args="word word s_word word")
    def SetScrollPos(ctx: CallContext) -> int:          # (hwnd, bar, pos, redraw)
        sys = _sys(ctx)
        hwnd, bar, pos, _redraw = ctx.args
        win = sys.handles.require(hwnd, Window)
        lo, hi, old = win.scroll.get(bar, (0, 0, 0))
        win.scroll[bar] = (lo, hi, _signed(pos))
        return old & 0xFFFF

    @api.register("USER", 33, args="word ptr")          # GetClientRect(hwnd, rect)
    def GetClientRect(ctx: CallContext) -> int:
        sys = _sys(ctx)
        win = sys.handles.require(ctx.args[0], Window)
        seg, off = (ctx.args[1] >> 16) & 0xFFFF, ctx.args[1] & 0xFFFF
        w, h = win.client_size
        for i, v in enumerate((0, 0, w, h)):
            ctx.mem.ww(seg, (off + 2 * i) & 0xFFFF, v & 0xFFFF)
        return 1

    @api.register("USER", 32, args="word ptr")          # GetWindowRect(hwnd, rect)
    def GetWindowRect(ctx: CallContext) -> int:
        sys = _sys(ctx)
        win = sys.handles.require(ctx.args[0], Window)
        seg, off = (ctx.args[1] >> 16) & 0xFFFF, ctx.args[1] & 0xFFFF
        for i, v in enumerate((win.x, win.y, win.x + win.w, win.y + win.h)):
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
        # Content is a bitmap handle (MF_BITMAP) or string ptr; stored only —
        # menu rendering is host-side UI, not game state.
        menu.item_flags[new_id] = flags & 0x000B
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
        if proc:
            raise NotImplementedError("SetTimer with TimerProc callback")
        sys.timers[(hwnd, timer_id)] = max(ms, 1)
        sys.timer_due[(hwnd, timer_id)] = sys.clock_ms + max(ms, 1)
        return timer_id

    @api.register("USER", 12, args="word word")         # KillTimer(hwnd, id)
    def KillTimer(ctx: CallContext) -> int:
        sys = _sys(ctx)
        sys.timer_due.pop((ctx.args[0], ctx.args[1]), None)
        return 1 if sys.timers.pop((ctx.args[0], ctx.args[1]), None) is not None else 0

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
        win = sys.handles.get(hwnd)
        if not isinstance(win, Window):
            return 0
        return sys.call_wndproc(win, msg, wparam, lparam)

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
        win = sys.handles.require(hwnd, Window)
        win.x, win.y = _signed(x), _signed(y)
        resized = (win.w, win.h) != (_signed(w), _signed(h))
        win.w, win.h = _signed(w), _signed(h)
        if resized:
            win._surface = None                          # client surface rebuilds
            cw, ch = win.client_size
            sys.call_wndproc(win, WM_SIZE, 0, ((ch & 0xFFFF) << 16) | (cw & 0xFFFF))
        sys.call_wndproc(win, WM_MOVE, 0,
                         ((win.y & 0xFFFF) << 16) | (win.x & 0xFFFF))
        if repaint:
            win.dirty = True
        return 1

    @api.register("USER", 42, args="word word")         # ShowWindow(hwnd, cmd)
    def ShowWindow(ctx: CallContext) -> int:
        sys = _sys(ctx)
        win = sys.handles.require(ctx.args[0], Window)
        was = win.visible
        win.visible = ctx.args[1] != 0                  # 0 = SW_HIDE
        if win.visible and not was:
            win.dirty = True
        return 1 if was else 0

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

    @api.register("USER", 1, args="word str str word")  # MessageBox
    def MessageBox(ctx: CallContext) -> int:            # (hwnd, text, caption, type)
        sys = _sys(ctx)
        text = ctx.read_string(ctx.args[1]).decode("latin-1") if ctx.args[1] else ""
        caption = ctx.read_string(ctx.args[2]).decode("latin-1") if ctx.args[2] else ""
        ctx.registry.services.setdefault("messagebox_log", []).append(
            (sys.clock_ms, caption, text, ctx.args[3]))
        ui = ctx.registry.services.get("messagebox_ui")
        if ui is not None:
            # An interactive host shows the real modal box; this blocks the
            # CPU thread until the user answers — faithful modal semantics.
            return ui(caption, text, ctx.args[3]) & 0xFFFF
        return 1                    # headless: IDOK — modal UI auto-dismissed

    @api.register("USER", 87, args="word str word segptr")
    def DialogBox(ctx: CallContext) -> int:             # (hInst, template, parent, proc)
        # STOPGAP until the dialog engine lands: skip the dialog, tell the
        # host, and return -1 (the real API's failure value, which apps
        # handle).  Logged loudly — never silently.
        name = _resource_name(ctx, ctx.args[1])
        _sys(ctx)  # ensure system exists
        ctx.registry.services.setdefault("skipped_ui", []).append(
            ("DialogBox", name))
        return 0xFFFF                                    # -1: dialog not shown

    @api.register("USER", 171, args="word str word long")
    def WinHelp(ctx: CallContext) -> int:                # (hwnd, file, cmd, data)
        helpfile = ctx.read_string(ctx.args[1]).decode("latin-1") if ctx.args[1] else ""
        ctx.registry.services.setdefault("skipped_ui", []).append(
            ("WinHelp", helpfile))
        return 0                                         # help engine unavailable

    @api.register("USER", 69, args="word")              # SetCursor(hcursor)
    def SetCursor(ctx: CallContext) -> int:
        sys = _sys(ctx)
        prev = sys.machine.api.services.get("cursor", 0)
        sys.machine.api.services["cursor"] = ctx.args[0]
        return prev

    @api.register("USER", 22, args="word")              # SetFocus(hwnd)
    def SetFocus(ctx: CallContext) -> int:
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

    @api.register("USER", 34, args="word word")         # EnableWindow(hwnd, enable)
    def EnableWindow(ctx: CallContext) -> int:
        _sys(ctx).handles.require(ctx.args[0], Window)
        return 0                    # was not disabled (disabled state unmodelled)

    @api.register("USER", 28, args="word ptr")          # ClientToScreen(hwnd, pt)
    def ClientToScreen(ctx: CallContext) -> int:
        sys = _sys(ctx)
        win = sys.handles.require(ctx.args[0], Window)
        seg, off = (ctx.args[1] >> 16) & 0xFFFF, ctx.args[1] & 0xFFFF
        x = _signed(ctx.mem.rw(seg, off)) + win.x
        y = _signed(ctx.mem.rw(seg, (off + 2) & 0xFFFF)) + win.y
        ctx.mem.ww(seg, off, x & 0xFFFF)
        ctx.mem.ww(seg, (off + 2) & 0xFFFF, y & 0xFFFF)
        return 1

    @api.register("USER", 31, args="word")              # IsIconic(hwnd)
    def IsIconic(ctx: CallContext) -> int:
        _sys(ctx).handles.require(ctx.args[0], Window)
        return 0                    # minimization is host-side UI; never iconic

    @api.register("USER", 125, args="word ptr word")    # InvalidateRect(hwnd, rc, erase)
    def InvalidateRect(ctx: CallContext) -> int:
        sys = _sys(ctx)
        win = sys.handles.require(ctx.args[0], Window)
        # Update region granularity is whole-client for now; the rect (and the
        # erase flag) will matter when pixel evidence demands them.
        win.dirty = True
        return 1

    @api.register("USER", 39, args="word ptr")          # BeginPaint(hwnd, lpPaint)
    def BeginPaint(ctx: CallContext) -> int:
        from .objects import Brush
        sys = _sys(ctx)
        win = sys.handles.require(ctx.args[0], Window)
        hdc = sys.new_dc(window=win)
        # Erase the background with the class brush (real USER does this when
        # the update region is marked for erase).
        bg = sys.handles.get(win.wndclass.h_background)
        if isinstance(bg, Brush):
            c = bg.color
            win.surface.fill((c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF))
        win.dirty = False
        seg, off = (ctx.args[1] >> 16) & 0xFFFF, ctx.args[1] & 0xFFFF
        w, h = win.client_size
        mem = ctx.mem
        mem.ww(seg, off, hdc)                            # hdc
        mem.ww(seg, off + 2, 0)                          # fErase (already erased)
        for i, v in enumerate((0, 0, w, h)):             # rcPaint
            mem.ww(seg, (off + 4 + 2 * i) & 0xFFFF, v & 0xFFFF)
        for i in range(10):                              # fRestore/fIncUpdate/reserved
            mem.ww(seg, (off + 12 + 2 * i) & 0xFFFF, 0)
        return hdc

    @api.register("USER", 40, args="word ptr")          # EndPaint(hwnd, lpPaint)
    def EndPaint(ctx: CallContext) -> int:
        sys = _sys(ctx)
        seg, off = (ctx.args[1] >> 16) & 0xFFFF, ctx.args[1] & 0xFFFF
        sys.handles.remove(ctx.mem.rw(seg, off))         # the BeginPaint DC
        return 1

    @api.register("USER", 66, args="word")              # GetDC(hwnd)
    def GetDC(ctx: CallContext) -> int:
        sys = _sys(ctx)
        win = sys.handles.require(ctx.args[0], Window)
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
