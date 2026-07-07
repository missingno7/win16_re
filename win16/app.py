"""Generic Win16 application launcher — load any NE executable into the VM.

Game-agnostic: builds the full Win16 API surface (KERNEL/USER/GDI/SOUND/
MMSYSTEM/win87em + the dialog engine) over the dos_re CPU and returns a booted
`Win16Machine` with a `Win16System` attached.  A game adapter (e.g. ppython)
is only a thin wrapper choosing the EXE path and boot flags; other games run
through the same launcher to exercise and harden this layer.
"""
from __future__ import annotations

from pathlib import Path

from win16.api import dialogs, gdi, kernel, mmsystem, sound, user, win87em
from win16.api.core import ApiRegistry
from win16.api.system import Win16System
from win16.loader import Win16Machine, load_ne
from win16.ne import NEExecutable, parse_ne

# __WINFLAGS (KERNEL.178 equate) default: WF_PMODE | WF_CPU286 | WF_STANDARD,
# no WF_80x87 — the loader leaves FP OSFIXUPs unapplied, so a program's INT
# 34h..3Dh emulator forms stay live.  A game with real x87 (no OSFIXUPs) can
# pass a value with WF_80x87 set.
WINFLAGS_NO_FPU = 0x0013


def build_registry(*, winflags: int = WINFLAGS_NO_FPU) -> ApiRegistry:
    api = ApiRegistry()
    api.register_equate("KERNEL", 178, winflags)       # __WINFLAGS
    api.register_equate("KERNEL", 113, 3)              # __AHSHIFT (huge-array stride)
    api.register_equate("KERNEL", 114, 8)              # __AHINCR
    kernel.install(api)
    user.install(api)
    gdi.install(api)
    sound.install(api)
    mmsystem.install(api)
    dialogs.install(api)
    win87em.install(api)
    return api


def create_machine(exe_path: str | Path, *,
                   winflags: int = WINFLAGS_NO_FPU) -> Win16Machine:
    """Parse the NE at `exe_path`, load it, and return a booted machine."""
    machine = load_ne(parse_ne(exe_path), build_registry(winflags=winflags))
    Win16System(machine)
    return machine
