"""Game-tick demos for win16 — the endgame's mode-independent equivalence proof.

A thin win16 seam over :mod:`dos_re.tick_demo` (which owns the doctrine and the
native-core record / replay / verify loops — a win16 machine's ``.cpu`` IS the
dos_re CPU8086, so ``record_ticks(machine, ...)`` drives it directly), plus the
win16-shaped RECORD/REPLAY pair for the message-pump world:

Why a tick demo (vs the v4 instruction-keyed input demo in :mod:`win16.demo`):
the v4 anchor is the INSTRUCTION COUNT, which is hook-config-dependent — a lifted
island runs far fewer emulated instructions than the ASM it replaces, so one v4
recording only replays faithfully under the exact hook set it was recorded with
(cross-config replay silently desyncs).  A tick demo keys everything to the GAME
TICK instead — for a message-pump game, the sim WM_TIMER consumption:

* **input** is bucketed per tick and injected ARRIVAL-style into the queue at
  the tick boundary; the game's own pump then fetches freely.  (The v1-v3
  lesson: coupling delivery to the fetch API deadlocks; the v4 lesson: polled
  state is noted at arrival.  Both kept — only the ANCHOR changes.)
* **the boundary WM_TIMER** is delivered when the game ASKS for it (its pacing
  spin's first iteration), so no wall clock and no instruction count exists
  anywhere in the replay path — the two mode-dependent clocks that made earlier
  formats config-specific.
* **GetTickCount** serves the current tick's recorded base value plus a
  deterministic call-count stall escape (API-call counts are game-logic-driven,
  not instruction-driven), clamped monotonic.
* **digests** (one per tick, over :func:`default_digest`'s masked memory image)
  are filled by a CANONICALIZATION pass — a tick replay with ``mode="record"`` —
  so they reflect the tick-replay clock model, never the recording drive's.
  A ``mode="check"`` replay then proves two configs compute identical gameplay
  tick-by-tick, or names the first divergent tick.

Everything game-specific — the sim-tick seam for the native path, the digest
ownership mask, known scratch don't-cares — is adapter-supplied.  This module
never learns a game.
"""
from __future__ import annotations

import json
from pathlib import Path

from dos_re.tick_demo import (TickDemo, masked_digest, record_ticks, replay_to,
                              verify_ticks)

from .demo import DemoDivergence, DemoEnded

__all__ = ["TickDemo", "masked_digest", "record_ticks", "replay_to",
           "verify_ticks", "input_demo_drive", "default_digest",
           "TickDemoRecorder", "TickDemoDriver", "WM_TIMER", "is_input_message"]

VERSION = 1
WM_TIMER = 0x0113

#: Within-tick clock RAMP: GetTickCount is read many times per tick (SimAnt
#: reads it 100-250x/tick, ALL through one wrapper GR!_TickCount), returning a
#: smooth value ramp from this tick's fire-time toward the next tick's.  We
#: reproduce that ramp: reach the next boundary's recorded ms over this many
#: clock reads, then hold — so a render loop that waits for the clock to cross
#: the tick's span (a blink/animation timer) elapses, instead of spinning.  Read
#: counts are game-logic-driven (same state -> same reads), so hook-invariant.
RAMP_CALLS = 8

#: Tail clock rate past the last recorded boundary (end-of-demo), 1 ms per N
#: reads — only matters after every tick is consumed.
STALL_CALLS_PER_MS = 32


def is_input_message(msg_type: int) -> bool:
    """External-input message classes — the only messages a tick demo
    records/injects; everything else is machine-deterministic and regenerates
    on replay.  Host-posted per the win16 layer's own senders: keyboard and
    mouse events, WM_SIZE (host window resize), WM_COMMAND (host menubar picks),
    WM_HSCROLL/WM_VSCROLL (host scrollbar drags).  WM_CHAR/WM_SYSCHAR/
    WM_*DEADCHAR are EXCLUDED although they sit in the keyboard range: our
    TranslateMessage posts them into the queue derived from a consumed
    WM_KEYDOWN, so a replay regenerates them — recording them too would
    double-deliver."""
    if msg_type in (0x0102, 0x0103, 0x0106, 0x0107):    # machine-derived chars
        return False
    return (0x0100 <= msg_type <= 0x0109 or 0x0200 <= msg_type <= 0x020D
            or msg_type in (0x0005, 0x0111, 0x0114, 0x0115))


def default_digest(machine, *, zero=()) -> str:
    """The win16-generic gameplay digest: the full VM memory image with the
    transient stack window (below SS:SP — dead scratch at a pump boundary)
    zeroed, plus adapter-supplied `zero` linear offsets for known don't-care
    scratch bytes (game knowledge, e.g. a decoder's mid-op resume residue)."""
    s = machine.cpu.s
    stack_lin = machine.mem._xlat(s.ss & 0xFFFF, 0)
    sp = s.sp & 0xFFFF

    def zero_stack(buf: bytearray) -> None:
        buf[stack_lin:stack_lin + sp] = b"\x00" * sp

    return masked_digest(machine.mem.data, zero=zero, post=zero_stack)


def input_demo_drive(machine, *, chunk: int = 4096):
    """An ``advance_one_frame`` callable for :func:`record_ticks` that drives a
    win16 ``machine`` by replaying an already-installed v4 input demo.

    This is the win16 analogue of DOS's input-demo drive: the tick boundaries are
    found by ``record_ticks``'s seam-watching ``cpu.step`` wrapper, so a "frame"
    here is just a bounded run of ``chunk`` instructions.  Each call runs one
    chunk and returns True; when the input demo is exhausted the pump raises
    :class:`~win16.demo.DemoEnded`, which we translate to False (drive done).

    The caller installs the ``DemoDriver`` on the system object first (so the
    machine's own message pump replays the recorded input) — see
    ``win16.demo.DemoDriver.install``.
    """
    cpu = machine.cpu

    def advance() -> bool:
        try:
            cpu.run(chunk)
            return True
        except DemoEnded:
            return False

    return advance


class TickDemoRecorder:
    """Records a tick demo from a running machine (live play or a v4-demo
    replay — the conversion path).  The system taps feed it:

    * ``input(msg)`` — an external-input message was CONSUMED by the game's
      pump; bucketed under the current tick.
    * ``boundary(msg)`` — a WM_TIMER was consumed: the current tick is complete.
      Records the timer identity + its clock; the digest column is left for the
      canonicalization pass (a tick-driver replay with ``mode="record"``), so
      digests always reflect the tick-replay clock model, never the recording
      drive's.
    * ``quit()`` — WM_QUIT reached the pump.

    Format (JSON lines):
        {"kind": "win16-tickdemo", "version": 1, "exe": ..., "ms0": ...}
        {"t": "i", "k": 0, "v": [hwnd, msg, wp, lp, time, pt]}
        {"t": "b", "k": 0, "key": [hwnd, id], "ms": 12345, "d": null | "sha1"}
        {"t": "quit", "k": 7}
    """

    def __init__(self, path: str | Path, exe_name: str, *, ms0: int = 0) -> None:
        self.path = Path(path)
        self._fh = open(self.path, "w", encoding="ascii")
        self._fh.write(json.dumps({"kind": "win16-tickdemo", "version": VERSION,
                                   "exe": exe_name, "ms0": ms0}) + "\n")
        self.bucket = 0
        self.records = 0
        self._quit_done = False
        self._clk_base = ms0            # deltas encoded off the last boundary ms
        self._clk: list[int] = []       # this bucket's GetTickCount reads

    def clock(self, value: int) -> None:
        """One GetTickCount read the game consumed this tick — the sideband the
        replay reproduces so clock-derived state (sim timing, tick-seeded RNG,
        animation) matches byte-for-byte (module docstring / dos_re.tick_demo)."""
        self._clk.append(value)

    def input(self, msg) -> None:
        self._fh.write(json.dumps(
            {"t": "i", "k": self.bucket, "v": list(msg)}) + "\n")
        self.records += 1

    def boundary(self, msg) -> None:
        # Store the tick's clock reads as deltas off the bucket base (small,
        # monotone) — the pacing-spin tail is redundant on replay (its reads are
        # far fewer) but harmless; the GAMEPLAY reads come first and are exact.
        deltas = [v - self._clk_base for v in self._clk]
        self._fh.write(json.dumps(
            {"t": "b", "k": self.bucket, "key": [msg[0], msg[2]],
             "ms": msg[4], "d": None, "cb": self._clk_base, "clk": deltas}) + "\n")
        self.bucket += 1
        self.records += 1
        self._clk_base = msg[4]
        self._clk = []

    def quit(self) -> None:
        if not self._quit_done:
            self._fh.write(json.dumps({"t": "quit", "k": self.bucket}) + "\n")
            self._quit_done = True
            self.records += 1

    def close(self) -> None:
        self._fh.close()


class TickDemoDriver:
    """Replays a tick demo — hook-config-invariantly (see the module docstring
    for the model).

    ``mode``: "check" verifies the recorded per-tick digest and raises
    :class:`DemoDivergence` at the first mismatching tick; "record" fills the
    digest column (the canonicalization pass — save with :meth:`save`);
    "off" skips digests.
    """

    def __init__(self, path: str | Path, *, digest_fn=None,
                 mode: str = "check") -> None:
        self.path = Path(path)
        lines = self.path.read_text(encoding="ascii").splitlines()
        header = json.loads(lines[0])
        if header.get("kind") != "win16-tickdemo":
            raise ValueError(f"{path}: not a win16 tick demo")
        self.exe = header.get("exe")
        self.ms0 = header.get("ms0", 0)
        self.buckets: list[list[tuple]] = [[]]
        self.boundaries: list[dict] = []
        self.quit_k: int | None = None
        for line in lines[1:]:
            if not line.strip():
                continue
            r = json.loads(line)
            if r["t"] == "i":
                while len(self.buckets) <= r["k"]:
                    self.buckets.append([])
                self.buckets[r["k"]].append(tuple(r["v"]))
            elif r["t"] == "b":
                self.boundaries.append(r)
            elif r["t"] == "quit":
                self.quit_k = r["k"]
        while len(self.buckets) <= len(self.boundaries):
            self.buckets.append([])
        self.digest_fn = digest_fn
        self.mode = mode
        if mode == "check" and digest_fn is not None and \
                all(b["d"] is None for b in self.boundaries):
            raise ValueError(
                f"{path}: no digests recorded — run the canonicalization pass "
                f"first (a tick replay with mode='record', then save())")
        # Per-bucket recorded GetTickCount reads (absolute), if the demo carries
        # them (the clock sideband); else None -> synthetic ramp fallback.
        self.clocks: list[list[int] | None] = [
            ([b["cb"] + d for d in b["clk"]] if "clk" in b else None)
            for b in self.boundaries]
        self.bucket = 0                     # current bucket (0 = pre-tick)
        self._cursor = 0                    # next undelivered msg in the bucket
        self._clk_i = 0                     # next clock-read index in the bucket
        self.ticks_checked = 0
        self._calls = 0                     # GetTickCount stall-escape counter
        self._ms_floor = self.ms0
        self.sys = None

    @property
    def n_ticks(self) -> int:
        return len(self.boundaries)

    # -- installation -------------------------------------------------------
    def install(self, sysobj) -> None:
        self.sys = sysobj
        sysobj.interactive = False
        sysobj.tick_driver = self
        self._cursor = 0                    # next undelivered msg in the bucket

    # -- consumption-ordered input delivery -----------------------------------
    # The recorded stream is held back and dealt ONE message per pump ask, in
    # consumption order — NOT pre-injected into the queue.  Pre-injecting a
    # whole bucket lets an input-discarding phase (a title screen's pump) eat
    # messages the recording consumed later, and snaps the polled cursor to its
    # final position instantly; per-ask delivery keeps both faithful, and the
    # pump-ask sequence is game-state-driven, so it is hook-config-invariant.
    def next_input(self, hwnd_filter: int, lo: int, hi: int, remove: bool):
        """The pump scanned an empty queue: deliver the current bucket's next
        recorded message if it matches the ask's filters."""
        bucket = self.buckets[self.bucket]
        if self._cursor >= len(bucket):
            if self.quit_k == self.bucket:
                self.sys.quit_posted = True
            return None
        m = bucket[self._cursor]
        if hwnd_filter and m[0] != hwnd_filter:
            return None
        if (lo or hi) and not (lo <= m[1] <= hi):
            return None
        if remove:
            self._cursor += 1
            self.sys._note_input(m)         # polled state at delivery
        return m

    # -- the boundary: the game asks for its WM_TIMER ------------------------
    def timer_ask(self, hwnd_filter: int, remove: bool):
        """The game's pump wants a WM_TIMER (a PeekMessage timer filter, or an
        idle GetMessage with an armed timer).  Deliver the next recorded
        boundary — but only once the current bucket is drained: the recording
        proves the game consumed those messages BEFORE this boundary, so it
        will come back for them (no deadlock).  PM_NOREMOVE shows the boundary
        without advancing."""
        j = self.bucket
        if j >= len(self.boundaries):
            raise DemoEnded(
                f"tick demo exhausted after {j} ticks — machine asked for more")
        if self._cursor < len(self.buckets[j]):
            return None                     # bucket not drained yet
        b = self.boundaries[j]
        hwnd, timer_id = b["key"]
        if hwnd_filter and hwnd != hwnd_filter:
            return None
        if remove:
            if self.digest_fn is not None and self.mode != "off":
                got = self.digest_fn(self.sys.machine)
                if self.mode == "record":
                    b["d"] = got
                elif b["d"] is not None and got != b["d"]:
                    raise DemoDivergence(
                        f"tick {j}: gameplay digest mismatch "
                        f"(got {got[:12]} != recorded {b['d'][:12]})")
                self.ticks_checked += 1
            self.bucket += 1
            self._cursor = 0
            self._clk_i = 0
            self._calls = 0
            if self.quit_k == self.bucket and not self.buckets[self.bucket]:
                self.sys.quit_posted = True
        proc = self.sys.timer_procs.get((hwnd, timer_id), 0)
        return (hwnd, WM_TIMER, timer_id, proc, self._base_ms(), 0)

    # -- the clock -----------------------------------------------------------
    def _base_ms(self) -> int:
        if self.bucket == 0:
            return self.ms0
        return self.boundaries[self.bucket - 1]["ms"]

    def tick_count(self) -> int:
        """GetTickCount under tick replay.  If the demo carries the clock
        sideband, return the exact recorded read for this tick (by index; the
        gameplay reads come first and align regardless of the shorter replay
        pacing-spin) — this is what makes clock-derived state byte-faithful.
        Otherwise (or once the recorded reads run out), fall back to the
        synthetic ramp+escape.  Clamped monotonic either way."""
        clk = self.clocks[self.bucket] if self.bucket < len(self.clocks) else None
        if clk is not None and self._clk_i < len(clk):
            ms = clk[self._clk_i]
            self._clk_i += 1
            self._calls += 1
            if ms < self._ms_floor:
                ms = self._ms_floor
            self._ms_floor = ms
            return ms
        self._calls += 1
        lo = self._base_ms()
        span = (self.boundaries[self.bucket]["ms"] - lo
                if self.bucket < len(self.boundaries) else 0)
        if span < 0:
            span = 0
        # RAMP fast to the next boundary (so a render loop waiting on the tick's
        # clock ramp elapses), but also keep ESCAPING past it (so the long
        # pre-tick init / any multi-tick wait elapses instead of pinning at the
        # boundary).  The max of the two advances gives both.
        ramp = span if self._calls >= RAMP_CALLS else span * self._calls // RAMP_CALLS
        ms = lo + max(ramp, self._calls // STALL_CALLS_PER_MS)
        if ms < self._ms_floor:
            ms = self._ms_floor
        self._ms_floor = ms
        return ms

    # -- canonicalization output ---------------------------------------------
    def save(self, path: str | Path) -> None:
        """Re-emit the demo with the digests this run recorded (mode="record")."""
        with open(path, "w", encoding="ascii") as out:
            out.write(json.dumps({"kind": "win16-tickdemo", "version": VERSION,
                                  "exe": self.exe, "ms0": self.ms0}) + "\n")
            for k in range(len(self.buckets)):
                for m in self.buckets[k]:
                    out.write(json.dumps({"t": "i", "k": k, "v": list(m)}) + "\n")
                if k < len(self.boundaries):
                    b = self.boundaries[k]
                    rec = {"t": "b", "k": k, "key": b["key"], "ms": b["ms"],
                           "d": b["d"]}
                    if "clk" in b:                       # preserve the sideband
                        rec["cb"] = b["cb"]
                        rec["clk"] = b["clk"]
                    out.write(json.dumps(rec) + "\n")
            if self.quit_k is not None:
                out.write(json.dumps({"t": "quit", "k": self.quit_k}) + "\n")
