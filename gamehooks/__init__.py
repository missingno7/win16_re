"""Per-game hook packages — the dos_re method applied to win16 games.

Each module in this package is named after a game in scripts/games.py and
exposes `install(machine) -> int` (number of hooks installed).  Hooks are
GAME-SPECIFIC lifted islands: Python reimplementations of that game's hot
ASM routines, registered at exact CS:IP addresses via the dos_re CPU's
`replacement_hooks`.  The game-agnostic `win16/` layer never imports from
here — hooks are opt-in per launch (play.py `--no-hooks` disables them).

Every module must verify the code bytes at each hook address against the
expected signature at install time and refuse to install on mismatch — a
hook landing on different code corrupts silently.
"""
from __future__ import annotations

import importlib


def install_game_hooks(game_name: str, machine) -> int:
    """Install the hooks module for `game_name` if one exists.

    Returns the number of hooks installed (0 when the game has no module).
    """
    try:
        mod = importlib.import_module(f"gamehooks.{game_name}")
    except ModuleNotFoundError:
        return 0
    return mod.install(machine)
