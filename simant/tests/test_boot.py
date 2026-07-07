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

    # "Booted through real startup and rendered" is proven by the API sequence
    # SimAnt only reaches deep in startup (file loads -> menu build -> palette
    # -> font setup -> the MAXIS splash DIB blit at ~3.4M instructions), plus a
    # painted window.  Pixel-sum thresholds are unreliable here (substantial
    # content lands within the first 200k, and the mostly-black splash sums
    # lower than a solid fill).  Drive until the splash-rendering calls appear,
    # tolerating the next frontier (SimAnt keeps advancing past the splash).
    goal = {"GDI.56:CreateFont", "GDI.443:SetDIBitsToDevice"}
    for _ in range(80):
        try:
            machine.cpu.run(200_000)
        except Exception:  # noqa: BLE001 — a later frontier is still acceptable
            break
        called = {c.split("(")[0] for c in machine.api.call_log}
        if goal <= called:
            break

    called = {c.split("(")[0] for c in machine.api.call_log}
    for expected in ("USER.41:CreateWindow", "USER.151:CreateMenu",
                     "GDI.56:CreateFont", "GDI.443:SetDIBitsToDevice"):
        assert expected in called, expected

    win = next((w for w in sysm.windows if w.wndclass.name == "AntRoot"
                and w.visible and sum(w.surface.pixels) > 0), None)
    assert win is not None, "SimAnt AntRoot never painted a frame"
