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
"""
from __future__ import annotations

CALLBACK_RET_IP = 0xFFFE        # sentinel offset inside the thunk segment
_CHUNK = 65536                  # steps between yield_check / max-step checks


class CallbackOverrun(RuntimeError):
    pass


class _CallbackReturn(Exception):
    """Raised by the sentinel hook when a callback far-returns; caught by the
    innermost active call_far (correct even when callbacks nest)."""


def _return_hook(cpu):
    raise _CallbackReturn()


def _install_return_hook(cpu, thunk_seg: int) -> None:
    key = (thunk_seg & 0xFFFF, CALLBACK_RET_IP)
    if cpu.replacement_hooks.get(key) is not _return_hook:
        cpu.replacement_hooks[key] = _return_hook
        cpu.hook_names[key] = "callback-return"


def call_far(cpu, thunk_seg: int, seg: int, off: int, args: list[int],
             *, max_steps: int = 20_000_000, yield_check=None) -> tuple[int, int]:
    """Far-call seg:off with 16-bit pascal args; returns (AX, DX) at return.

    `args` entries are 16-bit words, pushed in list order (pascal declaration
    order).  32-bit values must be pre-split into (hi, lo) word pairs.
    `yield_check`, if given, is called between chunks (host pause / input).
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

    steps = 0
    try:
        while steps < max_steps:
            steps += cpu.run(min(_CHUNK, max_steps - steps))
            if cpu.halted:
                raise HaltExecution()
            if yield_check is not None:
                yield_check()
        raise CallbackOverrun(
            f"callback {seg:04X}:{off:04X} did not return within {max_steps} "
            f"steps (at {s.cs:04X}:{s.ip:04X})")
    except _CallbackReturn:
        pass                    # far-returned to the sentinel — done

    if (s.sp & 0xFFFF) != saved_sp:
        raise CallbackOverrun(
            f"callback {seg:04X}:{off:04X} returned with SP {s.sp:04X} != "
            f"{saved_sp:04X} (wrong argument pop?)")
    s.cs, s.ip = saved_cs, saved_ip
    return s.ax & 0xFFFF, s.dx & 0xFFFF
