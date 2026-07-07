"""MessageBox button sets + return codes (win16/msgbox.py) — the Yes/No fix.

microman's Restart menu calls MessageBox with MB_YESNO|MB_ICONQUESTION
(0x24); the old handler always returned IDOK(1), which the game read as "not
Yes" and cancelled the restart.  This pins the mapping the API + host UI share.
"""
from win16 import msgbox


def test_yesno_buttons_and_ids():
    assert msgbox.buttons(0x24) == [("Yes", 6), ("No", 7)]     # MB_YESNO + icon
    assert msgbox.default_result(0x24) == 6                     # IDYES
    assert msgbox.close_result(0x24) == 7                       # IDNO (no Cancel)


def test_all_button_sets():
    assert msgbox.buttons(msgbox.MB_OK) == [("OK", 1)]
    assert msgbox.buttons(msgbox.MB_OKCANCEL) == [("OK", 1), ("Cancel", 2)]
    assert msgbox.buttons(msgbox.MB_ABORTRETRYIGNORE) == [
        ("Abort", 3), ("Retry", 4), ("Ignore", 5)]
    assert msgbox.buttons(msgbox.MB_YESNOCANCEL) == [
        ("Yes", 6), ("No", 7), ("Cancel", 2)]
    assert msgbox.buttons(msgbox.MB_RETRYCANCEL) == [("Retry", 4), ("Cancel", 2)]


def test_defaults_and_close():
    # Default = first (affirmative) button; close = Cancel, else No, else sole.
    assert msgbox.default_result(msgbox.MB_OK) == 1
    assert msgbox.close_result(msgbox.MB_OK) == 1
    assert msgbox.close_result(msgbox.MB_YESNOCANCEL) == 2      # Cancel wins
    assert msgbox.close_result(msgbox.MB_YESNO) == 7            # No
    assert msgbox.default_result(msgbox.MB_ABORTRETRYIGNORE) == 3


def test_unknown_high_bits_ignored():
    # High nibbles are icon/default/modality flags; only the low nibble picks.
    assert msgbox.buttons(0x4) == msgbox.buttons(0xF204)        # both MB_YESNO
    assert msgbox.default_result(0x4024) == 6                   # still MB_YESNO
