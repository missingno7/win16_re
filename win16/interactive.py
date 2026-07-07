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
        self._t0 = time.monotonic()
        sysobj.message_source = self._next

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

    # -- CPU thread side (called inside GetMessage) ------------------------
    def _drain_input(self) -> None:
        with self._cond:
            pending, self._input = self._input, []
        now = self.now_ms()
        for hwnd, msg, wparam, lparam in pending:
            self.sys.msg_queue.append((hwnd, msg, wparam, lparam, now, 0))

    def _next(self, sysobj):
        while True:
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
                    return (key[0], WM_TIMER, key[1], 0, now, 0)
                due = when

            with self._cond:
                if self._input or not self.running:
                    continue
                timeout = 0.03
                if due is not None:
                    timeout = min(timeout, max((due - now) / 1000.0 / self.speed, 0.0))
                self._cond.wait(timeout)
