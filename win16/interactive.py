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


class BoundaryParked(Exception):
    """A lifted boundary head parked (dos_re lift/emit ``boundary_heads``).

    Raised by the observer installed via ``arm_boundary_parks``: CS:IP has
    already been re-pointed at the head's RESUME entry, so the CPU worker
    must treat this as a YIELD, not a VM stop — handle it with
    ``boundary_yield`` and keep running; the next ``cpu.run`` re-enters the
    lifted body at the resume entry (zero interpreted instructions).
    """

    def __init__(self, head_cs: int, head_ip: int, resume_ip: int) -> None:
        super().__init__(f"boundary park at {head_cs:04X}:{head_ip:04X} "
                         f"(resume {head_cs:04X}:{resume_ip:04X})")
        self.head = (head_cs, head_ip)
        self.resume_ip = resume_ip


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
        sysobj.interactive = True           # GetTickCount tracks the wall clock
        # Let PeekMessage flush posted input into the queue too — SimAnt's menus
        # / in-game spin on PeekMessage and never call GetMessage, so without
        # this a click would sit undelivered until the next GetMessage.
        sysobj.input_drainer = self._drain_input
        # Keep a long VM callback (the sim-tick TimerProc, where in-game spends
        # ALL its time) pausable, so F9/snapshot and the window stay responsive.
        sysobj.yield_check = self.check_pause
        # ...and uncapped: a live sim-tick legitimately busy-waits on the real
        # clock and on input (a paused game, a "press a key" prompt), so the
        # runaway cap would kill a perfectly valid wait mid-callback.  Interactive
        # is user-interruptible (close window / pause), so no cap is needed.
        sysobj.callback_max_steps = None

    # -- boundary parks (fact-declared wall-clock wait loops) ----------------
    def arm_boundary_parks(self, cpu, *, spin_sleep: float = 0.001) -> None:
        """Arm the lifted graph's boundary observers for INTERACTIVE pacing.

        A fact-declared wait head (dos_re lift/emit ``boundary_heads`` — e.g.
        a splash timeout spinning on GetTickCount) polls the WALL clock, but
        interactively that clock only advances between ``cpu.run`` chunks
        (``check_pause``): inside one lifted invocation the spin can never
        exit and would die at the module's MAX_ITERATIONS runaway guard.
        The armed observer parks the spin on every pass — sleeps the
        pacing-spin cost (so the wait doesn't burn a host core), re-points
        CS:IP at the head's RESUME entry and raises :class:`BoundaryParked`,
        unwinding the lifted Python chain back to the nearest step loop.
        EVERY step loop that drives lifted code on an interactive host must
        catch it as a yield: the CPU worker (``boundary_yield``) and
        ``win16.callback.call_far``'s chunk loop (a wait head reached inside
        a WndProc/TimerProc callback parks there) — the next ``cpu.step()``
        resumes INSIDE the lifted body at the RESUME entry, and abandoned
        outer lifted frames re-establish through their own call-continuation
        resume entries as the guest stack unwinds (the unwind re-entry rule).
        Headless/demo runs never arm this: the emitted observer is inert
        with ``cpu.boundary_hook`` None.
        """

        def hook(cpu2, head_cs: int, head_ip: int, resume_ip: int) -> None:
            time.sleep(spin_sleep)
            s = cpu2.s
            s.cs, s.ip = head_cs & 0xFFFF, resume_ip & 0xFFFF
            raise BoundaryParked(head_cs, head_ip, resume_ip)

        cpu.boundary_hook = hook

    def boundary_yield(self) -> None:
        """Handle one :class:`BoundaryParked` in the CPU worker loop: advance
        the wall clock / honour a pause request (``check_pause``) and keep
        running.  The spin exits by its own original condition (the game
        re-polls the clock on resume); the pacing sleep already happened in
        the observer."""
        self.check_pause()

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
        """Called by the CPU worker (and by call_far between chunks) at
        instruction-chunk boundaries.  Advances the wall clock so GetTickCount
        keeps ticking at REAL time INSIDE a long callback (SimAnt's sim-tick
        paces its frame on GetTickCount; without this it would spin/overrun),
        then parks if a snapshot pause was requested."""
        now = self.now_ms()
        if now > self.sys.clock_ms:
            self.sys.clock_ms = now
        # Sample the (instruction -> wall tick) map periodically so a v4 demo can
        # reproduce GetTickCount during input-free busy-waits (splash timeouts,
        # sim-tick pacing) — the recorder rate-limits these.
        recorder = self._recorder()
        if recorder is not None:
            recorder.clock_sample(self._instr(), now)
        if self._pause_requested:
            self._park()

    def _instr(self) -> int:
        return self.sys.machine.cpu.instruction_count

    def _recorder(self):
        machine = getattr(self.sys, "machine", None)
        return (machine.api.services.get("demo_recorder")
                if machine is not None else None)

    def _drain_input(self) -> None:
        with self._cond:
            pending, self._input = self._input, []
        now = self.now_ms()
        recorder = self._recorder()
        for hwnd, msg, wparam, lparam in pending:
            m = (hwnd, msg, wparam, lparam, now, 0)
            self.sys.msg_queue.append(m)
            # Feed polled state (async keys / cursor) AS INPUT ARRIVES, not when
            # a message is consumed — SimAnt's caste-slider drag polls
            # GetAsyncKeyState without pumping, so consume-time noting would never
            # see the button release.  get_message/peek_message skip their own
            # _note_input while a drainer is attached (no double-note).
            self.sys._note_input(m)
            if recorder is not None:
                # The v4 timeline is these raw arrivals, each keyed to the
                # instruction count at which it landed: replay injects it into
                # the queue at that same instruction, and the game's own pump
                # (GetMessage / PeekMessage) fetches it exactly as it did live.
                recorder.arrival(m, self._instr())

    def _next(self, sysobj):
        while True:
            if self._pause_requested:
                self._park()
            self._drain_input()
            if not self.running or sysobj.quit_posted is not None:
                recorder = self._recorder()
                if recorder is not None and sysobj.quit_posted is not None:
                    recorder.quit(self._instr())        # WM_QUIT ends the timeline
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
