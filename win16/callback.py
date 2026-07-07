"""Calling from Python INTO VM code (WndProc, dialog procs, timers).

Mechanism: push pascal args left-to-right, push a sentinel far return
address, set CS:IP to the callback, and run a nested interpreter loop until
execution far-returns to the sentinel.  API hooks encountered inside the
callback dispatch normally (cpu.step() handles them), so callbacks nest.
"""
from __future__ import annotations

CALLBACK_RET_IP = 0xFFFE        # sentinel offset inside the thunk segment


class CallbackOverrun(RuntimeError):
    pass


def call_far(cpu, thunk_seg: int, seg: int, off: int, args: list[int],
             *, max_steps: int = 20_000_000) -> tuple[int, int]:
    """Far-call seg:off with 16-bit pascal args; returns (AX, DX) at return.

    `args` entries are 16-bit words, pushed in list order (declaration order
    for pascal).  32-bit values must be pre-split into (hi, lo) word pairs.
    """
    s = cpu.s
    saved_cs, saved_ip, saved_sp = s.cs, s.ip, s.sp

    def push(word: int) -> None:
        s.sp = (s.sp - 2) & 0xFFFF
        cpu.mem.ww(s.ss, s.sp, word & 0xFFFF)

    for w in args:
        push(w)
    push(thunk_seg)             # sentinel far return address
    push(CALLBACK_RET_IP)
    s.cs, s.ip = seg & 0xFFFF, off & 0xFFFF

    steps = 0
    while (s.cs & 0xFFFF, s.ip & 0xFFFF) != (thunk_seg, CALLBACK_RET_IP):
        cpu.step()
        steps += 1
        if steps >= max_steps:
            raise CallbackOverrun(
                f"callback {seg:04X}:{off:04X} did not return within {max_steps} steps "
                f"(at {s.cs:04X}:{s.ip:04X})")
    if (s.sp & 0xFFFF) != saved_sp:
        raise CallbackOverrun(
            f"callback {seg:04X}:{off:04X} returned with SP {s.sp:04X} != {saved_sp:04X} "
            f"(wrong argument pop?)")
    s.cs, s.ip = saved_cs, saved_ip
    return s.ax & 0xFFFF, s.dx & 0xFFFF
