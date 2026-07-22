"""Win16 ReplayArtifact support: channels, coordinates, recorder, input driver.

The Win16 adapter over :mod:`dos_re.replay` (dos_re 3.0).  This module is
deliberately NOT a replay format — the format, timeline, profile bases,
boundary caches and validations are owned by ``dos_re.replay.ReplayArtifact``.
What is Win16 here is exactly what was Win16 about the retired v4 demo:

* the EVENT CHANNELS — message arrivals, clock samples, modal-dialog events,
  MessageBox button results, WM_QUIT;
* the STOP COORDINATE — the guest instruction count (a guest coordinate, never
  a host dispatch count);
* the APPLICATION MECHANICS — inject each arrival into the system's message
  queue exactly when the machine reaches its recorded instruction count, at
  the same pump touchpoints and callback yields the live run used, and
  reproduce GetTickCount by interpolating the recorded (instr, tick) samples.

Timeline shape: ordinal ``n`` is the position AFTER applying the first ``n``
timeline events; every ordinal carries a ``ReplayPointCoordinate`` holding the
instruction count at which that event happened live.  ``n`` is the exactly
reproducible stop (the driver counts applied events); the coordinate is the
determinism CHECK — a composition that reaches an event at a different
instruction count has diverged and fails loudly, it does not resync.

The injection model (v4's instruction-keyed arrival timeline) is load-bearing:
earlier consumption-keyed formats deadlocked any loop whose fetch path drifted
from the recording (a message recorded under GetMessage was invisible to a
PeekMessage that wanted it).  See docs/history in the consuming game project.

Determinism requires replaying under an execution profile with the same
``ReplayExecutionIdentity`` the artifact's events were captured under
(instruction counts are composition-specific); the event stream itself is
portable, continuation caches are profile-local — dos_re 3.0's formulation of
the old "demos are hook-config-specific" rule.
"""
from __future__ import annotations

import bisect
from pathlib import Path
from typing import Sequence

from dos_re.replay import (ReplayArtifact, ReplayError, ReplayEvent,
                           ReplayPoint, ReplayRecording)

#: Event channels (namespaced, Win16-owned).  Game projects add their own
#: channels for behavioral features; unknown channels fail loud on apply.
INPUT_CHANNEL = "win16.input"
CLOCK_CHANNEL = "win16.clock"
DIALOG_CHANNEL = "win16.dialog"
MESSAGEBOX_CHANNEL = "win16.messagebox"
QUIT_CHANNEL = "win16.quit"

#: The Win16 stop-coordinate schema: the guest instruction count at which a
#: timeline event happened live.  A guest coordinate — host backend-dispatch
#: counts are forbidden by the dos_re 3.0 replay contract.
GUEST_INSTRUCTION_COORDINATE = "win16-re:guest-instruction-count:v1"

# Instruction floor rate used only to keep GetTickCount progressing PAST the
# last recorded clock sample (end-of-timeline tail); the recorded samples
# drive everything before that.  Matches Win16System's headless clock floor.
_INSTR_PER_MS = 1000


def input_payload(msg) -> dict:
    """One posted-message arrival: ``[hwnd, msg, wparam, lparam, tick, pt]``."""
    return {"message": [int(v) for v in msg]}


def clock_payload(ms: int) -> dict:
    return {"ms": int(ms)}


def dialog_payload(dlg_name: str, event) -> dict:
    return {"dialog": str(dlg_name), "event": list(event)}


def messagebox_payload(caption: str, result: int) -> dict:
    return {"caption": str(caption), "result": int(result)}


class ReplayDivergence(ReplayError):
    """Replay and machine disagreed about what happens next."""


class ReplayExhausted(ReplayError):
    """The timeline ran out of events while the machine wanted more input."""


class Win16ReplayRecorder:
    """Record the Win16 input timeline into a ``ReplayRecording``.

    Every tap carries the live instruction count; each becomes one timeline
    event at the next ordinal with its instruction-count coordinate.  The
    same tap surface the v4 recorder exposed, so the recording call sites
    (input drain, clock check, dialog engine, MessageBox) stay unchanged.
    """

    def __init__(self, recording: ReplayRecording, *, start_instruction: int = 0):
        self.recording = recording
        self._ordinal = 0
        self._last_sample_instr = int(start_instruction)
        recording.mark(0, schema_id=GUEST_INSTRUCTION_COORDINATE,
                       value=int(start_instruction))
        self.records = 0

    def _add(self, channel: str, payload: dict, instr: int) -> None:
        self._ordinal += 1
        self.recording.add(self._ordinal, channel, payload)
        self.recording.mark(self._ordinal,
                            schema_id=GUEST_INSTRUCTION_COORDINATE,
                            value=int(instr))
        self.records += 1

    # -- taps (same surface as the retired v4 recorder) --------------------

    def arrival(self, msg, instr: int) -> None:
        self._add(INPUT_CHANNEL, input_payload(msg), instr)
        self._last_sample_instr = int(instr)

    def clock_sample(self, instr: int, ms: int, *, min_gap: int = 20000) -> None:
        """Periodic (instr -> tick) sample so GetTickCount is reproducible
        during input-free stretches.  Rate-limited to one per ``min_gap``
        instructions to keep timelines small."""
        if instr - self._last_sample_instr < min_gap:
            return
        self._add(CLOCK_CHANNEL, clock_payload(ms), instr)
        self._last_sample_instr = int(instr)

    def dialog_event(self, dlg_name: str, event, instr: int) -> None:
        self._add(DIALOG_CHANNEL, dialog_payload(dlg_name, event), instr)

    def messagebox_result(self, caption: str, result: int, instr: int) -> None:
        """Which BUTTON the user pressed on a modal message box — a user
        decision that steers control flow, recorded for the same reason a
        dialog event is (an unanswered box replays the default button and
        diverges somewhere later, at a point that looks unrelated)."""
        self._add(MESSAGEBOX_CHANNEL, messagebox_payload(caption, result), instr)

    def quit(self, instr: int) -> None:
        self._add(QUIT_CHANNEL, {}, instr)

    @property
    def end_ordinal(self) -> int:
        return self._ordinal

    def final_mark(self, instr: int) -> int:
        """Close the timeline: one final ordinal marking where the recording
        stopped.  Returns the end ordinal to pass to ``recording.finish``."""
        self._ordinal += 1
        self.recording.mark(self._ordinal,
                            schema_id=GUEST_INSTRUCTION_COORDINATE,
                            value=int(instr))
        return self._ordinal


class Win16ReplayInputDriver:
    """Apply a Win16 replay timeline to a running machine.

    Install with :meth:`install`; the system's message pump and ``tick_count``
    consult it via ``sysobj.demo_driver`` (the established pump touchpoints).
    ``current_ordinal`` counts applied timeline events — the driver half of the
    ``ReplayDriver.current_point`` contract; the surrounding Win16 replay
    driver owns profile/capture/restore/projection.
    """

    def __init__(self, events: Sequence[ReplayEvent], coordinates, *,
                 timeline_id: str, base_instruction: int = 0,
                 strict_coordinates: bool = True):
        self.timeline_id = timeline_id
        self.events = tuple(events)
        self.base_instruction = int(base_instruction)
        self.strict_coordinates = bool(strict_coordinates)
        #: ordinal -> declared instruction count (from ReplayPointCoordinates)
        self._instr_at: dict[int, int] = {}
        for coord in coordinates:
            if coord.schema_id != GUEST_INSTRUCTION_COORDINATE:
                raise ReplayError(
                    f"timeline coordinate schema {coord.schema_id!r} is not "
                    f"the Win16 guest-instruction schema")
            self._instr_at[coord.point.ordinal] = int(coord.value)
        missing = [e.point.ordinal for e in self.events
                   if e.point.ordinal not in self._instr_at]
        if missing:
            raise ReplayError(
                f"timeline events without instruction coordinates: "
                f"ordinals {missing[:5]}")

        # The arrival/quit timeline (ordered by ordinal == instruction order),
        # and the in-order dialog / message-box queues.
        self._events = [e for e in self.events
                        if e.channel in (INPUT_CHANNEL, QUIT_CHANNEL)]
        self._ei = 0
        self._dialogs = [e for e in self.events if e.channel == DIALOG_CHANNEL]
        self._di = 0
        self._boxes = [e for e in self.events
                       if e.channel == MESSAGEBOX_CHANNEL]
        self._mi = 0
        self.applied = 0            # timeline events applied (all channels)

        # Clock reconstruction points: the clock samples plus each arrival's
        # own tick.  Sorted, deduped by instruction count.
        pts: dict[int, int] = {}
        for e in self.events:
            instr = self._instr_at[e.point.ordinal]
            if e.channel == CLOCK_CHANNEL:
                pts[instr] = int(e.payload["ms"])
            elif e.channel == INPUT_CHANNEL:
                pts[instr] = int(e.payload["message"][4])
        if not pts:
            pts[self.base_instruction] = 0
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

    @property
    def current_ordinal(self) -> int:
        """Timeline position: events applied so far, in ordinal order."""
        return self.applied

    # -- GetTickCount reproduction ----------------------------------------

    def tick_at(self, instr: int) -> int:
        cs = self._cs
        if instr <= cs[0][0]:
            return cs[0][1]
        if instr >= cs[-1][0]:
            li, lm = cs[-1]
            return lm + (instr - li) // _INSTR_PER_MS   # tail: floor past last
        j = bisect.bisect_right(self._cs_instr, instr)
        (i0, m0), (i1, m1) = cs[j - 1], cs[j]
        if i1 == i0:
            return m1
        return m0 + (m1 - m0) * (instr - i0) // (i1 - i0)

    # -- input injection ---------------------------------------------------

    def _check_coordinate(self, event: ReplayEvent) -> None:
        """The determinism assertion: the machine must reach each event AT its
        recorded instruction count, never before (an injection point is only
        consulted once the count is due) and — under a faithful composition —
        the injection touchpoint at which it becomes due is the same one the
        live run recorded, so a large overshoot means the composition
        diverged.  Only the DUE side is checkable here; the exact-count check
        happens at replay stops (the surrounding driver compares its stop
        coordinate)."""
        declared = self._instr_at[event.point.ordinal]
        if self.strict_coordinates and self._instr() < declared:
            raise ReplayDivergence(
                f"timeline event at ordinal {event.point.ordinal} applied at "
                f"instruction {self._instr()} before its recorded coordinate "
                f"{declared}")

    def _apply_event(self, event: ReplayEvent) -> None:
        self._check_coordinate(event)
        if event.channel == QUIT_CHANNEL:
            self.sys.quit_posted = True
        else:                                           # INPUT_CHANNEL
            m = tuple(event.payload["message"])
            self.sys.msg_queue.append(m)
            self.sys._note_input(m)                     # polled keys/mouse
        self.applied += 1

    def inject_due(self) -> int:
        """Inject every arrival whose recorded instruction count has been
        reached.  Called at pump touchpoints and inside long callbacks.
        Returns how many arrivals were injected."""
        cur = self._instr()
        n = 0
        while (self._ei < len(self._events)
               and self._instr_at[self._events[self._ei].point.ordinal] <= cur):
            self._apply_event(self._events[self._ei])
            self._ei += 1
            n += 1
        return n

    def _on_yield(self) -> bool:
        """Yield hook for ``win16.callback.call_far``.  True when the RECORDED
        TIMELINE ADVANCED — the replay's evidence that a long-running callback
        is making progress, not hung (a modal loop with its own pump never
        far-returns while the player interacts with it; the recording proves
        it returned eventually)."""
        progress = False
        if self._prev_yield is not None:
            progress = bool(self._prev_yield())
        if self.sys is not None:
            progress = bool(self.inject_due()) or progress
        return progress

    def _force_next(self):
        """A blocking GetMessage with nothing queued: deliver the next
        scheduled arrival now (live, the CPU thread was parked with its
        instruction count frozen until this input arrived)."""
        if self._ei >= len(self._events):
            return None                                 # end of timeline
        event = self._events[self._ei]
        self._ei += 1
        if event.channel == QUIT_CHANNEL:
            self.sys.quit_posted = True
            self.applied += 1
            return None
        m = tuple(event.payload["message"])
        self.sys._note_input(m)
        self.applied += 1
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
        if (m is None and self.sys.quit_posted is None
                and self._ei >= len(self._events)):
            self.ended = True
            raise ReplayExhausted(
                f"timeline exhausted after {self._ei} events — machine "
                f"wanted input")
        return m

    def pump_peek(self):
        """PeekMessage under replay only needs due arrivals injected; the
        system then scans the real queue itself.  A busy-poll simply misses
        until its awaited arrival's instruction is reached."""
        self.inject_due()
        if self._ei >= len(self._events) and not self.sys.msg_queue:
            self.ended = True
            raise ReplayExhausted(
                f"timeline exhausted after {self._ei} events — machine "
                f"peeked for more input")

    def next_dialog_event(self, dlg_name: str):
        if self._di >= len(self._dialogs):
            raise ReplayExhausted(
                f"timeline exhausted — dialog {dlg_name!r} wanted an event")
        event = self._dialogs[self._di]
        if event.payload["dialog"] != dlg_name:
            raise ReplayDivergence(
                f"dialog {dlg_name!r} wanted an event but the timeline has "
                f"{event.payload['dialog']!r} next")
        self._di += 1
        self.applied += 1
        return tuple(event.payload["event"])

    def next_messagebox_result(self, caption: str):
        """The recorded button for this message box, or ``None`` if the
        timeline carries no answer for it (``None`` = the default-button
        result, which is what recordings made before boxes were captured rely
        on)."""
        if self._mi >= len(self._boxes):
            return None
        event = self._boxes[self._mi]
        if event.payload["caption"] != caption:
            raise ReplayDivergence(
                f"message box {caption!r} wanted a result but the timeline "
                f"has {event.payload['caption']!r} next")
        self._mi += 1
        self.applied += 1
        return int(event.payload["result"])


def input_driver_for(artifact: ReplayArtifact, *,
                     strict_coordinates: bool = True) -> Win16ReplayInputDriver:
    """Build the input driver for an artifact's full timeline."""
    coords = artifact.timeline_coordinates
    base_instr = 0
    for coord in coords:
        if coord.point.ordinal == 0:
            base_instr = int(coord.value)
            break
    return Win16ReplayInputDriver(
        artifact.events, coords, timeline_id=artifact.timeline_id,
        base_instruction=base_instr, strict_coordinates=strict_coordinates)
