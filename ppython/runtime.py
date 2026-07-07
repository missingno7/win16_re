"""Boot wiring for PYTHON.EXE: parse the NE, build the API registry, load."""
from __future__ import annotations

from pathlib import Path

from . import _env  # noqa: F401

from win16.api import gdi, kernel, user, win87em
from win16.api.core import ApiRegistry
from win16.api.system import Win16System
from win16.loader import Win16Machine, load_ne
from win16.ne import NEExecutable, parse_ne

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS = REPO_ROOT / "assets"
EXE_PATH = ASSETS / "PYTHON.EXE"

# __WINFLAGS (KERNEL.178 equate): WF_PMODE | WF_CPU286 | WF_STANDARD, no
# WF_80x87 — this loader never applies FP OSFIXUPs, so the executable's
# INT 34h..3Dh emulator forms stay live and the machine must service them.
WINFLAGS_NO_FPU = 0x0013


def assets_present() -> bool:
    return EXE_PATH.exists()


def load_exe() -> NEExecutable:
    return parse_ne(EXE_PATH)


def create_registry() -> ApiRegistry:
    api = ApiRegistry()
    api.register_equate("KERNEL", 178, WINFLAGS_NO_FPU)
    kernel.install(api)
    user.install(api)
    gdi.install(api)
    win87em.install(api)
    return api


def create_machine() -> Win16Machine:
    machine = load_ne(load_exe(), create_registry())
    Win16System(machine)
    return machine
