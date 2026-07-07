"""MessageBox button sets and return codes (USER.MessageBox, ordinal 1).

The low nibble of the MessageBox `type` selects which buttons appear; each
button returns a specific ID.  This is the game-agnostic mapping shared by the
API handler (for the headless default) and any host modal UI (to render the
right buttons and report the right ID).
"""
from __future__ import annotations

# Button-set selector (mtype & 0x0F).
MB_OK = 0x0
MB_OKCANCEL = 0x1
MB_ABORTRETRYIGNORE = 0x2
MB_YESNOCANCEL = 0x3
MB_YESNO = 0x4
MB_RETRYCANCEL = 0x5

# Return codes.
IDOK, IDCANCEL, IDABORT, IDRETRY, IDIGNORE, IDYES, IDNO = 1, 2, 3, 4, 5, 6, 7

# mtype & 0x0F -> list of (label, id).  Order is left-to-right.
_BUTTONS = {
    MB_OK: [("OK", IDOK)],
    MB_OKCANCEL: [("OK", IDOK), ("Cancel", IDCANCEL)],
    MB_ABORTRETRYIGNORE: [("Abort", IDABORT), ("Retry", IDRETRY),
                          ("Ignore", IDIGNORE)],
    MB_YESNOCANCEL: [("Yes", IDYES), ("No", IDNO), ("Cancel", IDCANCEL)],
    MB_YESNO: [("Yes", IDYES), ("No", IDNO)],
    MB_RETRYCANCEL: [("Retry", IDRETRY), ("Cancel", IDCANCEL)],
}


def buttons(mtype: int) -> list[tuple[str, int]]:
    """The (label, id) buttons for a MessageBox type (defaults to OK)."""
    return _BUTTONS.get(mtype & 0x0F, _BUTTONS[MB_OK])


def default_result(mtype: int) -> int:
    """The ID a headless/auto-dismissed box reports: the DEFAULT (first)
    button — the affirmative for every set (OK / Abort / Yes / Retry)."""
    return buttons(mtype)[0][1]


def close_result(mtype: int) -> int:
    """The ID for closing the box without choosing (Esc / window close): the
    real Windows rule is Cancel when present, else No, else the sole button."""
    ids = [bid for _lbl, bid in buttons(mtype)]
    for candidate in (IDCANCEL, IDNO):
        if candidate in ids:
            return candidate
    return ids[-1]
