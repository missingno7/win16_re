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
    except Exception as exc:  # noqa: BLE001 — the frontier moves; the gate below is what must hold
        allowed = ("DialogBox",)            # current frontier: high-score dialog
        assert any(a in str(exc) for a in allowed), f"unexpected gap: {exc!r}"

    boxes = m.api.services.get("messagebox_log", [])
    assert boxes and boxes[0][1] == "Next Screen:"
    assert boxes[0][2] == "Portrait of a Python"        # WAYOUT level 1

    # The playfield must have been painted into the main window.
    surf = main.surface
    assert any(surf.pixels), "main window stayed black — playfield never blitted"

    # The level-start jingle went through SOUND.DRV.
    slog = m.api.services.get("sound_log", [])
    assert any(e[1] == "note" for e in slog), "no SetVoiceNote events recorded"
