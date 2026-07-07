"""Boot wiring for the RE target, Paulie Python (assets/PPYTHON/PYTHON.EXE).

This is a thin game adapter over the generic win16 launcher (`win16.app`):
it only pins the EXE path and boot flags.  Everything else — the Win16 API
surface, the loader, the OS state — is the game-agnostic framework.
"""
from __future__ import annotations

from pathlib import Path

from . import _env  # noqa: F401  (puts the dos_re framework on sys.path)

from win16.app import WINFLAGS_NO_FPU, create_machine as _create_machine
from win16.loader import Win16Machine
from win16.ne import NEExecutable, parse_ne

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS = REPO_ROOT / "assets"
EXE_PATH = ASSETS / "PPYTHON" / "PYTHON.EXE"


def assets_present() -> bool:
    return EXE_PATH.exists()


def load_exe() -> NEExecutable:
    return parse_ne(EXE_PATH)


def create_machine() -> Win16Machine:
    # PYTHON.EXE links win87em but its NE carries real x87 opcodes (no OSFIXUPs
    # applied), so it runs FPU-less-emulator-form free — WINFLAGS_NO_FPU.
    return _create_machine(EXE_PATH, winflags=WINFLAGS_NO_FPU)
