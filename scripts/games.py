"""The test-game registry.

Paulie Python is the RE target (its adapter lives in `ppython/`); the other
games are fixtures used only to exercise and harden the game-agnostic `win16/`
layer — run them with `scripts/boot.py <name>`.  Each entry is the EXE path
relative to assets/ plus its boot __WINFLAGS.
"""
from __future__ import annotations

from pathlib import Path

from win16.app import WINFLAGS_NO_FPU

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS = REPO_ROOT / "assets"

GAMES = {
    # name        (exe path under assets/,        winflags)
    "ppython":    ("PPYTHON/PYTHON.EXE",          WINFLAGS_NO_FPU),
    "microman":   ("MICROMAN/MICROMAN.EXE",       WINFLAGS_NO_FPU),
    "simant":     ("ANTWIN/SIMANTW.EXE",          WINFLAGS_NO_FPU),  # brought up
    # Not yet brought up — listed so `boot.py` can reach them:
    "bangbang":   ("BANGBANG/BANGBANG.EXE",       WINFLAGS_NO_FPU),
    "kye":        ("KYE/KYE.EXE",                 WINFLAGS_NO_FPU),
    "skifree":    ("SKIFREE/SKI.EXE",             WINFLAGS_NO_FPU),
}


def game_exe(name: str) -> Path:
    if name not in GAMES:
        raise SystemExit(f"unknown game {name!r}; known: {', '.join(sorted(GAMES))}")
    return ASSETS / GAMES[name][0]


def game_winflags(name: str) -> int:
    return GAMES[name][1]


def install_game_hooks(name: str, machine) -> int:
    """Install a game's lifted-island hooks if it ships a package with them.

    Each game with recovered hot-path hooks is its own package (e.g.
    `microman/`) exposing `runtime.install_hooks(machine)`.  Games without a
    package (or without hooks) install nothing.  Returns the count installed.
    """
    import importlib
    try:
        runtime = importlib.import_module(f"{name}.runtime")
    except ModuleNotFoundError:
        return 0
    installer = getattr(runtime, "install_hooks", None)
    return installer(machine) if installer is not None else 0
