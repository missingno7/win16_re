"""MICROMAN — a second Win16 game used to harden the game-agnostic layer.

Not an RE target; this just proves the shared win16 launcher loads a different
NE (different modules incl. MMSYSTEM, real 80186 ENTER frames, the _l* file
API, palette-mode device queries) and runs deep into its own code, reporting a
clean named frontier rather than crashing.
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import ppython._env  # noqa: E402,F401

from scripts.games import game_exe, game_winflags  # noqa: E402
from win16.api.core import Win16ApiGap  # noqa: E402
from win16.api.system import Win16System  # noqa: E402
from win16.app import create_machine  # noqa: E402

MICROMAN = game_exe("microman")

pytestmark = pytest.mark.skipif(not MICROMAN.exists(),
                                reason="microman assets not present")


def test_microman_boots_deep_into_game_code():
    machine = create_machine(MICROMAN, winflags=game_winflags("microman"))
    Win16System(machine)
    machine.cpu.trace_enabled = False
    # It should run WELL past startup (into file loading + game init) before it
    # reaches the current frontier, the palette subsystem (GDI.360 CreatePalette).
    with pytest.raises(Win16ApiGap, match=r"GDI\.360:CreatePalette"):
        machine.cpu.run(2_000_000)
    assert machine.cpu.instruction_count > 1_000_000

    called = {c.split("(")[0] for c in machine.api.call_log}
    for expected in ("KERNEL.91:InitTask", "KERNEL.85:_lopen",
                     "USER.286:GetDesktopWindow", "GDI.80:GetDeviceCaps"):
        assert expected in called, expected
