"""Play Paulie Python interactively — each Win16 window is a real OS window.

    python scripts/play.py [--speed N] [--scale N]

The VM runs on a worker thread; the GUI (main) thread mirrors every Win16
window the game creates as its own tkinter window: the "Paulie Python" main
window carries the game's real menu bar (from its MENU resource, with live
grayed/checked state), the "Paulie-O-Meter" floats beside it, splash windows
come and go.  Game MessageBoxes appear as real modal message boxes.

Controls (the game's own):
    Arrow keys   steer Paulie
    F2 new game / F3 sound / F4 pause / F5 high scores / F10 exit
    mouse        move / click (in mouse-control mode)

Close the Paulie Python window to quit.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from PIL import Image, ImageTk

import ppython._env  # noqa: F401  (puts the dos_re framework on sys.path)

from scripts.games import GAMES, game_exe, game_winflags
from win16.api.core import Win16ApiGap
from win16.api.objects import Window
from win16.api.system import Win16System
from win16.app import create_machine
from win16.dialog import du_to_px
from win16.interactive import InteractiveDriver
from win16.menu import (MF_CHECKED, MF_DISABLED, MF_GRAYED, MF_POPUP,
                        MF_SEPARATOR, parse_menu)

# Button styles (low nibble).
BS_CHECKBOX, BS_AUTOCHECKBOX = 0x2, 0x3
BS_RADIOBUTTON, BS_GROUPBOX, BS_AUTORADIOBUTTON = 0x4, 0x7, 0x9
SS_ICON = 0x3                                    # Static style low nibble
# Notification codes packed into WM_COMMAND's HIWORD(lParam).
BN_CLICKED, CBN_SELCHANGE = 0, 1

# Win 3.1 dialog font "Helv" is the ancestor of MS Sans Serif — the closest
# faithful substitute available on modern Windows.
DIALOG_FONT_MAP = {"Helv": "MS Sans Serif", "MS Sans Serif": "MS Sans Serif",
                   "System": "MS Sans Serif", "Times New Roman": "Times New Roman"}
DIALOG_BG = "#c0c0c0"                            # Win 3.1 dialog face colour

WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_COMMAND = 0x0111
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
MK_LBUTTON, MK_RBUTTON = 0x0001, 0x0002

# tkinter keysym -> Win16 virtual-key code.
KEYSYM_VK = {
    "Left": 0x25, "Up": 0x26, "Right": 0x27, "Down": 0x28,
    "space": 0x20, "Return": 0x0D, "Escape": 0x1B, "Tab": 0x09,
    "BackSpace": 0x08, "Prior": 0x21, "Next": 0x22, "Home": 0x24, "End": 0x23,
    "Insert": 0x2D, "Delete": 0x2E, "Pause": 0x13,
    **{f"F{i}": 0x6F + i for i in range(1, 13)},
}


def keysym_to_vk(event) -> int | None:
    if event.keysym in KEYSYM_VK:
        return KEYSYM_VK[event.keysym]
    ks = event.keysym
    if len(ks) == 1 and ks.upper().isalnum():
        return ord(ks.upper())
    return None


class WindowView:
    """One real tkinter window mirroring one Win16 window."""

    def __init__(self, app: "PlayApp", win: Window) -> None:
        self.app = app
        self.win = win
        self.scale = app.scale
        self._photo = None
        self._img_item = None
        self._last_version = -1
        self._menu_entries: list[tuple] = []
        #        (menu widget, entry index, item id, initial flags, check-var|None)
        self._menu_applied: dict[tuple[str, int], tuple] = {}
        self._menu_images: list = []            # keep PhotoImage refs alive
        self._last_geo = None

        self._menu_sig = None                   # rebuild the menubar when it changes

        WS_THICKFRAME, WS_SYSMENU = 0x00040000, 0x00080000
        WS_VSCROLL, WS_HSCROLL = 0x00200000, 0x00100000
        # A window's own frame chrome mirrors its Win16 styles: WS_THICKFRAME =>
        # user-resizable (the OS gives resize handles + the maximize button when
        # WS_MAXIMIZEBOX is set too); WS_SYSMENU => a close button; WS_H/VSCROLL
        # => scroll bars.  SimAnt's in-game panels ("Caste Control" etc.) are
        # fixed+closable; its "SimAnt - Quick Game" view is resizable + scrollable.
        self._can_resize = bool(win.style & WS_THICKFRAME)
        self._can_close = bool(win.style & WS_SYSMENU)
        self._has_vscroll = bool(win.style & WS_VSCROLL)
        self._has_hscroll = bool(win.style & WS_HSCROLL)

        self.top = tk.Toplevel(app.root)
        self.top.title(win.title or win.wndclass.name)
        self.top.resizable(self._can_resize, self._can_resize)
        w, h = win.client_size
        self.canvas = tk.Canvas(self.top, width=w * self.scale,
                                height=h * self.scale, highlightthickness=0,
                                bg="black")
        self._resize_after = None
        self._vscroll = self._hscroll = None
        self._layout_canvas()
        # "main" = carries a menu bar (from a MENU resource OR built at runtime
        # via CreateMenu/AppendMenu, like SimAnt).  Detected live in sync() too,
        # since SetMenu may land after this view is created.
        self.is_main = self._has_menu()
        self.status_var = tk.StringVar(value="ready")
        if self.is_main:
            self._install_status_bar()
        self._build_menubar()
        self._place()
        self._bind_input()
        # Closing the main frame quits; closing an in-game panel just sends it
        # WM_CLOSE (the game hides/destroys it) — like a real window's close box.
        self.top.protocol("WM_DELETE_WINDOW", self._on_close_box)

    def _on_close_box(self) -> None:
        WM_CLOSE = 0x0010
        if self.is_main or not self._can_close:
            self.app.on_close()
        else:
            self.app.driver.post_input(self.win.handle, WM_CLOSE, 0, 0)

    def _layout_canvas(self) -> None:
        """Place the canvas, plus WS_H/VSCROLL scroll bars.  Scroll bars force a
        grid layout (canvas + bars); resize binds <Configure>."""
        if self._has_vscroll or self._has_hscroll:
            self.top.rowconfigure(0, weight=1)
            self.top.columnconfigure(0, weight=1)
            self.canvas.grid(row=0, column=0, sticky="nsew")
            if self._has_vscroll:
                self._vscroll = tk.Scrollbar(self.top, orient="vertical",
                                             command=self._on_vscroll)
                self._vscroll.grid(row=0, column=1, sticky="ns")
            if self._has_hscroll:
                self._hscroll = tk.Scrollbar(self.top, orient="horizontal",
                                             command=self._on_hscroll)
                self._hscroll.grid(row=1, column=0, sticky="ew")
        elif self._can_resize:
            self.canvas.pack(fill="both", expand=True)
        else:
            self.canvas.pack()
        if self._can_resize:
            # Dragging the frame resizes the Win16 window (client = canvas/scale)
            # and fires WM_SIZE so the game re-lays-out (SimAnt is resolution-
            # adaptive).  Debounced so a drag doesn't post WM_SIZE per pixel.
            self.canvas.bind("<Configure>", self._on_configure)

    # -- scroll bars (WS_H/VSCROLL windows) -----------------------------------
    def _on_vscroll(self, *args):
        self._post_scroll(0x0115, 1, args)      # WM_VSCROLL, SB_VERT
    def _on_hscroll(self, *args):
        self._post_scroll(0x0114, 0, args)      # WM_HSCROLL, SB_HORZ

    def _post_scroll(self, msg: int, bar: int, args) -> None:
        # Map the tkinter scroll command to a Win16 scroll code + thumb pos and
        # post WM_H/VSCROLL(wParam = code | pos<<16) to the window.
        lo, hi, pos = self.win.scroll.get(bar, (0, 0, 0))
        if args[0] == "moveto":                 # SB_THUMBPOSITION
            newpos = int(round(lo + float(args[1]) * (hi - lo)))
            wparam = 4 | ((newpos & 0xFFFF) << 16)
        else:                                   # ("scroll", n, "units"|"pages")
            n, unit = int(args[1]), args[2]
            if unit == "units":
                wparam = 1 if n > 0 else 0       # SB_LINEDOWN / SB_LINEUP
            else:
                wparam = 3 if n > 0 else 2       # SB_PAGEDOWN / SB_PAGEUP
        self.app.driver.post_input(self.win.handle, msg, wparam, 0)

    def _sync_scrollbars(self) -> None:
        for bar, sb in ((1, self._vscroll), (0, self._hscroll)):
            if sb is None:
                continue
            lo, hi, pos = self.win.scroll.get(bar, (0, 0, 0))
            if hi > lo:
                first = (pos - lo) / (hi - lo)
                sb.set(first, min(first + 0.15, 1.0))
            else:
                sb.set(0.0, 1.0)

    def _has_menu(self) -> bool:
        if self.win.wndclass.menu_name is not None:
            return True
        mo = getattr(self.win, "menu_obj", None)
        return mo is not None and bool(mo.items)

    def _install_status_bar(self) -> None:
        tk.Label(self.top, textvariable=self.status_var, anchor="w",
                 font=("Consolas", 9)).pack(fill="x")

    # -- placement ----------------------------------------------------------
    def _place(self) -> None:
        # Absolute VM-virtual origin (walks the parent chain), so a promoted
        # child panel lands where the game placed it relative to the frame, not
        # at its raw parent-relative (x, y).  For a top-level frame this is just
        # its own (x, y).
        ox, oy = self.app.sys._window_origin(self.win.handle)
        x = self.app.origin_x + ox * self.scale
        y = self.app.origin_y + oy * self.scale
        self.top.geometry(f"+{x}+{y}")
        self._last_geo = (self.win.x, self.win.y)

    # -- the game's menu bar --------------------------------------------------
    def _menu_signature(self):
        """A cheap key that changes when the menu structure changes, so sync()
        can (re)build the native menubar once the game's SetMenu/AppendMenu land."""
        mo = getattr(self.win, "menu_obj", None)
        if mo is None:
            return ("res", self.win.wndclass.menu_name)
        return ("obj", tuple((it.flags, it.id, it.text) for it in mo.items))

    def _build_menubar(self) -> None:
        self._menu_entries = []
        self._menu_applied = {}
        self._menu_sig = self._menu_signature()
        resources = self.app.machine.exe.find_resources("MENU")
        if resources and self.win.wndclass.menu_name is not None:
            bar = tk.Menu(self.top)
            for item in parse_menu(resources[0].data):
                self._add_menu_item(bar, item)
            self.top.config(menu=bar)
            return
        # No MENU resource: build from the runtime-built menu (CreateMenu/
        # AppendMenu -> SetMenu), which is how SimAnt makes File/Window/View/...
        mo = getattr(self.win, "menu_obj", None)
        if mo is None or not mo.items:
            return
        bar = tk.Menu(self.top)
        for it in mo.items:
            self._add_menu_obj_item(bar, it)
        self.top.config(menu=bar)

    def _add_menu_obj_item(self, parent: tk.Menu, it) -> None:
        """Add one runtime Menu item (win16.api.objects.MenuItem) to a tk menu."""
        label = (it.text or "").replace("&", "")
        if it.flags & MF_SEPARATOR:
            parent.add_separator()
            return
        if (it.flags & MF_POPUP) and it.submenu is not None:
            sub = tk.Menu(parent, tearoff=0)
            for child in it.submenu.items:
                self._add_menu_obj_item(sub, child)
            parent.add_cascade(label=label, menu=sub)
            return
        hwnd, cmd_id = self.win.handle, it.id
        image = self._menu_image(cmd_id)
        if image is not None:
            var = tk.IntVar(value=1 if (it.flags & MF_CHECKED) else 0)
            parent.add_checkbutton(
                image=image, variable=var, onvalue=1, offvalue=0,
                command=lambda: self.app.driver.post_input(hwnd, WM_COMMAND, cmd_id, 0))
            self._menu_entries.append((parent, parent.index("end"), cmd_id,
                                       it.flags, var))
        else:
            parent.add_command(
                label=label,
                command=lambda: self.app.driver.post_input(hwnd, WM_COMMAND, cmd_id, 0))
            self._menu_entries.append((parent, parent.index("end"), cmd_id,
                                       it.flags, None))


    def _add_menu_item(self, parent: tk.Menu, item) -> None:
        if item.is_separator:
            parent.add_separator()
            return
        if item.is_popup:
            sub = tk.Menu(parent, tearoff=0)
            for child in item.children:
                self._add_menu_item(sub, child)
            parent.add_cascade(label=item.text_and_accel()[0], menu=sub)
            return
        text, accel = item.text_and_accel()
        hwnd, cmd_id = self.win.handle, item.item_id
        # The game may have replaced this text item with a bitmap (the
        # ScreenSculptor Shape menu, via ModifyMenu MF_BITMAP) — render the real
        # icon.  add_checkbutton keeps the selected-shape checkmark working.
        image = self._menu_image(cmd_id)
        if image is not None:
            var = tk.IntVar(value=1 if (item.flags & MF_CHECKED) else 0)
            parent.add_checkbutton(
                image=image, variable=var, onvalue=1, offvalue=0,
                command=lambda: self.app.driver.post_input(hwnd, WM_COMMAND, cmd_id, 0))
            self._menu_entries.append((parent, parent.index("end"), cmd_id,
                                       item.flags, var))
        else:
            parent.add_command(
                label=text, accelerator=accel or None,
                command=lambda: self.app.driver.post_input(hwnd, WM_COMMAND, cmd_id, 0))
            self._menu_entries.append((parent, parent.index("end"), cmd_id,
                                       item.flags, None))

    def _menu_image(self, cmd_id: int):
        """PhotoImage for a menu item the game turned into a bitmap, or None."""
        from win16.api.objects import Bitmap
        menu_obj = self.win.menu_obj
        if menu_obj is None:
            return None
        handle = menu_obj.item_bitmaps.get(cmd_id)
        if not handle:
            return None
        bmp = self.app.sys.handles.get(handle)
        if not isinstance(bmp, Bitmap):
            return None
        surf = bmp.surface
        try:
            img = Image.frombytes("RGB", (surf.w, surf.h), bytes(surf.pixels))
        except ValueError:
            return None
        if self.scale != 1:
            img = img.resize((surf.w * self.scale, surf.h * self.scale), Image.NEAREST)
        photo = ImageTk.PhotoImage(img)
        self._menu_images.append(photo)
        return photo

    def _sync_menu_state(self) -> None:
        """Mirror the game's menu state: grayed items are unclickable (real
        USER never delivers WM_COMMAND for them) and checks show live.

        Only entries whose computed state CHANGED are reconfigured —
        reconfiguring an open tkinter menu every tick makes it flicker and
        fight the user's selection."""
        flags_now = {}
        if self.win.menu_obj is not None:
            flags_now = self.win.menu_obj.item_flags
        for menu, index, cmd_id, initial, var in self._menu_entries:
            flags = flags_now.get(cmd_id, initial)
            state = "disabled" if flags & (MF_GRAYED | MF_DISABLED) else "normal"
            checked = bool(flags & MF_CHECKED)
            key = (str(menu), index)
            if var is not None:                 # bitmap item: check via the var
                if self._menu_applied.get(key) != (state, checked):
                    var.set(1 if checked else 0)
                    try:
                        menu.entryconfig(index, state=state)
                        self._menu_applied[key] = (state, checked)
                    except tk.TclError:
                        pass
                continue
            check = "✓ " if checked else ""
            if self._menu_applied.get(key) == (state, check):
                continue
            try:
                label = check + menu.entrycget(index, "label").lstrip("✓ ")
                menu.entryconfig(index, state=state, label=label)
                self._menu_applied[key] = (state, check)
            except tk.TclError:
                pass

    # -- input ----------------------------------------------------------------
    def _bind_input(self) -> None:
        t, c = self.top, self.canvas
        t.bind("<KeyPress>", self._on_key_down)
        t.bind("<KeyRelease>", self._on_key_up)
        c.bind("<Motion>", lambda e: self._on_mouse(e, WM_MOUSEMOVE, 0))
        c.bind("<Button-1>", lambda e: self._on_mouse(e, WM_LBUTTONDOWN, MK_LBUTTON))
        c.bind("<ButtonRelease-1>", lambda e: self._on_mouse(e, WM_LBUTTONUP, 0))
        c.bind("<Button-3>", lambda e: self._on_mouse(e, WM_RBUTTONDOWN, MK_RBUTTON))
        c.bind("<ButtonRelease-3>", lambda e: self._on_mouse(e, WM_RBUTTONUP, 0))

    def _on_key_down(self, event) -> None:
        if event.keysym == "F9":                # harness key: take a snapshot
            self.app.take_snapshot()
            return
        vk = keysym_to_vk(event)
        if vk is not None:
            self.app.driver.post_input(self.win.handle, WM_KEYDOWN, vk, 0x0001)

    def _on_key_up(self, event) -> None:
        vk = keysym_to_vk(event)
        if vk is not None:
            self.app.driver.post_input(self.win.handle, WM_KEYUP, vk, 0xC0000001)

    def _on_mouse(self, event, msg: int, mk: int) -> None:
        # The native tkinter menubar occupies its own non-client strip (not the
        # canvas), so canvas coords map straight to the game's client space.
        self._raise_z()
        cx, cy = event.x // self.scale, event.y // self.scale
        target, tx, ty = self._route_click(cx, cy)
        lparam = ((ty & 0xFFFF) << 16) | (tx & 0xFFFF)
        self.app.driver.post_input(target, msg, mk, lparam)

    def _route_click(self, cx: int, cy: int):
        """Deliver the click to the deepest COMPOSITED child window under it,
        with coords relative to that child — real Windows sends a click to the
        child window it lands on, not the frame.  SimAnt's ribbon (a WS_CHILD
        toolbar composited into the main frame) has its OWN wndproc that
        hit-tests its buttons, so a click posted to the frame (0x114) never
        reaches it; the promoted panels already get theirs directly (own view),
        which is why they worked and the ribbon did not.  Standalone/promoted
        children (own view) are skipped here.  Returns (hwnd, x, y)."""
        from win16 import compositor
        sysobj = self.app.sys
        cur, x, y = self.win, cx, cy
        while True:
            nxt = None
            for ch in sysobj.windows:            # later in list = topmost Z
                if getattr(ch, "parent", 0) != cur.handle or not ch.visible:
                    continue
                if compositor.presents_standalone(ch):
                    continue
                if ch.x <= x < ch.x + ch.w and ch.y <= y < ch.y + ch.h:
                    nxt = ch
            if nxt is None:
                return cur.handle, x, y
            cur, x, y = nxt, x - nxt.x, y - nxt.y

    def _raise_z(self) -> None:
        # A promoted panel (Caste/Behavior/Nest/Quick Game) is a WS_CHILD that
        # OVERLAPS its siblings in the game's virtual screen space — they were
        # laid out as stacked MDI children.  The game hit-tests the polled cursor
        # with WindowFromPoint, which tie-breaks to the LAST window in z-order,
        # so unless the panel under the real cursor is moved to the top of the VM
        # window list every click routes to whichever panel sorts last (Quick
        # Game) and the others' buttons go dead.  Raise on any mouse event, so
        # the panel being pointed at is the one the game resolves the click to.
        if not (self.win.style & 0x40000000):        # WS_CHILD only (not the frame)
            return
        wins = self.app.sys.windows
        if wins and wins[-1] is not self.win:        # identity: distinct Windows
            for i, w in enumerate(wins):             # can compare-equal by value
                if w is self.win:
                    del wins[i]
                    wins.append(self.win)
                    break

    # -- resize (WS_THICKFRAME windows) ---------------------------------------
    def _on_configure(self, event) -> None:
        if self._resize_after is not None:
            self.top.after_cancel(self._resize_after)
        self._resize_after = self.top.after(120, self._apply_resize)

    def _apply_resize(self) -> None:
        self._resize_after = None
        cw = max(self.canvas.winfo_width() // self.scale, 1)
        ch = max(self.canvas.winfo_height() // self.scale, 1)
        win = self.win
        if (cw, ch) == (win.w, win.h):
            return
        WM_SIZE = 0x0005
        win.w, win.h = cw, ch
        win._surface = None                     # reallocate at the new client size
        self.app.driver.post_input(win.handle, WM_SIZE, 0,
                                   ((ch & 0xFFFF) << 16) | (cw & 0xFFFF))

    # -- per-tick sync ---------------------------------------------------------
    def _composited(self):
        """The image to display: this window with its plain WS_CHILD windows
        composited in at their offsets (SimAnt's canvas/ribbon/body live in child
        windows).  menu_bar=False: the menu is a real tkinter widget here, so the
        painted strip (headless/screenshot chrome) is suppressed.  `standalone` =
        every window that has its OWN view, so captioned panels promoted to real
        Toplevels are NOT also drawn into this frame."""
        from win16 import compositor
        standalone = set(self.app.views)
        sysobj = self.app.sys
        # The CPU worker thread rewrites the game's surfaces (a SetDIBits blit of
        # the whole Quick Game frame can run for ms) while WE read them here on
        # the tkinter thread — a concurrent read can catch a half-updated buffer
        # and show a torn/ghosted frame.  Take a version fence around the copy:
        # if a surface write COMPLETED (touch bumped the version) while we were
        # compositing, the copy may be torn, so redo it once — the write is done
        # by then.  Cheap; only matters during active redraw (idle => no bump).
        for _ in range(2):
            before = compositor.tree_version(sysobj, self.win)
            out = compositor.composite(sysobj, self.win, menu_bar=False,
                                       standalone=standalone)
            if compositor.tree_version(sysobj, self.win) == before:
                break
        return out

    def sync(self) -> None:
        from win16 import compositor
        win = self.win
        if (win.x, win.y) != self._last_geo:
            self._place()
        # The game may SetMenu / AppendMenu after this view was created (SimAnt
        # builds its bar at runtime); (re)build the native menubar when it lands
        # or changes.
        if self._menu_signature() != self._menu_sig:
            if not self.is_main and self._has_menu():
                self.is_main = True
                self._install_status_bar()
            self._build_menubar()
        version = compositor.tree_version(self.app.sys, win)
        if version != self._last_version:
            self._last_version = version
            self._redraw(self._composited())
        if self.top.title() != (win.title or win.wndclass.name):
            self.top.title(win.title or win.wndclass.name)
        if self.is_main:
            self._sync_menu_state()
        if self._vscroll is not None or self._hscroll is not None:
            self._sync_scrollbars()

    def force_render(self) -> None:
        """Redraw the current frame regardless of the version gate — used to
        flush the very last frame (e.g. the crash frame) onto the screen just
        before a modal box/dialog blocks further ticks."""
        from win16 import compositor
        self._last_version = compositor.tree_version(self.app.sys, self.win)
        self._redraw(self._composited())

    def _redraw(self, surf) -> None:
        w, h = self.win.client_size
        if not self._can_resize and surf.w == w and surf.h == h \
                and int(self.canvas["width"]) != w * self.scale:
            self.canvas.config(width=w * self.scale, height=h * self.scale)
        try:
            img = Image.frombytes("RGB", (surf.w, surf.h), bytes(surf.pixels))
        except ValueError:
            return
        if self.scale != 1:
            img = img.resize((surf.w * self.scale, surf.h * self.scale),
                             Image.NEAREST)
        # Update the one canvas image in place — deleting and recreating it
        # every tick is what caused visible flicker.
        self._photo = ImageTk.PhotoImage(img)
        if self._img_item is None:
            self._img_item = self.canvas.create_image(0, 0, image=self._photo,
                                                      anchor="nw")
        else:
            self.canvas.itemconfig(self._img_item, image=self._photo)

    def destroy(self) -> None:
        try:
            self.top.destroy()
        except tk.TclError:
            pass


class DialogView:
    """A real modal tkinter window rendering one Win16 Dialog.  Widgets read
    from / write to the shared DialogControlState objects; user actions post
    events onto the dialog's event queue for the CPU-thread modal loop."""

    def __init__(self, app: "PlayApp", dlg) -> None:
        self.app = app
        self.dlg = dlg
        self.scale = app.scale
        self.widgets: dict[int, object] = {}          # ctrl_id -> tk widget
        self.vars: dict[int, tk.Variable] = {}

        # Parent to a VISIBLE game window — the root is withdrawn, and a
        # dialog transient to a withdrawn window never maps (it just grabs
        # input invisibly, freezing the game).  Prefer the dialog's own parent
        # HWND, else the main game window.
        parent_view = app.views.get(dlg.parent) or next(
            (v for v in app.views.values() if v.is_main), None)
        parent_top = parent_view.top if parent_view else app.root

        self.top = tk.Toplevel(parent_top)
        self.top.title(dlg.template.caption or "Dialog")
        self.top.resizable(False, False)

        # Dialog font + base units derived FROM that font, exactly as Windows
        # maps dialog units to pixels: x_px = du * baseX/4, y_px = du * baseY/8,
        # where baseX = the font's average char width and baseY = its height.
        fam = DIALOG_FONT_MAP.get(dlg.template.font or "", "MS Sans Serif")
        pt = dlg.template.point_size or 8
        self.font = tkfont.Font(family=fam, size=pt)
        alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        self.base_x = max(round(self.font.measure(alphabet) / 52), 1)
        self.base_y = max(self.font.metrics("linespace"), 1)

        w, h = self._du_px(dlg.template.cx, dlg.template.cy)
        w, h = max(w, 1) * self.scale, max(h, 1) * self.scale
        self.frame = tk.Frame(self.top, width=w, height=h, bg=DIALOG_BG)
        self.frame.pack(fill="both", expand=True)
        self.frame.pack_propagate(False)
        for ctrl in dlg.controls:
            self._build_control(ctrl)

        # Centre over the parent, then map and raise it before grabbing input.
        self.top.update_idletasks()
        if parent_view is not None:
            px, py = parent_top.winfo_rootx(), parent_top.winfo_rooty()
            pw, ph = parent_top.winfo_width(), parent_top.winfo_height()
            x = px + max((pw - w) // 2, 0)
            y = py + max((ph - h) // 2, 0)
        else:
            x = y = 80
        self.top.geometry(f"{w}x{h}+{x}+{y}")
        self.top.transient(parent_top)
        self.top.deiconify()
        self.top.lift()
        self.top.update_idletasks()
        try:
            self.top.grab_set()                       # modal — only once visible
            self.top.focus_force()
        except tk.TclError:
            pass
        self.top.protocol("WM_DELETE_WINDOW",
                           lambda: dlg.events.put(("close",)))

    def _du_px(self, du_x: int, du_y: int) -> tuple[int, int]:
        return du_x * self.base_x // 4, du_y * self.base_y // 8

    def _place(self, widget, ctrl) -> None:
        tc = ctrl.template
        x, y = self._du_px(tc.x, tc.y)
        w, h = self._du_px(tc.cx, tc.cy)
        s = self.scale
        widget.place(x=x * s, y=y * s, width=w * s, height=h * s)

    def _build_control(self, ctrl) -> None:
        cls, style, text = ctrl.cls, ctrl.style, ctrl.text
        dlg = self.dlg
        if cls == "Static":
            if (style & 0xF) == SS_ICON:
                self._build_icon(ctrl)
                return
            anchor = "center" if (style & 0xF) == 0x1 else "w"   # SS_CENTER
            lbl = tk.Label(self.frame, text=text.replace("&", ""), anchor=anchor,
                           justify="left", font=self.font, bg=DIALOG_BG)
            self._place(lbl, ctrl)
            self.widgets[id(ctrl)] = lbl
        elif cls == "Button":
            sub = style & 0xF
            if sub in (BS_CHECKBOX, BS_AUTOCHECKBOX, BS_RADIOBUTTON, BS_AUTORADIOBUTTON):
                var = tk.IntVar(value=ctrl.checked)
                self.vars[ctrl.ctrl_id] = var
                w = tk.Checkbutton(self.frame, text=text.replace("&", ""),
                                   variable=var, anchor="w", font=self.font,
                                   bg=DIALOG_BG, activebackground=DIALOG_BG,
                                   command=lambda c=ctrl: self._on_check(c))
                self._place(w, ctrl)
                self.widgets[id(ctrl)] = w
            elif sub == BS_GROUPBOX:
                w = tk.LabelFrame(self.frame, text=text.replace("&", ""),
                                  font=self.font, bg=DIALOG_BG, fg="black")
                self._place(w, ctrl)
                self.widgets[id(ctrl)] = w
            else:                                     # push / default button
                w = tk.Button(self.frame, text=text.replace("&", ""),
                              font=self.font,
                              command=lambda c=ctrl: dlg.events.put(
                                  ("command", c.ctrl_id, BN_CLICKED)))
                self._place(w, ctrl)
                self.widgets[id(ctrl)] = w
        elif cls == "Edit":
            var = tk.StringVar(value=text)
            self.vars[ctrl.ctrl_id] = var
            var.trace_add("write",
                          lambda *_a, c=ctrl, v=var: setattr(c, "text", v.get()))
            e = tk.Entry(self.frame, textvariable=var, font=self.font)
            self._place(e, ctrl)
            self.widgets[id(ctrl)] = e
        elif cls == "ComboBox":
            var = tk.StringVar()
            self.vars[ctrl.ctrl_id] = var
            cb = ttk.Combobox(self.frame, textvariable=var, values=list(ctrl.items),
                              state="readonly", font=self.font)
            cb.bind("<<ComboboxSelected>>", lambda _e, c=ctrl, w=cb: self._on_combo(c, w))
            self._place(cb, ctrl)
            self.widgets[id(ctrl)] = cb
        else:                                         # unknown class: label it
            lbl = tk.Label(self.frame, text=f"[{cls}]", bg=DIALOG_BG)
            self._place(lbl, ctrl)
            self.widgets[id(ctrl)] = lbl

    def _build_icon(self, ctrl) -> None:
        from win16.icon import load_named_icon
        lbl = tk.Label(self.frame, bg=DIALOG_BG)
        try:
            decoded = load_named_icon(self.app.machine.exe, ctrl.text)
        except Exception as exc:  # noqa: BLE001 — a bad icon shouldn't kill the dialog
            print(f"[play] icon {ctrl.text!r} failed to decode: {exc}", flush=True)
            decoded = None
        if decoded is not None:
            iw, ih, rgba = decoded
            img = Image.frombytes("RGBA", (iw, ih), bytes(rgba))
            if self.scale != 1:
                img = img.resize((iw * self.scale, ih * self.scale), Image.NEAREST)
            self._icon_photo = ImageTk.PhotoImage(img)   # keep a reference
            lbl.config(image=self._icon_photo)
        x, y = self._du_px(ctrl.template.x, ctrl.template.y)
        lbl.place(x=x * self.scale, y=y * self.scale)
        self.widgets[id(ctrl)] = lbl

    def _on_check(self, ctrl) -> None:
        ctrl.checked = self.vars[ctrl.ctrl_id].get()
        self.dlg.events.put(("command", ctrl.ctrl_id, BN_CLICKED))

    def _on_combo(self, ctrl, widget) -> None:
        ctrl.sel = widget.current()
        self.dlg.events.put(("command", ctrl.ctrl_id, CBN_SELCHANGE))

    def sync(self) -> None:
        """Pull live control state (the game may have changed it) into widgets."""
        for ctrl in self.dlg.controls:
            widget = self.widgets.get(id(ctrl))
            if widget is None:
                continue
            if ctrl.cls == "Static":
                if (ctrl.style & 0xF) == SS_ICON:
                    continue                        # image label, no text sync
                if widget.cget("text") != ctrl.text.replace("&", ""):
                    widget.config(text=ctrl.text.replace("&", ""))
            elif ctrl.cls == "Edit":
                if self.vars[ctrl.ctrl_id].get() != ctrl.text:
                    self.vars[ctrl.ctrl_id].set(ctrl.text)
            elif ctrl.cls == "Button" and ctrl.ctrl_id in self.vars:
                if self.vars[ctrl.ctrl_id].get() != ctrl.checked:
                    self.vars[ctrl.ctrl_id].set(ctrl.checked)
            elif ctrl.cls == "ComboBox":
                if list(widget.cget("values")) != list(ctrl.items):
                    widget.config(values=list(ctrl.items))
                if 0 <= ctrl.sel < len(ctrl.items):
                    widget.current(ctrl.sel)

    def destroy(self) -> None:
        try:
            self.top.grab_release()
            self.top.destroy()
        except tk.TclError:
            pass


class ModalBox:
    """A MessageBox request shared between the CPU thread (which pumps a modal
    loop polling `done`) and the GUI thread (which shows it and sets `done`)."""

    def __init__(self, caption: str, text: str, mtype: int) -> None:
        from win16.msgbox import close_result
        self.caption = caption
        self.text = text
        self.mtype = mtype
        self.done = threading.Event()
        self.result = close_result(mtype)        # if closed without choosing


class MessageBoxView:
    """A non-blocking Win-3.1-style message box (so the GUI keeps ticking and
    rendering the game frame behind it while the CPU pumps WM_PAINT)."""

    TK_BITMAP = {0x30: "warning", 0x10: "error", 0x20: "question", 0x40: "info"}

    def __init__(self, app: "PlayApp", box: ModalBox) -> None:
        self.app = app
        self.box = box
        parent_view = next((v for v in app.views.values() if v.is_main), None)
        parent_top = parent_view.top if parent_view else app.root
        self.top = tk.Toplevel(parent_top)
        self.top.title(box.caption or "")
        self.top.resizable(False, False)
        self.top.configure(bg=DIALOG_BG)

        body = tk.Frame(self.top, bg=DIALOG_BG)
        body.pack(padx=16, pady=14)
        bmp = self.TK_BITMAP.get(box.mtype & 0xF0, "info")
        tk.Label(body, bitmap=bmp, bg=DIALOG_BG).pack(side="left", padx=(0, 14))
        tk.Label(body, text=box.text, bg=DIALOG_BG, justify="left",
                 font=("MS Sans Serif", 9)).pack(side="left")

        # Render the button set the MessageBox type asks for (OK / Yes-No /
        # Retry-Cancel / ...), each reporting its real Win16 ID.
        from win16.msgbox import buttons, close_result
        row = tk.Frame(self.top, bg=DIALOG_BG)
        row.pack(pady=(0, 12))
        first = None
        for label, result_id in buttons(box.mtype):
            b = tk.Button(row, text=label, width=8,
                          command=lambda r=result_id: self._choose(r))
            b.pack(side="left", padx=4)
            if first is None:
                first = b
        if first is not None:
            first.configure(default="active")
            first.focus_set()
        affirmative = buttons(box.mtype)[0][1]
        cancel = close_result(box.mtype)
        self.top.bind("<Return>", lambda _e: self._choose(affirmative))
        self.top.bind("<Escape>", lambda _e: self._choose(cancel))
        self.top.protocol("WM_DELETE_WINDOW", lambda: self._choose(cancel))

        self.top.update_idletasks()
        w, h = self.top.winfo_reqwidth(), self.top.winfo_reqheight()
        if parent_view is not None:
            px, py = parent_top.winfo_rootx(), parent_top.winfo_rooty()
            pw, ph = parent_top.winfo_width(), parent_top.winfo_height()
            x, y = px + max((pw - w) // 2, 0), py + max((ph - h) // 3, 0)
        else:
            x = y = 120
        self.top.geometry(f"+{x}+{y}")
        self.top.transient(parent_top)
        self.top.lift()
        try:
            self.top.grab_set()
            self.top.focus_force()
        except tk.TclError:
            pass

    def _choose(self, result_id: int) -> None:
        self.box.result = result_id
        self.box.done.set()

    def destroy(self) -> None:
        try:
            self.top.grab_release()
            self.top.destroy()
        except tk.TclError:
            pass


class PlayApp:
    def __init__(self, exe_path, winflags: int, speed: float, scale: int,
                 record: str | None = None, mute: bool = False,
                 snapshot_on_box: str | None = None,
                 game_name: str = "", hooks: bool = True,
                 resume: str | None = None) -> None:
        self.scale = scale
        self.game_name = game_name
        self.origin_x, self.origin_y = 60, 60
        if resume:
            from win16.vmsnap import load_snapshot
            self.machine = load_snapshot(
                resume, lambda: create_machine(exe_path, winflags=winflags))
            print(f"[play] resumed from snapshot {resume} "
                  f"(instruction {self.machine.cpu.instruction_count})", flush=True)
        else:
            self.machine = create_machine(exe_path, winflags=winflags)
        if hooks and game_name:
            from scripts.games import install_game_hooks
            n = install_game_hooks(game_name, self.machine)
            if n:
                print(f"[play] {n} game hook(s) installed for {game_name} "
                      f"(--no-hooks to disable)", flush=True)
        self.sys: Win16System = self.machine.api.services["system"]
        self.driver = InteractiveDriver(self.sys, speed=speed)
        self.status = "running"
        self.stopped = False
        self.snapshot_on_box = snapshot_on_box and snapshot_on_box.lower()
        self.recorder = None
        if record:
            from win16.demo import DemoRecorder
            self.recorder = DemoRecorder(record, self.machine.exe.path.name)
            self.machine.api.services["demo_recorder"] = self.recorder
            print(f"[play] recording demo to {record}", flush=True)

        # Host audio: square-wave synthesis of the SOUND.DRV voice stream.
        self.audio = None
        if not mute:
            from win16.audio import SquareWaveBackend
            self.audio = SquareWaveBackend()
            self.machine.api.services["sound_backend"] = self.audio
        self.views: dict[int, WindowView] = {}
        self.dialog_views: dict[int, DialogView] = {}
        self._dialog_reqs: list[tuple] = []
        self._dialog_lock = threading.Lock()
        self._box_reqs: list[ModalBox] = []
        self._box_lock = threading.Lock()
        self.box_view: "MessageBoxView | None" = None

        self.root = tk.Tk()
        self.root.withdraw()                    # game windows are the UI

        # Modal MessageBox host: the CPU thread's MessageBox runs a pumping
        # modal loop and only reads box.done — the GUI shows a non-blocking box.
        self.machine.api.services["messagebox_host"] = self
        # Dialog engine presentation host (called from the CPU thread).
        self.machine.api.services["dialog_ui"] = self

        self.worker = threading.Thread(target=self._run_cpu, daemon=True)
        self.worker.start()
        self.root.after(33, self._tick)

    # -- CPU worker -------------------------------------------------------------
    def _run_cpu(self) -> None:
        from dos_re.cpu import HaltExecution
        cpu = self.machine.cpu
        cpu.trace_enabled = False
        try:
            # Run in small chunks so a snapshot pause (F9) can park at an
            # instruction boundary even while the game busy-polls PeekMessage
            # (menus, in-game) and never calls GetMessage.  4096 instrs bounds
            # the pause latency to well under a frame.
            while self.driver.running:
                self.driver.check_pause()
                cpu.run(4096)
            self.status = "stopped"
        except HaltExecution:
            self.status = "app exited cleanly (DOS terminate)"
            print(f"[play] {self.status}", flush=True)
        except Exception as exc:  # noqa: BLE001 — console first, then the UI
            self.status = f"VM STOPPED - {type(exc).__name__}: {exc}"
            print(f"\n[play] {self.status}", file=sys.stderr, flush=True)
            print(f"[play] at CS:IP {cpu.s.cs:04X}:{cpu.s.ip:04X}, "
                  f"instruction {cpu.instruction_count}", file=sys.stderr)
            traceback.print_exc()
            print("[play] last trace lines:", file=sys.stderr)
            for line in cpu.trace[-10:]:
                print("   ", line, file=sys.stderr)
            for line in self.machine.api.call_log[-10:]:
                print("    api:", line, file=sys.stderr)
        self.stopped = True
        self.driver.running = False

    # -- modal MessageBox bridge (CPU thread <-> GUI thread) ---------------------
    def present_box(self, caption: str, text: str, mtype: int) -> "ModalBox":
        """Called from the CPU thread's MessageBox modal loop.  Queues a
        non-blocking box for the GUI to show and returns it; the CPU loop pumps
        WM_PAINT and polls box.done."""
        print(f"[game] MessageBox: {caption!r}: {text!r}", flush=True)
        box = ModalBox(caption, text, mtype)
        with self._box_lock:
            self._box_reqs.append(box)
        return box

    def _service_box(self) -> None:
        with self._box_lock:
            reqs, self._box_reqs = self._box_reqs, []
        for box in reqs:                        # one modal at a time in practice
            self.box_view = MessageBoxView(self, box)
            if self.snapshot_on_box and (self.snapshot_on_box in box.caption.lower()
                                         or self.snapshot_on_box in box.text.lower()):
                self._flush_windows()
                self._snapshot_inspection(
                    "".join(c for c in box.caption if c.isalnum())[:16] or "box")
        if self.box_view is not None and self.box_view.box.done.is_set():
            self.box_view.destroy()
            self.box_view = None

    # -- snapshots (F9) -----------------------------------------------------------
    def take_snapshot(self) -> None:
        from win16.vmsnap import SnapshotError, save_snapshot
        if not self.driver.pause_at_boundary():
            print("[play] snapshot failed: CPU did not reach a quiescent point "
                  "in time (stuck in a modal dialog/message box, or halted)",
                  file=sys.stderr)
            return
        try:
            stamp = time.strftime("%H%M%S")
            out = Path("artifacts") / "snapshots" / f"snap_{stamp}"
            save_snapshot(self.machine, out, note="taken from play.py (F9)",
                          game=self.game_name)
            print(f"[play] snapshot saved to {out}", flush=True)
        except SnapshotError as exc:
            print(f"[play] snapshot failed: {exc}", file=sys.stderr)
        finally:
            self.driver.resume()

    def _flush_windows(self) -> None:
        """Force the latest game frame onto the screen (e.g. the crash frame)
        before a modal blocks further rendering."""
        for view in self.views.values():
            try:
                view.force_render()
            except tk.TclError:
                pass
        self.root.update_idletasks()

    def _snapshot_inspection(self, tag: str) -> None:
        """Save an INSPECTION snapshot with the CPU parked in a modal handler.
        Memory + CPU + pixels are consistent (the worker is blocked); it is not
        resumable (the native modal call stack is not captured) — use it to
        examine state, and demos (--record) for reproducible replay."""
        from win16.vmsnap import SnapshotError, save_snapshot
        stamp = time.strftime("%H%M%S")
        out = Path("artifacts") / "snapshots" / f"box_{tag}_{stamp}"
        try:
            save_snapshot(self.machine, out,
                          note=f"inspection snapshot at MessageBox {tag!r} "
                               "(mid-modal; not resumable)",
                          game=self.game_name)
            print(f"[play] inspection snapshot at {tag!r} box -> {out}\n"
                  f"       memory+CPU+pixels valid; load with "
                  f"win16.vmsnap.load_snapshot for inspection.", flush=True)
        except SnapshotError as exc:
            print(f"[play] snapshot-on-box failed: {exc}", file=sys.stderr)

    # -- dialog host (called from the CPU thread) --------------------------------
    def show(self, dlg) -> None:
        with self._dialog_lock:
            self._dialog_reqs.append(("show", dlg))

    def update(self, dlg, ctrl) -> None:
        pass                                     # pull-based: _tick re-syncs widgets

    def close(self, dlg) -> None:
        with self._dialog_lock:
            self._dialog_reqs.append(("close", dlg))

    def now_ms(self) -> int:
        return self.driver.now_ms()

    def _service_dialogs(self) -> None:
        with self._dialog_lock:
            reqs, self._dialog_reqs = self._dialog_reqs, []
        for kind, dlg in reqs:
            if kind == "show" and dlg.handle not in self.dialog_views:
                self._flush_windows()          # show the last game frame behind it
                self.dialog_views[dlg.handle] = DialogView(self, dlg)
            elif kind == "close":
                view = self.dialog_views.pop(dlg.handle, None)
                if view is not None:
                    view.destroy()
        for view in self.dialog_views.values():
            view.sync()

    # -- main tick ---------------------------------------------------------------
    def _tick(self) -> None:
        self._service_dialogs()
        self._service_box()
        # Top-level frames AND captioned children (SimAnt's in-game panels) each
        # get their own OS window; plain WS_CHILD windows (ribbon/canvas/body)
        # composite into their parent's view.
        from win16 import compositor
        live = {w.handle: w for w in compositor.own_windows(self.sys)}
        for handle in [h for h in self.views if h not in live]:
            self.views.pop(handle).destroy()
        for handle, win in live.items():
            if handle not in self.views:
                self.views[handle] = WindowView(self, win)
        for view in self.views.values():
            view.sync()

        main = next((v for v in self.views.values() if v.is_main), None)
        if main is not None:
            # Promoted panels float ABOVE the main frame and travel with it (the
            # closest a real OS window gets to the game's in-frame MDI children).
            for v in self.views.values():
                if v is not main and not getattr(v, "_grouped", False):
                    try:
                        v.top.transient(main.top)
                    except tk.TclError:
                        pass
                    v._grouped = True
            clk = self.sys.clock_ms
            rec = f"   REC {self.recorder.records}" if self.recorder else ""
            main.status_var.set(f"{self.status}   t={clk // 1000}.{clk % 1000:03d}s{rec}")
            if self.stopped and "STOPPED" in self.status \
                    and not getattr(self, "_banner", None):
                self._banner = tk.Label(main.top, text=self.status + "  (see console)",
                                        bg="#c00000", fg="white",
                                        font=("Consolas", 10, "bold"))
                self._banner.pack(fill="x")

        if self.stopped and ("exited cleanly" in self.status or not self.views):
            self.on_close()
            return
        self.root.after(33, self._tick)

    def on_close(self) -> None:
        self.driver.stop()
        if self.audio is not None:
            self.audio.close()
            self.audio = None
        if self.recorder is not None:
            self.recorder.close()
            print(f"[play] demo saved: {self.recorder.path} "
                  f"({self.recorder.records} records)", flush=True)
            self.recorder = None
        # Release a CPU thread parked in a modal MessageBox loop.
        with self._box_lock:
            for box in self._box_reqs:
                box.done.set()
            self._box_reqs.clear()
        if self.box_view is not None:
            self.box_view.box.done.set()
        self.root.after(150, self.root.destroy)

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    ap = argparse.ArgumentParser(description="Play a Win16 game in the VM.")
    ap.add_argument("--game", default="ppython",
                    help=f"which game to run (default: ppython). "
                         f"known: {', '.join(sorted(GAMES))}")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="time multiplier (1.0 = real speed)")
    ap.add_argument("--scale", type=int, default=1,
                    help="integer pixel scale (e.g. 2 doubles the windows)")
    ap.add_argument("--record", metavar="FILE", default=None,
                    help="record a demo (message + dialog event stream) to FILE")
    ap.add_argument("--mute", action="store_true", help="disable host audio output")
    ap.add_argument("--snapshot-on-box", metavar="TEXT", default=None,
                    help="save an inspection snapshot whenever a MessageBox whose "
                         "caption/text contains TEXT appears (e.g. 'Collision')")
    ap.add_argument("--no-hooks", action="store_true",
                    help="run pure ASM (skip the game's lifted-island hooks)")
    ap.add_argument("--resume", metavar="SNAP_DIR", default=None,
                    help="start from a snapshot directory (taken with F9) "
                         "instead of a cold boot — exact same state")
    args = ap.parse_args()
    game = args.game
    # A snapshot records its own game — resuming one auto-selects it, so
    # `--resume DIR` alone works without also repeating `--game`.
    if args.resume:
        from win16.vmsnap import snapshot_game
        snap_game = snapshot_game(args.resume)
        if not snap_game:
            # Pre-v3 snapshot (no game field): match its recorded EXE name
            # against the registry so old snapshots still auto-select.
            import json
            exe_name = json.loads(
                (Path(args.resume) / "state.json").read_text()).get("exe", "")
            snap_game = next((n for n in GAMES
                              if game_exe(n).name.upper() == exe_name.upper()), "")
        if snap_game and "--game" not in sys.argv:
            game = snap_game
        elif snap_game and snap_game != game:
            raise SystemExit(
                f"snapshot is for game {snap_game!r} but --game {game!r} was "
                f"given; omit --game or pass --game {snap_game}")
    exe = game_exe(game)
    if not exe.exists():
        raise SystemExit(f"{exe} not found — put the game files under assets/")
    PlayApp(exe, game_winflags(game), args.speed, args.scale,
            record=args.record, mute=args.mute,
            snapshot_on_box=args.snapshot_on_box,
            game_name=game, hooks=not args.no_hooks,
            resume=args.resume).run()


if __name__ == "__main__":
    main()
