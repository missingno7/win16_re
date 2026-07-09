"""Real-time interactive driver for the Win16 message pump.

Installs itself as `Win16System.message_source` so GetMessage paces to
wall-clock time instead of auto-advancing the virtual clock: posted input is
delivered first, then pending repaints, then timers as they come due in real
time; otherwise the CPU thread blocks (releasing the host) until input arrives
or the next timer is due.

Game-agnostic and GUI-toolkit-agnostic — it only knows the Win16 message
model.  The CPU runs on one thread and calls `_next` from inside GetMessage;
a GUI thread feeds input via `post_input` / stops via `stop`.
"""
from __future__ import annotations

import threading
import time

WM_PAINT = 0x000F
WM_TIMER = 0x0113


class InteractiveDriver:
    def __init__(self, sysobj, *, speed: float = 1.0) -> None:
        self.sys = sysobj
        self.speed = speed
        self.running = True
        self._input: list[tuple[int, int, int, int]] = []
        self._cond = threading.Condition()
        # Resume the system's virtual clock (nonzero when the machine was
        # restored from a snapshot): now_ms() must continue from clock_ms or
        # every armed timer sits "in the future" for that many REAL seconds.
        self._t0 = time.monotonic() - sysobj.clock_ms / 1000.0 / max(speed, 1e-9)
        self._pause_requested = False
        self.paused = threading.Event()     # set while the CPU thread is
        self._resume = threading.Event()    # parked at the message boundary
        sysobj.message_source = self._next
        # Let PeekMessage flush posted input into the queue too — SimAnt's menus
        # / in-game spin on PeekMessage and never call GetMessage, so without
        # this a click would sit undelivered until the next GetMessage.
        sysobj.input_drainer = self._drain_input
        # Keep a long VM callback (the sim-tick TimerProc, where in-game spends
        # ALL its time) pausable, so F9/snapshot and the window stay responsive.
        sysobj.yield_check = self.check_pause

    # -- host (GUI thread) side --------------------------------------------
    def now_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000 * self.speed)

    def post_input(self, hwnd: int, msg: int, wparam: int, lparam: int) -> None:
        with self._cond:
            self._input.append((hwnd, msg, wparam, lparam))
            self._cond.notify()

    def stop(self) -> None:
        with self._cond:
            self.running = False
            self._cond.notify()
        self._resume.set()                  # release a parked CPU thread

    def pause_at_boundary(self, timeout: float = 3.0) -> bool:
        """Ask the CPU thread to park at its next quiescent point — either a
        GetMessage boundary OR an instruction-chunk boundary (see check_pause).
        The latter lets a snapshot be taken while the game BUSY-POLLS via
        PeekMessage (SimAnt's menus and in-game loops never call GetMessage for
        long stretches), which used to time out here.  Returns True once parked.
        An instruction boundary is as valid a snapshot point as a message one:
        no Python handler loop is open between top-level CPU steps.  (A modal
        DialogBox/MessageBox IS a nested Python loop — save_snapshot still
        refuses those, and the CPU won't reach these checks inside one anyway.)"""
        self._pause_requested = True
        with self._cond:
            self._cond.notify()
        return self.paused.wait(timeout)

    def resume(self) -> None:
        self._pause_requested = False
        self.paused.clear()
        self._resume.set()

    # -- CPU thread side ---------------------------------------------------
    def _park(self) -> None:
        """Block the CPU thread here until resume() — the caller has reached a
        quiescent point the host asked to pause at."""
        self.paused.set()
        self._resume.wait()
        self._resume.clear()

    def check_pause(self) -> None:
        """Called by the CPU worker between instruction chunks: park here if a
        pause was requested.  This is the busy-poll snapshot point — the game
        may spin in PeekMessage without ever hitting GetMessage, so `_next`
        alone would never see the request."""
        if self._pause_requested:
            self._park()

    def _drain_input(self) -> None:
        with self._cond:
            pending, self._input = self._input, []
        now = self.now_ms()
        for hwnd, msg, wparam, lparam in pending:
            self.sys.msg_queue.append((hwnd, msg, wparam, lparam, now, 0))

    def _next(self, sysobj):
        while True:
            if self._pause_requested:
                self._park()
            self._drain_input()
            if not self.running or sysobj.quit_posted is not None:
                return None
            if sysobj.msg_queue:
                return sysobj.msg_queue.popleft()
            for win in sysobj.windows:
                if win.visible and win.dirty:
                    return (win.handle, WM_PAINT, 0, 0, self.now_ms(), 0)

            now = self.now_ms()
            sysobj.clock_ms = now
            due = None
            if sysobj.timer_due:
                key, when = min(sysobj.timer_due.items(), key=lambda kv: kv[1])
                if now >= when:
                    # Reschedule from now (drop missed ticks — no catch-up storm).
                    sysobj.timer_due[key] = now + sysobj.timers[key]
                    proc = sysobj.timer_procs.get(key, 0)   # lParam = TimerProc
                    return (key[0], WM_TIMER, key[1], proc, now, 0)
                due = when

            with self._cond:
                if self._input or not self.running:
                    continue
                timeout = 0.03
                if due is not None:
                    timeout = min(timeout, max((due - now) / 1000.0 / self.speed, 0.0))
                self._cond.wait(timeout)
