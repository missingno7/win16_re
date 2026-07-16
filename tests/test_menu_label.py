"""Win16 menu strings: the TAB splits the label from its accelerator.

A menu string carries its accelerator after a TAB — "&New\tCtrl+N" — and
Windows draws that half RIGHT-ALIGNED in the popup.  Both menu sources speak
the convention: the MENU resource template, and the items a program appends at
runtime through AppendMenu/InsertMenu.  The runtime path used to concatenate
("NewCtrl+N") because it stripped '&' by hand instead of splitting.
"""
from win16.menu import MF_POPUP, MenuItem, split_label


def test_accelerator_splits_off_the_tab():
    assert split_label("&New\tCtrl+N") == ("New", "Ctrl+N")


def test_label_without_accelerator_has_empty_accel():
    assert split_label("&About...") == ("About...", "")


def test_mnemonic_markers_are_stripped_only_from_the_label():
    # '&' marks the mnemonic in the label; the accelerator half is literal.
    assert split_label("E&xit\tAlt+F4") == ("Exit", "Alt+F4")


def test_empty_label_is_not_an_error():
    assert split_label("") == ("", "")


def test_parsed_menu_item_uses_the_same_split():
    it = MenuItem(flags=0, item_id=101, label="&Open\tCtrl+O")
    assert it.text_and_accel() == split_label("&Open\tCtrl+O") == ("Open", "Ctrl+O")


def test_popup_title_keeps_its_text():
    it = MenuItem(flags=MF_POPUP, item_id=None, label="&File")
    assert it.text_and_accel() == ("File", "")
