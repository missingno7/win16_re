"""Deterministic demos: record and replay the game's input timeline (v4).

The Win16 analogue of dos_re's instruction-keyed input demos.  The anchor is
the **instruction count**, not the message-fetch API — this is what makes a
peek-driven modal loop replay deterministically.

Earlier formats (v1-v3) recorded input as *consumption* events keyed to the
fetch API (GetMessage "m" vs PeekMessage "p" vs arrival "a"), and replay served
each record only to the exact API + stream position that consumed it live.
That deadlocks any loop whose fetch path drifts even slightly from the
recording: a message recorded under GetMessage is invisible to a PeekMessage
that wants it, and a click recorded behind a message can never reach a
busy-poll that only peeks.  (SimAnt's cold-start "click to continue" modal loop
hit exactly this — see docs/run_status.md 2026-07-10.)

v4 records the raw INPUT TIMELINE instead:

  * "i" — an input arrival (a host event: key / mouse / etc.), stamped with the
    instruction_count at which it arrived live and its virtual-clock tick.
  * "c" — a clock sample (instruction_count -> tick), taken periodically so the
    replay can reproduce GetTickCount during input-free stretches.
  * "d" — a modal dialog event (consumed in order by the dialog engine).
  * "quit" — WM_QUIT, at the instruction where it was posted.

On replay a DemoDriver injects each "i" arrival into `msg_queue` (and notes its
polled state) exactly when the machine reaches that instruction count, and
reproduces GetTickCount by interpolating the (instr, tick) samples.  The game's
OWN message pump (GetMessage / PeekMessage / GetAsyncKeyState) then fetches
exactly as it did live — no API matching, no stream-position coupling.  Because
the machine is deterministic and the clock is reproduced, its instruction count
at each fetch matches the recording, so every arrival lands at the right spot.

Determinism requires replaying under the SAME island/hook configuration the
demo was recorded with (instruction counts are config-specific).

Format: JSON lines.  Header, then one record per line:
    {"kind": "win16-demo", "version": 4, "exe": "SIMANTW.EXE",
     "snapshot": "snap_114308" | null, "instruction": 17050442}
    {"t": "i", "i": 17060123, "v": [hwnd, msg, wparam, lparam, tick, pt]}
    {"t": "c", "i": 17070000, "ms": 28875}
    {"t": "d", "i": 17080000, "dlg": "myd_high_scores", "v": ["command", 1, 0]}
    {"t": "quit", "i": 17090000}
"""
from __future__ import annotations

import bisect
import json
from pathlib import Path

VERSION = 4

WM_PAINT = 0x000F
WM_TIMER = 0x0113
WM_QUIT = 0x0012

# Instruction floor rate used only to keep GetTickCount progressing PAST the
# last recorded clock sample (end-of-demo tail); the recorded samples drive
# everything before that.
_INSTR_PER_MS = 1000


class DemoDivergence(RuntimeError):
    """Replay and machine disagreed about what happens next."""


class DemoEnded(RuntimeError):
    """The demo ran out of records while the machine wanted more input."""


class DemoRecorder:
    """Records the v4 input timeline.  Every tap carries the instruction count
    at which it happened live (`instr`), so replay can reproduce the exact
    interleaving of input arrivals against the game's own execution."""

    def __init__(self, path: str | Path, exe_name: str, *,
                 snapshot: str | None = None, instruction: int = 0) -> None:
        self.path = Path(path)
        self.snapshot = snapshot            # the anchor's name (or None)
        self._fh = open(self.path, "w", encoding="ascii")
        self._fh.write(json.dumps(
            {"kind": "win16-demo", "version": VERSION, "exe": exe_name,
             "snapshot": snapshot, "instruction": instruction}) + "\n")
        self.records = 0
        self._last_sample_instr = instruction

    def arrival(self, msg, instr: int) -> None:
        """Tap for every input ARRIVAL (host key / mouse event drained into the
        queue).  `instr` is the instruction count at arrival."""
        self._fh.write(json.dumps({"t": "i", "i": instr, "v": list(msg)}) + "\n")
        self._fh.flush()
        self.records += 1
        self._last_sample_instr = instr

    def clock_sample(self, instr: int, ms: int, *, min_gap: int = 20000) -> None:
        """Periodic (instr -> tick) sample so GetTickCount is reproducible
        during input-free stretches (busy-waits, splash timeouts).  Rate-limited
        to one per `min_gap` instructions to keep demos small."""
        if instr - self._last_sample_instr < min_gap:
            return
        self._fh.write(json.dumps({"t": "c", "i": instr, "ms": ms}) + "\n")
        self._fh.flush()
        self.records += 1
        self._last_sample_instr = instr

    def dialog_event(self, dlg_name: str, event, instr: int) -> None:
        self._fh.write(json.dumps(
            {"t": "d", "i": instr, "dlg": dlg_name, "v": list(event)}) + "\n")
        self._fh.flush()
        self.records += 1

    def messagebox_result(self, caption: str, result: int, instr: int) -> None:
        """Which BUTTON the user pressed on a modal message box.

        A message box is a user decision that steers control flow — "save the
        current file?" answered No goes on to Open, answered Yes goes to SaveAs
        first — so it belongs in the demo for the same reason a dialog event
        does.  Without it a replay silently takes the DEFAULT button's branch
        and diverges somewhere later, at a point that looks unrelated."""
        self._fh.write(json.dumps(
            {"t": "m", "i": instr, "cap": caption, "r": int(result)}) + "\n")
        self._fh.flush()
        self.records += 1

    def quit(self, instr: int) -> None:
        self._fh.write(json.dumps({"t": "quit", "i": instr}) + "\n")
        self._fh.flush()
        self.records += 1

    def close(self) -> None:
        self._fh.close()


class DemoDriver:
    """Replays a v4 demo by injecting input at instruction counts and
    reproducing GetTickCount.  Install with `install(sysobj)`; the system's
    message pump and `tick_count` consult it via `sysobj.demo_driver`."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        lines = self.path.read_text(encoding="ascii").splitlines()
        header = json.loads(lines[0])
        if header.get("kind") != "win16-demo":
            raise ValueError(f"{path}: not a win16 demo")
        version = header.get("version", 1)
        if version < 4:
            raise ValueError(
                f"{path}: demo version {version} is pre-v4 and no longer "
                f"replayable — re-record it (the format was redesigned to key "
                f"input to instruction counts; see win16/demo.py)")
        self.exe = header.get("exe")
        self.snapshot = header.get("snapshot")          # anchor, or None
        self.instruction = header.get("instruction", 0)
        self.records = [json.loads(line) for line in lines[1:] if line.strip()]

        # Input-arrival + quit timeline (ordered by instruction), and the
        # dialog-event queue (consumed in order by the dialog engine).
        self._events = [r for r in self.records if r["t"] in ("i", "quit")]
        self._ei = 0                                    # next event index
        self._dialogs = [r for r in self.records if r["t"] == "d"]
        self._di = 0                                    # next dialog index
        self._boxes = [r for r in self.records if r["t"] == "m"]
        self._mi = 0                                    # next message-box index

        # Clock samples: every (instr, tick) we can reconstruct from — the "c"
        # samples plus each arrival's own tick (v[4]).  Sorted, deduped by instr.
        pts = {self.instruction: 0} if not self.records else {}
        for r in self.records:
            if r["t"] == "c":
                pts[r["i"]] = r["ms"]
            elif r["t"] == "i":
                pts[r["i"]] = r["v"][4]
        self._cs = sorted(pts.items())
        self._cs_instr = [p[0] for p in self._cs]

        self.sys = None
        self._prev_yield = None
        self.ended = False

    # -- installation ------------------------------------------------------
    def install(self, sysobj) -> None:
        self.sys = sysobj
        sysobj.interactive = False
        sysobj.demo_driver = self
        # Inject during long callbacks (a sim-tick / modal loop that never
        # returns to GetMessage) so busy-polled input still arrives on schedule.
        self._prev_yield = getattr(sysobj, "yield_check", None)
        sysobj.yield_check = self._on_yield

    def _instr(self) -> int:
        return self.sys.machine.cpu.instruction_count

    # -- GetTickCount reproduction ----------------------------------------
    def tick_at(self, instr: int) -> int:
        cs = self._cs
        if not cs:
            return (instr - self.instruction) // _INSTR_PER_MS
        if instr <= cs[0][0]:
            return cs[0][1]
        if instr >= cs[-1][0]:
            li, lm = cs[-1]
            return lm + (instr - li) // _INSTR_PER_MS   # tail: floor past last sample
        j = bisect.bisect_right(self._cs_instr, instr)
        (i0, m0), (i1, m1) = cs[j - 1], cs[j]
        if i1 == i0:
            return m1
        return m0 + (m1 - m0) * (instr - i0) // (i1 - i0)

    # -- input injection ---------------------------------------------------
    def _apply_event(self, rec) -> None:
        if rec["t"] == "quit":
            self.sys.quit_posted = True
        else:                                           # "i"
            m = tuple(rec["v"])
            self.sys.msg_queue.append(m)
            self.sys._note_input(m)                     # polled state (keys/mouse)

    def inject_due(self) -> None:
        """Inject every arrival whose recorded instruction count has been
        reached.  Called at pump touchpoints and inside long callbacks."""
        cur = self._instr()
        while self._ei < len(self._events) and self._events[self._ei]["i"] <= cur:
            self._apply_event(self._events[self._ei])
            self._ei += 1

    def _on_yield(self) -> None:
        if self._prev_yield is not None:
            self._prev_yield()
        if self.sys is not None:
            self.inject_due()

    def _force_next(self):
        """A blocking GetMessage with nothing queued: deliver the next scheduled
        arrival now (live, the CPU thread was parked with its instruction count
        frozen until this input arrived, so its recorded instr == this fetch)."""
        if self._ei >= len(self._events):
            return None                                 # end of timeline
        rec = self._events[self._ei]
        self._ei += 1
        if rec["t"] == "quit":
            self.sys.quit_posted = True
            return None
        m = tuple(rec["v"])
        self.sys._note_input(m)
        return m

    # -- pump entry points (called by Win16System) -------------------------
    def pump_get(self):
        """GetMessage under replay: inject due arrivals, run the normal pump
        order (posted > paint > timer), and if still idle deliver the next
        scheduled arrival (the recorded blocking wait)."""
        self.inject_due()
        sys = self.sys
        try:
            return sys.next_message()
        except RuntimeError:
            pass                                        # idle: fall through
        m = self._force_next()
        if m is None and self.sys.quit_posted is None and self._ei >= len(self._events):
            self.ended = True
            raise DemoEnded(
                f"demo exhausted after {self._ei} events — machine wanted input")
        return m

    def pump_peek(self):
        """PeekMessage under replay only needs due arrivals injected; the
        system then scans the real queue itself.  A busy-poll simply misses
        until its awaited arrival's instruction is reached."""
        self.inject_due()
        if self._ei >= len(self._events) and not self.sys.msg_queue:
            self.ended = True
            raise DemoEnded(
                f"demo exhausted after {self._ei} events — machine peeked "
                f"for more input")

    def next_dialog_event(self, dlg_name: str):
        if self._di >= len(self._dialogs):
            raise DemoEnded(
                f"demo exhausted — dialog {dlg_name!r} wanted an event")
        rec = self._dialogs[self._di]
        if rec["dlg"] != dlg_name:
            raise DemoDivergence(
                f"dialog {dlg_name!r} wanted an event but the demo has "
                f"{rec['dlg']!r} next")
        self._di += 1
        return tuple(rec["v"])

    def next_messagebox_result(self, caption: str):
        """The recorded button for this message box, or ``None`` if the demo
        carries no answer for it.

        ``None`` means "decide it the way you would with no demo" — the
        default-button result.  That is what every demo recorded before message
        boxes were captured relies on, so those keep replaying bit-identically
        instead of failing on a record that was never written."""
        if self._mi >= len(self._boxes):
            return None
        rec = self._boxes[self._mi]
        if rec["cap"] != caption:
            raise DemoDivergence(
                f"message box {caption!r} wanted a result but the demo has "
                f"{rec['cap']!r} next")
        self._mi += 1
        return int(rec["r"])
