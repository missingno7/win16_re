"""SimAnt bring-up gate: boots through startup into its running window and
paints its first frame (the MAXIS splash).

SimAnt is a full commercial Win16 app — reaching a painted frame exercises raw
INT 21h file I/O, programmatic menu construction, the selector heap under
GlobalReAlloc, 16-colour (4bpp) DIB rendering, and the font/palette setup.
Bounded on the outcome (first non-blank paint of its AntRoot window), like the
microman gate, because the app self-drives and never goes idle.
"""
import pytest

from simant import runtime

pytestmark = pytest.mark.skipif(not runtime.assets_present(),
                                reason="simant assets not present")


def test_simant_boots_and_paints_splash():
    machine = runtime.create_machine()
    machine.cpu.trace_enabled = False
    sysm = machine.api.services["system"]

    # Drive through SimAnt's heavy startup (file loads, menu build, palette,
    # font setup) to the point where the MAXIS splash is up.  The splash is a
    # large 16-colour DIB blit that leaves the AntRoot surface substantially
    # non-black; small early UI blits/fills don't reach that.  ~5M instructions
    # reliably has it (empirically the splash is present from ~5M onward).
    while machine.cpu.instruction_count < 5_000_000:
        try:
            machine.cpu.run(200_000)
        except Exception:  # noqa: BLE001 — a later frontier is still acceptable
            break

    assert machine.cpu.instruction_count >= 5_000_000, "startup stalled early"

    win = next((w for w in sysm.windows if w.wndclass.name == "AntRoot"
                and w.visible and sum(w.surface.pixels) > 1_000_000), None)
    assert win is not None, "SimAnt AntRoot never painted its splash frame"

    called = {c.split("(")[0] for c in machine.api.call_log}
    for expected in ("USER.41:CreateWindow", "USER.151:CreateMenu",
                     "GDI.56:CreateFont", "GDI.443:SetDIBitsToDevice"):
        assert expected in called, expected
