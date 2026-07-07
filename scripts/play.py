"""Play Paulie Python interactively — a real window, keyboard + mouse control.

    python scripts/play.py [--speed N] [--scale N]

The VM runs on a worker thread; this (main) thread is the GUI: it renders every
Win16 window onto a virtual "desktop" and forwards your keyboard/mouse into the
game.  Controls:

    Arrow keys   steer Paulie
    F2           New game        F3  toggle sound     F4  pause
    F5           high scores     F1  help             F10 exit
    mouse        move / click (when the game is in mouse-control mode)

The game keeps its own window layout; close the window to quit.
"""
from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tkinter as tk

from PIL import Image, ImageDraw, ImageTk

from ppython.runtime import assets_present, create_machine
from win16.api.core import Win16ApiGap
from win16.api.objects import Window
from win16.api.system import Win16System
from win16.interactive import InteractiveDriver

try:
    from dos_re.cpu import HaltExecution
except ImportError:  # pragma: no cover - dos_re always present at runtime
    HaltExecution = ()

DESK_W, DESK_H = 640, 480
CAPTION_H = 18
DESKTOP_BG = (0, 128, 128)          # Windows 3.1 default teal
CAPTION_BG = (0, 0, 128)            # active caption blue
CAPTION_FG = (255, 255, 255)

# Windows messages.
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
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
    ch = event.char
    if ch and len(ch) == 1:
        o = ord(ch.upper())
        if 0x30 <= o <= 0x5A:
            return o
    ks = event.keysym
    if len(ks) == 1 and ks.upper().isalnum():
        return ord(ks.upper())
    return None


class PlayApp:
    def __init__(self, speed: float, scale: int) -> None:
        self.scale = scale
        self.machine = create_machine()
        self.sys: Win16System = self.machine.api.services["system"]
        self.driver = InteractiveDriver(self.sys, speed=speed)
        self.active_hwnd = 0
        self.status = "booting…"
        self._photo = None

        self.root = tk.Tk()
        self.root.title("Paulie Python — VM-less port (dos_re / win16)")
        self.root.resizable(False, False)
        self.canvas = tk.Canvas(self.root, width=DESK_W * scale,
                                height=DESK_H * scale, highlightthickness=0,
                                bg="#%02x%02x%02x" % DESKTOP_BG)
        self.canvas.pack()
        self.status_var = tk.StringVar(value=self.status)
        tk.Label(self.root, textvariable=self.status_var, anchor="w",
                 font=("Consolas", 9)).pack(fill="x")

        self._bind_input()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.worker = threading.Thread(target=self._run_cpu, daemon=True)
        self.worker.start()
        self.root.after(33, self._render)

    # -- CPU worker --------------------------------------------------------
    def _run_cpu(self) -> None:
        cpu = self.machine.cpu
        cpu.trace_enabled = False
        try:
            while self.driver.running:
                cpu.step()
            self.status = "stopped"
        except HaltExecution:
            self.status = "the app exited (WinMain returned)"
        except Win16ApiGap as exc:
            self.status = f"reached an unimplemented API: {exc}"
        except Exception as exc:  # noqa: BLE001 — surface everything in the UI
            self.status = f"{type(exc).__name__}: {exc}"
        self.driver.running = False

    # -- input -------------------------------------------------------------
    def _bind_input(self) -> None:
        c = self.canvas
        c.focus_set()
        c.bind("<KeyPress>", self._on_key_down)
        c.bind("<KeyRelease>", self._on_key_up)
        c.bind("<Motion>", lambda e: self._on_mouse(e, WM_MOUSEMOVE, 0))
        c.bind("<Button-1>", lambda e: self._on_mouse(e, WM_LBUTTONDOWN, MK_LBUTTON, activate=True))
        c.bind("<ButtonRelease-1>", lambda e: self._on_mouse(e, WM_LBUTTONUP, 0))
        c.bind("<Button-3>", lambda e: self._on_mouse(e, WM_RBUTTONDOWN, MK_RBUTTON, activate=True))
        c.bind("<ButtonRelease-3>", lambda e: self._on_mouse(e, WM_RBUTTONUP, 0))

    def _target_hwnd(self) -> int:
        if self.active_hwnd and isinstance(self.sys.handles.get(self.active_hwnd), Window):
            return self.active_hwnd
        main = self._main_window()
        return main.handle if main else 0

    def _main_window(self) -> Window | None:
        for win in self.sys.windows:
            if win.wndclass.name == "PYTHON":
                return win
        return self.sys.windows[0] if self.sys.windows else None

    def _on_key_down(self, event) -> None:
        vk = keysym_to_vk(event)
        if vk is not None:
            self.driver.post_input(self._target_hwnd(), WM_KEYDOWN, vk, 0x0001)

    def _on_key_up(self, event) -> None:
        vk = keysym_to_vk(event)
        if vk is not None:
            self.driver.post_input(self._target_hwnd(), WM_KEYUP, vk, 0xC0000001)

    def _window_at(self, px: int, py: int) -> Window | None:
        for win in reversed(self.sys.windows):
            if win.visible and win.x <= px < win.x + win.w and win.y <= py < win.y + win.h:
                return win
        return None

    def _on_mouse(self, event, msg: int, mk: int, *, activate: bool = False) -> None:
        px, py = event.x // self.scale, event.y // self.scale
        win = self._window_at(px, py)
        if win is None:
            return
        if activate:
            self.active_hwnd = win.handle
        cx, cy = px - win.x, py - win.y
        lparam = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
        self.driver.post_input(win.handle, msg, mk, lparam)

    # -- rendering ---------------------------------------------------------
    def _render(self) -> None:
        desktop = Image.new("RGB", (DESK_W, DESK_H), DESKTOP_BG)
        draw = ImageDraw.Draw(desktop)
        for win in self.sys.windows:
            if not win.visible:
                continue
            surf = win.surface
            try:
                img = Image.frombytes("RGB", (surf.w, surf.h), bytes(surf.pixels))
            except ValueError:
                continue
            cap_y = max(win.y - CAPTION_H, 0)
            draw.rectangle([win.x, cap_y, win.x + win.w - 1, win.y - 1], fill=CAPTION_BG)
            draw.text((win.x + 3, cap_y + 4), win.title or win.wndclass.name, fill=CAPTION_FG)
            desktop.paste(img, (win.x, win.y))
        if self.scale != 1:
            desktop = desktop.resize((DESK_W * self.scale, DESK_H * self.scale),
                                     Image.NEAREST)
        self._photo = ImageTk.PhotoImage(desktop)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self._photo, anchor="nw")
        clk = self.sys.clock_ms
        self.status_var.set(f"{self.status}   |   t={clk // 1000}.{clk % 1000:03d}s   "
                            f"windows={sum(w.visible for w in self.sys.windows)}")
        self.root.after(33, self._render)

    def _on_close(self) -> None:
        self.driver.stop()
        self.root.after(120, self.root.destroy)

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    ap = argparse.ArgumentParser(description="Play Paulie Python in the VM.")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="time multiplier (1.0 = real speed)")
    ap.add_argument("--scale", type=int, default=1,
                    help="integer pixel scale for the window (e.g. 2)")
    args = ap.parse_args()
    if not assets_present():
        raise SystemExit("assets/PYTHON.EXE not found — put the game files in assets/")
    PlayApp(args.speed, args.scale).run()


if __name__ == "__main__":
    main()
