"""MICROMAN — a second Win16 game used to harden the game-agnostic layer.

Not an RE target; this just proves the shared win16 launcher loads a different
NE (different modules incl. MMSYSTEM, real 80186 ENTER frames, the _l* file
API, palette-mode device queries) and runs deep into its own code, reporting a
clean named frontier rather than crashing.
"""
import pytest

from microman import runtime

pytestmark = pytest.mark.skipif(not runtime.assets_present(),
                                reason="microman assets not present")


def test_microman_boots_and_renders():
    """MICROMAN exercises the shared layer far beyond ppython — MMSYSTEM, 80186
    ENTER frames, the _l* file API, palette-mode DIB rendering.  Under the
    selector-based global heap it runs all the way THROUGH startup into its WAP
    title animation and paints a real frame (its WM_CREATE alone is ~10M nested
    instructions loading the sprites).  Because the game self-animates
    (InvalidateRect every frame) it never goes idle, so bound the drive on the
    outcome — the first non-blank paint of its window — not on an instruction
    budget that would otherwise grind through millions of frames."""
    machine = runtime.create_machine()
    machine.cpu.trace_enabled = False
    sysm = machine.api.services["system"]

    painted = False
    for _ in range(80):                     # bounded: reaches the first render
        try:
            machine.cpu.run(400)
        except Exception:  # noqa: BLE001 — a later frontier is still acceptable
            break
        win = next((w for w in sysm.windows
                    if w.wndclass.name == "MicroManClass"), None)
        if win is not None and sum(win.surface.pixels) > 0:
            painted = True
            break

    assert machine.cpu.instruction_count > 1_500_000
    assert painted, "MicroManClass window never painted a non-blank frame"

    called = {c.split("(")[0] for c in machine.api.call_log}
    for expected in ("KERNEL.91:InitTask", "KERNEL.85:_lopen",
                     "USER.286:GetDesktopWindow", "GDI.80:GetDeviceCaps",
                     "GDI.360:CreatePalette", "GDI.443:SetDIBitsToDevice"):
        assert expected in called, expected
