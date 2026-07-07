"""MENU resource parser — verified against PYTHON.EXE's menu."""
import pytest

from ppython import runtime
from win16.menu import parse_menu

pytestmark = pytest.mark.skipif(not runtime.assets_present(),
                                reason="game assets not present")


@pytest.fixture(scope="module")
def menu():
    exe = runtime.load_exe()
    return parse_menu(exe.find_resources("MENU")[0].data)


def test_top_level_bar(menu):
    labels = [item.text_and_accel()[0] for item in menu]
    assert labels == ["Game", "Options", "ScreenSculptor", "Help"]
    assert all(item.is_popup for item in menu)


def test_game_menu_commands(menu):
    game = menu[0].children
    by_label = {i.text_and_accel()[0]: i for i in game}
    assert by_label["New"].item_id == 1050
    assert by_label["New"].text_and_accel()[1] == "F2"
    assert by_label["Sound"].item_id == 1100
    assert by_label["Exit"].item_id == 1200
    # "Pause" ships grayed (no game running yet).
    assert not by_label["Pause"].enabled


def test_nested_popups_and_checkmark(menu):
    options = menu[1].children
    attitude = next(i for i in options if i.text_and_accel()[0] == "Attitude")
    assert attitude.is_popup
    names = [c.text_and_accel()[0] for c in attitude.children]
    assert names == ["Garter Snake", "Sidewinder", "Diamondback",
                     "King Cobra", "Black Mamba"]
    # ScreenSculptor ▸ Shape ▸ PPWALL1 is the initially-checked shape.
    sculptor = menu[2].children
    shape = next(i for i in sculptor if i.text_and_accel()[0] == "Shape")
    checked = [c.text_and_accel()[0] for c in shape.children if c.checked]
    assert checked == ["PPWALL1"]
