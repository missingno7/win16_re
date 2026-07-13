"""Calling from Python INTO VM code (WndProc, dialog procs, timers).

Mechanism: push pascal args left-to-right, push a sentinel far return address,
set CS:IP to the callback, and run the interpreter until execution far-returns
to the sentinel.  API hooks encountered inside the callback dispatch normally,
so callbacks nest.

Return is detected by a permanent replacement-hook at the sentinel CS:IP that
raises to break out — this lets the body run via cpu.run()'s TIGHT loop instead
of a per-instruction Python loop (SimAnt's ~59fps sim tick runs entirely inside
a TimerProc callback, so the per-step loop made in-game unplayably slow AND
un-pausable).  Between chunks an optional `yield_check` runs, so a long callback
stays responsive (snapshot pause / host input) instead of freezing the UI.

## Resuming a snapshot taken INSIDE a callback

An interactive snapshot (F9) parks at an instruction-chunk boundary, which for
an in-game machine is usually INSIDE a long TimerProc callback — the Python
call_far frame that dispatched it is not part of the snapshot.  What remains
to be done when that callback eventually far-returns is, for the APIs a game
parks under (DispatchMessage's TimerProc/WndProc branches, SendMessage), purely
mechanical VM work: the callback's own RETF already restored SP to the API
call frame, and the API's result IS the callback's AX/DX verbatim — so all the
lost Python frame owed the VM is `retf <api argbytes>` with registers passed
through.  call_far therefore keeps a serializable record of every active frame
(`cpu.win16_callback_frames`); a snapshot saves them, and a resumed machine
replays each pending return at the sentinel (`cpu.win16_orphan_frames`) — see
_return_hook.  APIs with Python-side post-work after the callback are NOT
resumable this way and fail loudly by name.
"""
from __future__ import annotations

CALLBACK_RET_IP = 0xFFFE        # sentinel offset inside the thunk segment
_CHUNK = 8192                   # steps between yield_check / max-step checks
#   Small so the interactive driver's yield_check refreshes the wall clock often
#   enough to pace a frame-timed callback (SimAnt's sim tick) to real time.

# APIs whose handler returns the callback's result verbatim with no Python
# post-work — the only ones an orphaned (resumed-from-snapshot) callback can
# return through.  Grown on evidence, never speculatively.
_ORPHAN_RESUMABLE = {"DispatchMessage", "SendMessage"}


class CallbackOverrun(RuntimeError):
    pass


class OrphanReturnError(RuntimeError):
    """A resumed snapshot's parked callback returned through an API whose
    continuation cannot be reconstructed VM-side."""


class _CallbackReturn(Exception):
    """Raised by the sentinel hook when a callback far-returns; caught by the
    innermost active call_far (correct even when callbacks nest)."""


def _return_hook(cpu):
    if getattr(cpu, "win16_callback_frames", None):
        raise _CallbackReturn()          # a live call_far is waiting for this
    orphans = getattr(cpu, "win16_orphan_frames", None)
    if orphans:
        # A callback that was parked in a snapshot has just far-returned; the
        # Python frame that dispatched it is gone.  Its RETF already restored
        # SP to the API call frame — finish the API: retf <argbytes>, with the
        # callback's AX/DX passing through as the API result.
        fr = orphans.pop()
        if fr["api"] not in _ORPHAN_RESUMABLE:
            raise OrphanReturnError(
                f"resumed callback returned through {fr['api']!r}, which has "
                f"Python-side post-work — not resumable from a snapshot")
        if fr.get("sp") is not None and (cpu.s.sp & 0xFFFF) != fr["sp"]:
            raise OrphanReturnError(
                f"orphaned {fr['api']} return: SP {cpu.s.sp:04X} != recorded "
                f"{fr['sp']:04X} (wrong argument pop?)")
        from win16.api.core import ret_far
        ret_far(cpu, fr["argbytes"])     # AX/DX stay = the callback's result
        return
    raise OrphanReturnError(
        "callback far-returned to the sentinel with no pending frame — "
        "snapshot taken before callback-frame recording?  Re-take it.")


def _install_return_hook(cpu, thunk_seg: int) -> None:
    key = (thunk_seg & 0xFFFF, CALLBACK_RET_IP)
    if cpu.replacement_hooks.get(key) is not _return_hook:
        cpu.replacement_hooks[key] = _return_hook
        cpu.hook_names[key] = "callback-return"


def call_far(cpu, thunk_seg: int, seg: int, off: int, args: list[int],
             *, max_steps: int | None = 20_000_000, yield_check=None
             ) -> tuple[int, int]:
    """Far-call seg:off with 16-bit pascal args; returns (AX, DX) at return.

    `args` entries are 16-bit words, pushed in list order (pascal declaration
    order).  32-bit values must be pre-split into (hi, lo) word pairs.
    `yield_check`, if given, is called between chunks (host pause / input).
    `max_steps` caps a runaway callback; pass None for no cap — correct for an
    INTERACTIVE, user-pausable callback (SimAnt's sim-tick TimerProc legitimately
    busy-waits on the real clock and on input, so a fixed cap kills a live wait).
    """
    from dos_re.cpu import HaltExecution

    s = cpu.s
    saved_cs, saved_ip, saved_sp = s.cs, s.ip, s.sp
    _install_return_hook(cpu, thunk_seg)

    def push(word: int) -> None:
        s.sp = (s.sp - 2) & 0xFFFF
        cpu.mem.ww(s.ss, s.sp, word & 0xFFFF)

    for w in args:
        push(w)
    push(thunk_seg)             # sentinel far return address
    push(CALLBACK_RET_IP)
    s.cs, s.ip = seg & 0xFFFF, off & 0xFFFF

    # The serializable record of this frame: everything a resumed snapshot
    # needs to complete the API this callback was dispatched from (the api
    # name + argbytes come from the dispatching API entry, via core.dispatch).
    api_name, api_argbytes = getattr(cpu, "win16_current_api", ("?", 0))
    frames = getattr(cpu, "win16_callback_frames", None)
    if frames is None:
        frames = cpu.win16_callback_frames = []
    frames.append({"api": api_name, "argbytes": api_argbytes, "sp": saved_sp})

    steps = 0
    unbounded = max_steps is None
    try:
        while unbounded or steps < max_steps:
            steps += cpu.run(_CHUNK if unbounded else min(_CHUNK, max_steps - steps))
            if cpu.halted:
                raise HaltExecution()
            if yield_check is not None:
                yield_check()
        raise CallbackOverrun(
            f"callback {seg:04X}:{off:04X} did not return within {max_steps} "
            f"steps (at {s.cs:04X}:{s.ip:04X})")
    except _CallbackReturn:
        pass                    # far-returned to the sentinel — done
    finally:
        frames.pop()

    if (s.sp & 0xFFFF) != saved_sp:
        raise CallbackOverrun(
            f"callback {seg:04X}:{off:04X} returned with SP {s.sp:04X} != "
            f"{saved_sp:04X} (wrong argument pop?)")
    s.cs, s.ip = saved_cs, saved_ip
    return s.ax & 0xFFFF, s.dx & 0xFFFF
