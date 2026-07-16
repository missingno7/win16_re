"""Generic Win16 application launcher — load any NE executable into the VM.

Game-agnostic: builds the full Win16 API surface (KERNEL/USER/GDI/SOUND/
MMSYSTEM/win87em + the dialog engine) over the dos_re CPU and returns a booted
`Win16Machine` with a `Win16System` attached.  A game adapter (e.g. ppython)
is only a thin wrapper choosing the EXE path and boot flags; other games run
through the same launcher to exercise and harden this layer.

The registry factory itself lives in ``win16.api.surface`` (re-exported here
unchanged) so the EXE-independent boot path (``win16.bootimage``) can build
the API surface without this module's loader imports on its import graph.
"""
from __future__ import annotations

from pathlib import Path

from win16.api.surface import WINFLAGS_NO_FPU, build_registry  # noqa: F401 (re-export)
from win16.api.system import Win16System
from win16.loader import Win16Machine, load_ne
from win16.ne import parse_ne


def create_machine(exe_path: str | Path, *,
                   winflags: int = WINFLAGS_NO_FPU) -> Win16Machine:
    """Parse the NE at `exe_path`, load it, and return a booted machine."""
    machine = load_ne(parse_ne(exe_path), build_registry(winflags=winflags))
    Win16System(machine)
    return machine
