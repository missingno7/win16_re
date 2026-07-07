"""Boot wiring for the MicroMan fixture (assets/MICROMAN/MICROMAN.EXE).

A thin game adapter over the generic win16 launcher (`win16.app`): it pins the
EXE path and boot flags, and installs microman's lifted-island hooks.  The
Win16 API surface, loader and OS state are all the game-agnostic framework.
"""
from __future__ import annotations

from pathlib import Path

from . import _env  # noqa: F401  (puts the dos_re framework on sys.path)

from win16.app import WINFLAGS_NO_FPU, create_machine as _create_machine
from win16.loader import Win16Machine
from win16.ne import NEExecutable, parse_ne

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS = REPO_ROOT / "assets"
EXE_PATH = ASSETS / "MICROMAN" / "MICROMAN.EXE"

# Canonical game name (matches scripts/games.py and snapshot metadata).
GAME_NAME = "microman"


def assets_present() -> bool:
    return EXE_PATH.exists()


def load_exe() -> NEExecutable:
    return parse_ne(EXE_PATH)


def create_machine() -> Win16Machine:
    return _create_machine(EXE_PATH, winflags=WINFLAGS_NO_FPU)


def install_hooks(machine) -> int:
    """Install microman's lifted-island hooks; returns the number installed."""
    from . import hooks
    return hooks.install(machine)
