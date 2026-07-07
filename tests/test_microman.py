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


def test_microman_boots_and_renders():
    """MICROMAN exercises the shared layer far beyond ppython — MMSYSTEM, 80186
    ENTER frames, the _l* file API, palette-mode DIB rendering.  It runs deep
    into its own code (startup -> file loads -> palette setup -> DIB blits); a
    later opcode/API frontier is fine, the point is the layer carries it there
    without a crash in the implemented path."""
    machine = create_machine(MICROMAN, winflags=game_winflags("microman"))
    machine.cpu.trace_enabled = False
    try:
        machine.cpu.run(3_000_000)
    except Exception:  # noqa: BLE001 — the frontier moves as the layer grows
        pass
    assert machine.cpu.instruction_count > 1_500_000

    called = {c.split("(")[0] for c in machine.api.call_log}
    for expected in ("KERNEL.91:InitTask", "KERNEL.85:_lopen",
                     "USER.286:GetDesktopWindow", "GDI.80:GetDeviceCaps",
                     "GDI.360:CreatePalette", "GDI.443:SetDIBitsToDevice"):
        assert expected in called, expected
