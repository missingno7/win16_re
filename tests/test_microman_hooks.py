"""The microman lifted islands (gamehooks/microman.py) — the A/B oracle gate.

Two machines run the same deterministic headless boot side by side: one pure
ASM, one with the WAP fill/copy islands hooked.  At every checkpoint the
window pixels must be IDENTICAL — the hook's value is byte-exact speed, and
this gate is what makes it a recovery instead of an approximation.
"""
import hashlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import ppython._env  # noqa: E402,F401

from gamehooks import install_game_hooks  # noqa: E402
from scripts.games import game_exe, game_winflags  # noqa: E402
from win16.app import create_machine  # noqa: E402

MICROMAN = game_exe("microman")

pytestmark = pytest.mark.skipif(not MICROMAN.exists(),
                                reason="microman assets not present")

# 20 batches reaches the first WAP page-transition animation (~batch 13+),
# where the islands fire tens of thousands of times — the boot window alone
# never enters these loops.
BATCHES = 20
BATCH_STEPS = 500


def _drive(hooked: bool):
    machine = create_machine(MICROMAN, winflags=game_winflags("microman"))
    machine.cpu.trace_enabled = False
    if hooked:
        assert install_game_hooks("microman", machine) == 17
    sysobj = machine.api.services["system"]
    hashes = []
    for _ in range(BATCHES):
        machine.cpu.run(BATCH_STEPS)
        win = next((w for w in sysobj.windows
                    if w.wndclass.name == "MicroManClass"), None)
        pixels = bytes(win.surface.pixels) if win is not None else b""
        hashes.append(hashlib.sha256(pixels).hexdigest())
    return machine, hashes


def test_islands_are_pixel_exact_and_engaged():
    plain, plain_hashes = _drive(hooked=False)
    hooked, hooked_hashes = _drive(hooked=True)

    # Byte-exact rendering at every checkpoint.
    assert hooked_hashes == plain_hashes

    # The islands actually engaged: a hook consumes ONE instruction where the
    # ASM loops consumed dozens per byte, so the hooked run must reach the
    # same checkpoints in materially fewer instructions.
    assert hooked.cpu.instruction_count < plain.cpu.instruction_count * 0.9, (
        f"hooks never engaged: {hooked.cpu.instruction_count} vs "
        f"{plain.cpu.instruction_count}")


def test_install_refuses_wrong_code():
    """A binary whose code segment lacks the WAP loop bodies must be refused
    (an island landing on different code corrupts silently)."""
    machine = create_machine(MICROMAN, winflags=game_winflags("microman"))
    from gamehooks import microman as mm
    cs = machine.seg_bases[mm.CODE_SEG_INDEX]
    machine.mem.data[cs << 4:(cs << 4) + 0x10000] = bytes(0x10000)
    with pytest.raises(AssertionError):
        mm.install(machine)
