"""Dialog resource parsing + the dialog engine running the game's procs."""
import pytest

from ppython import runtime
from win16.dialog import du_to_px, parse_dialog

pytestmark = pytest.mark.skipif(not runtime.assets_present(),
                                reason="game assets not present")


def _dialog(exe, name):
    res = exe.lookup_resource("DIALOG", name)
    assert res is not None, name
    return parse_dialog(res.data)


def test_parse_all_dialogs():
    exe = runtime.load_exe()
    assert len(exe.find_resources("DIALOG")) == 6
    about = _dialog(exe, "myd_about")
    assert about.caption == "About Paulie Python"
    assert about.font == "Helv"
    ok = next(c for c in about.controls if c.ctrl_id == 1)
    assert ok.cls == "Button" and "OK" in ok.text

    hi = _dialog(exe, "myd_high_scores")
    assert hi.caption == "REPTILE HALL OF FAME"
    edits = [c for c in hi.controls if c.cls == "Edit"]
    assert len(edits) == 5                          # five name entry fields
    combos = [c for c in _dialog(exe, "myd_select_screen").controls
              if c.cls == "ComboBox"]
    assert len(combos) == 2                         # screen name + screen set


def test_dialog_units_convert():
    assert du_to_px(4, 8) == (8, 13)                # one char cell (8x13 font)


def _run_command(cmd_id, budget=2_500_000):
    """Post a menu command, run the game, return (dialog_name, result) once
    the modal dialog proc calls EndDialog.  Uses the headless auto-OK host."""
    import win16.api.dialogs as dialogs
    m = runtime.create_machine()
    m.cpu.trace_enabled = False
    m.cpu.run(1_500_000)
    captured = []
    orig = dialogs._run_modal
    def spy(ctx, dlg):
        result = orig(ctx, dlg)
        captured.append((dlg.name, result))
        return result
    dialogs._run_modal = spy
    try:
        s = m.api.services["system"]
        s.post_message(s.windows[0].handle, 0x0111, cmd_id, 0)
        try:
            m.cpu.run(budget)
        except Exception:  # noqa: BLE001 — the game runs on after the dialog
            pass
    finally:
        dialogs._run_modal = orig
    return captured


def test_about_dialog_runs_and_returns_ok():
    captured = _run_command(4050)                   # Help > About
    assert captured, "About dialog never ran its proc"
    name, result = captured[0]
    assert name == "myd_about_shareware"
    assert result == 1                              # IDOK


def test_high_scores_dialog_runs():
    captured = _run_command(1175)                   # Game > High Scores
    assert captured and captured[0][0] == "myd_high_scores"
