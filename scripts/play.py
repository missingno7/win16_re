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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tkinter as tk
from tkinter import messagebox as tk_messagebox

from PIL import Image, ImageTk

from ppython.runtime import assets_present, create_machine
from win16.api.core import Win16ApiGap
from win16.api.objects import Window
from win16.api.system import Win16System
from win16.interactive import InteractiveDriver
from win16.menu import MF_CHECKED, MF_DISABLED, MF_GRAYED, parse_menu

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


class PlayApp:
    def __init__(self, speed: float, scale: int) -> None:
        self.scale = scale
        self.origin_x, self.origin_y = 60, 60
        self.machine = create_machine()
        self.sys: Win16System = self.machine.api.services["system"]
        self.driver = InteractiveDriver(self.sys, speed=speed)
        self.status = "running"
        self.stopped = False
        self.views: dict[int, WindowView] = {}
        self._pending_box: tuple | None = None
        self._box_lock = threading.Lock()

        self.root = tk.Tk()
        self.root.withdraw()                    # game windows are the UI

        # Real modal MessageBox service: the CPU thread blocks here until the
        # GUI thread shows the box and the user dismisses it.
        self.machine.api.services["messagebox_ui"] = self._messagebox_blocking

        self.worker = threading.Thread(target=self._run_cpu, daemon=True)
        self.worker.start()
        self.root.after(33, self._tick)

    # -- CPU worker -------------------------------------------------------------
    def _run_cpu(self) -> None:
        cpu = self.machine.cpu
        cpu.trace_enabled = False
        try:
            while self.driver.running:
                cpu.step()
            self.status = "stopped"
        except Win16ApiGap as exc:
            self.status = f"VM STOPPED - unimplemented API: {exc}"
        except Exception as exc:  # noqa: BLE001 — surface everything in the UI
            self.status = f"VM STOPPED - {type(exc).__name__}: {exc}"
        self.stopped = True
        self.driver.running = False

    # -- modal MessageBox bridge (CPU thread <-> GUI thread) ---------------------
    def _messagebox_blocking(self, caption: str, text: str, mtype: int) -> int:
        done = threading.Event()
        result = {"rc": 1}
        with self._box_lock:
            self._pending_box = (caption, text, mtype, done, result)
        done.wait(timeout=120)                  # never wedge the VM forever
        return result["rc"]

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

    # -- main tick ---------------------------------------------------------------
    def _tick(self) -> None:
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
            skipped = self.machine.api.services.get("skipped_ui", [])
            note = f"   [skipped: {skipped[-1][0]} {skipped[-1][1]}]" if skipped else ""
            clk = self.sys.clock_ms
            main.status_var.set(f"{self.status}   t={clk // 1000}.{clk % 1000:03d}s{note}")
            if self.stopped and not getattr(self, "_banner", None):
                self._banner = tk.Label(main.top, text=self.status, bg="#c00000",
                                        fg="white", font=("Consolas", 10, "bold"))
                self._banner.pack(fill="x")

        if not self.views and self.stopped:
            self.root.destroy()
            return
        self.root.after(33, self._tick)

    def on_close(self) -> None:
        self.driver.stop()
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
    args = ap.parse_args()
    if not assets_present():
        raise SystemExit("assets/PYTHON.EXE not found — put the game files in assets/")
    PlayApp(args.speed, args.scale).run()


if __name__ == "__main__":
    main()
