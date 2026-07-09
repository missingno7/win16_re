"""The game registry.

SimAnt is the sole target of this project (adapter in `simant/`); run it with
`scripts/boot.py simant`.  Each entry is the EXE path relative to assets/ plus
its boot __WINFLAGS.  (The other games this framework was hardened on have been
moved out to a separate project.)
"""
from __future__ import annotations

from pathlib import Path

from win16.app import WINFLAGS_NO_FPU

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS = REPO_ROOT / "assets"

GAMES = {
    # name        (exe path under assets/,        winflags)
    "simant":     ("ANTWIN/SIMANTW.EXE",          WINFLAGS_NO_FPU),
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
