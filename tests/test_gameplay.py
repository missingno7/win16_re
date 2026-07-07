"""The gameplay gate: boot to idle, start a New Game, watch it actually play."""
import pytest

from ppython import runtime

pytestmark = pytest.mark.skipif(not runtime.assets_present(),
                                reason="game assets not present")


def test_new_game_reaches_playfield_and_music():
    m = runtime.create_machine()
    m.cpu.trace_enabled = False
    m.cpu.run(1_500_000)                    # boot -> idle loop
    sysobj = m.api.services["system"]
    main = sysobj.windows[0]
    sysobj.post_message(main.handle, 0x0111, 1050, 0)   # WM_COMMAND: &New (F2)
    try:
        m.cpu.run(6_000_000)
    except Exception:  # noqa: BLE001 — the frontier keeps moving; the gate below is what matters
        pass

    boxes = m.api.services.get("messagebox_log", [])
    assert boxes and boxes[0][1] == "Next Screen:"
    assert boxes[0][2] == "Portrait of a Python"        # WAYOUT level 1

    # The playfield must have been painted into the main window.
    surf = main.surface
    assert any(surf.pixels), "main window stayed black — playfield never blitted"

    # The level-start jingle went through SOUND.DRV.
    slog = m.api.services.get("sound_log", [])
    assert any(e[1] == "note" for e in slog), "no SetVoiceNote events recorded"


def test_f2_accelerator_starts_new_game():
    """Deterministic proof the interactive input path works: a WM_KEYDOWN for
    VK_F2 must, via TranslateAccelerator + the accel table, become a New Game
    WM_COMMAND — exactly what pressing F2 in the real window does."""
    m = runtime.create_machine()
    m.cpu.trace_enabled = False
    m.cpu.run(1_500_000)                    # boot -> idle message loop
    sysobj = m.api.services["system"]
    main = sysobj.windows[0]
    sysobj.post_message(main.handle, 0x0100, 0x71, 0x0001)  # WM_KEYDOWN VK_F2
    try:
        m.cpu.run(6_000_000)
    except Exception:  # noqa: BLE001 — a later frontier may stop it; New-Game start is the gate
        pass
    boxes = m.api.services.get("messagebox_log", [])
    assert any(b[1] == "Next Screen:" for b in boxes), \
        "F2 accelerator did not start a new game"
