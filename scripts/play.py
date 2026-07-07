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
from tkinter import messagebox as tk_messagebox
from tkinter import ttk

from PIL import Image, ImageTk

from ppython.runtime import assets_present, create_machine
from win16.api.core import Win16ApiGap
from win16.api.objects import Window
from win16.api.system import Win16System
from win16.dialog import du_to_px
from win16.interactive import InteractiveDriver
from win16.menu import MF_CHECKED, MF_DISABLED, MF_GRAYED, parse_menu

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
        self._menu_entries: list[tuple[tk.Menu, int, int, int]] = []
        #                    (menu widget, entry index, item id, initial flags)
        self._menu_applied: dict[tuple[str, int], tuple[str, str]] = {}
        self._last_geo = None

        self.top = tk.Toplevel(app.root)
        self.top.title(win.title or win.wndclass.name)
        self.top.resizable(False, False)
        w, h = win.client_size
        self.canvas = tk.Canvas(self.top, width=w * self.scale,
                                height=h * self.scale, highlightthickness=0,
                                bg="black")
        self.canvas.pack()
        self.is_main = win.wndclass.menu_name is not None
        if self.is_main:
            self._build_menubar()
            self.status_var = tk.StringVar(value="ready")
            tk.Label(self.top, textvariable=self.status_var, anchor="w",
                     font=("Consolas", 9)).pack(fill="x")
        self._place()
        self._bind_input()
        self.top.protocol("WM_DELETE_WINDOW", app.on_close)

    # -- placement ----------------------------------------------------------
    def _place(self) -> None:
        x = self.app.origin_x + self.win.x * self.scale
        y = self.app.origin_y + self.win.y * self.scale
        self.top.geometry(f"+{x}+{y}")
        self._last_geo = (self.win.x, self.win.y)

    # -- the game's menu bar --------------------------------------------------
    def _build_menubar(self) -> None:
        resources = self.app.machine.exe.find_resources("MENU")
        if not resources:
            return
        bar = tk.Menu(self.top)
        for item in parse_menu(resources[0].data):
            self._add_menu_item(bar, item)
        self.top.config(menu=bar)

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
        parent.add_command(
            label=text, accelerator=accel or None,
            command=lambda: self.app.driver.post_input(hwnd, WM_COMMAND, cmd_id, 0))
        self._menu_entries.append(
            (parent, parent.index("end"), cmd_id, item.flags))

    def _sync_menu_state(self) -> None:
        """Mirror the game's menu state: grayed items are unclickable (real
        USER never delivers WM_COMMAND for them) and checks show live.

        Only entries whose computed state CHANGED are reconfigured —
        reconfiguring an open tkinter menu every tick makes it flicker and
        fight the user's selection."""
        flags_now = {}
        if self.win.menu_obj is not None:
            flags_now = self.win.menu_obj.item_flags
        for menu, index, cmd_id, initial in self._menu_entries:
            flags = flags_now.get(cmd_id, initial)
            state = "disabled" if flags & (MF_GRAYED | MF_DISABLED) else "normal"
            check = "✓ " if flags & MF_CHECKED else ""
            key = (str(menu), index)
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
        cx, cy = event.x // self.scale, event.y // self.scale
        lparam = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
        self.app.driver.post_input(self.win.handle, msg, mk, lparam)

    # -- per-tick sync ---------------------------------------------------------
    def sync(self) -> None:
        win = self.win
        if (win.x, win.y) != self._last_geo:
            self._place()
        surf = win.surface
        if surf.version != self._last_version:
            self._last_version = surf.version
            self._redraw(surf)
        if self.top.title() != (win.title or win.wndclass.name):
            self.top.title(win.title or win.wndclass.name)
        if self.is_main:
            self._sync_menu_state()

    def _redraw(self, surf) -> None:
        w, h = self.win.client_size
        if surf.w == w and surf.h == h and int(self.canvas["width"]) != w * self.scale:
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


class PlayApp:
    def __init__(self, speed: float, scale: int,
                 record: str | None = None, mute: bool = False) -> None:
        self.scale = scale
        self.origin_x, self.origin_y = 60, 60
        self.machine = create_machine()
        self.sys: Win16System = self.machine.api.services["system"]
        self.driver = InteractiveDriver(self.sys, speed=speed)
        self.status = "running"
        self.stopped = False
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
        self._console_counts = {"boxes": 0}
        self.views: dict[int, WindowView] = {}
        self.dialog_views: dict[int, DialogView] = {}
        self._dialog_reqs: list[tuple] = []
        self._dialog_lock = threading.Lock()
        self._pending_box: tuple | None = None
        self._box_lock = threading.Lock()

        self.root = tk.Tk()
        self.root.withdraw()                    # game windows are the UI

        # Real modal MessageBox service: the CPU thread blocks here until the
        # GUI thread shows the box and the user dismisses it.
        self.machine.api.services["messagebox_ui"] = self._messagebox_blocking
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
            while self.driver.running:
                cpu.step()
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
    def _messagebox_blocking(self, caption: str, text: str, mtype: int) -> int:
        print(f"[game] MessageBox: {caption!r}: {text!r}", flush=True)
        done = threading.Event()
        result = {"rc": 1}
        with self._box_lock:
            self._pending_box = (caption, text, mtype, done, result)
        done.wait(timeout=120)                  # never wedge the VM forever
        return result["rc"]

    # -- snapshots (F9) -----------------------------------------------------------
    def take_snapshot(self) -> None:
        from win16.vmsnap import SnapshotError, save_snapshot
        if not self.driver.pause_at_boundary():
            print("[play] snapshot failed: CPU did not reach a message "
                  "boundary (mid-frame or modal dialog open)", file=sys.stderr)
            return
        try:
            stamp = time.strftime("%H%M%S")
            out = Path("artifacts") / "snapshots" / f"snap_{stamp}"
            save_snapshot(self.machine, out, note="taken from play.py (F9)")
            print(f"[play] snapshot saved to {out}", flush=True)
        except SnapshotError as exc:
            print(f"[play] snapshot failed: {exc}", file=sys.stderr)
        finally:
            self.driver.resume()

    def _show_pending_box(self) -> None:
        with self._box_lock:
            pending, self._pending_box = self._pending_box, None
        if pending is None:
            return
        caption, text, mtype, done, result = pending
        icon = mtype & 0xF0
        if icon == 0x30:                        # MB_ICONEXCLAMATION
            tk_messagebox.showwarning(caption, text)
        elif icon == 0x10:                      # MB_ICONHAND
            tk_messagebox.showerror(caption, text)
        else:
            tk_messagebox.showinfo(caption, text)
        result["rc"] = 1                        # IDOK (only OK boxes observed)
        done.set()

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
        live = {w.handle: w for w in self.sys.windows if w.visible}
        for handle in [h for h in self.views if h not in live]:
            self.views.pop(handle).destroy()
        for handle, win in live.items():
            if handle not in self.views:
                self.views[handle] = WindowView(self, win)
        for view in self.views.values():
            view.sync()

        self._show_pending_box()

        main = next((v for v in self.views.values() if v.is_main), None)
        if main is not None:
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
        # Release a CPU thread blocked inside a modal MessageBox.
        with self._box_lock:
            if self._pending_box is not None:
                self._pending_box[3].set()
                self._pending_box = None
        self.root.after(150, self.root.destroy)

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    ap = argparse.ArgumentParser(description="Play Paulie Python in the VM.")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="time multiplier (1.0 = real speed)")
    ap.add_argument("--scale", type=int, default=1,
                    help="integer pixel scale (e.g. 2 doubles the windows)")
    ap.add_argument("--record", metavar="FILE", default=None,
                    help="record a demo (message + dialog event stream) to FILE")
    ap.add_argument("--mute", action="store_true", help="disable host audio output")
    args = ap.parse_args()
    if not assets_present():
        raise SystemExit("assets/PYTHON.EXE not found — put the game files in assets/")
    PlayApp(args.speed, args.scale, record=args.record, mute=args.mute).run()


if __name__ == "__main__":
    main()
