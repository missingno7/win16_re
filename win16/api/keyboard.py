"""KEYBOARD.DRV — the keyboard LAYOUT driver.

In Win16 the layout is a separate driver module, not part of USER: KEYBOARD.DRV
owns the mapping between virtual keys, scan codes and characters, and an app
imports from it directly.  This module is that driver's Python side.

What a layout is: a table.  There is no way to answer "which scan code is VK_A"
by deriving it — the answer IS the US 101-key layout, the same one Windows 3.1
shipped as its default.  So the table below is the implementation, kept to the
keys a standard US keyboard actually produces; a virtual key outside it raises
rather than inventing a plausible code, because a WRONG scan code or character
is indistinguishable from a right one to the caller and would corrupt whatever
the app builds from it.
"""
from __future__ import annotations

from .core import ApiRegistry, CallContext

# MapVirtualKey wMapType.
MAPVK_VK_TO_VSC = 0     # virtual key -> scan code
MAPVK_VSC_TO_VK = 1     # scan code -> virtual key
MAPVK_VK_TO_CHAR = 2    # virtual key -> unshifted character

# The US 101-key layout: VK -> (scan code, unshifted character).  A character
# of 0 means "this key produces none" (MapVirtualKey type 2's answer for the
# function/navigation/modifier keys — VK_INSERT among them).
_US_LAYOUT: dict[int, tuple[int, int]] = {
    0x08: (0x0E, 0x08),                     # VK_BACK
    0x09: (0x0F, 0x09),                     # VK_TAB
    0x0D: (0x1C, 0x0D),                     # VK_RETURN
    0x10: (0x2A, 0),                        # VK_SHIFT
    0x11: (0x1D, 0),                        # VK_CONTROL
    0x12: (0x38, 0),                        # VK_MENU (Alt)
    0x13: (0x45, 0),                        # VK_PAUSE
    0x14: (0x3A, 0),                        # VK_CAPITAL
    0x1B: (0x01, 0x1B),                     # VK_ESCAPE
    0x20: (0x39, 0x20),                     # VK_SPACE
    0x21: (0x49, 0),                        # VK_PRIOR (PgUp)
    0x22: (0x51, 0),                        # VK_NEXT  (PgDn)
    0x23: (0x4F, 0),                        # VK_END
    0x24: (0x47, 0),                        # VK_HOME
    0x25: (0x4B, 0),                        # VK_LEFT
    0x26: (0x48, 0),                        # VK_UP
    0x27: (0x4D, 0),                        # VK_RIGHT
    0x28: (0x50, 0),                        # VK_DOWN
    0x2C: (0x37, 0),                        # VK_SNAPSHOT
    0x2D: (0x52, 0),                        # VK_INSERT
    0x2E: (0x53, 0),                        # VK_DELETE
    0x90: (0x45, 0),                        # VK_NUMLOCK
    0x91: (0x46, 0),                        # VK_SCROLL
    # Digit row '0'..'9' (VK == ASCII).
    0x30: (0x0B, 0x30), 0x31: (0x02, 0x31), 0x32: (0x03, 0x32),
    0x33: (0x04, 0x33), 0x34: (0x05, 0x34), 0x35: (0x06, 0x35),
    0x36: (0x07, 0x36), 0x37: (0x08, 0x37), 0x38: (0x09, 0x38),
    0x39: (0x0A, 0x39),
    # Numeric keypad: VK_NUMPAD0..9 and the operators.
    0x60: (0x52, 0x30), 0x61: (0x4F, 0x31), 0x62: (0x50, 0x32),
    0x63: (0x51, 0x33), 0x64: (0x4B, 0x34), 0x65: (0x4C, 0x35),
    0x66: (0x4D, 0x36), 0x67: (0x47, 0x37), 0x68: (0x48, 0x38),
    0x69: (0x49, 0x39),
    0x6A: (0x37, 0x2A),                     # VK_MULTIPLY '*'
    0x6B: (0x4E, 0x2B),                     # VK_ADD      '+'
    0x6D: (0x4A, 0x2D),                     # VK_SUBTRACT '-'
    0x6E: (0x53, 0x2E),                     # VK_DECIMAL  '.'
    0x6F: (0x35, 0x2F),                     # VK_DIVIDE   '/'
    # OEM punctuation (US placement).
    0xBA: (0x27, 0x3B),                     # ';'
    0xBB: (0x0D, 0x3D),                     # '='
    0xBC: (0x33, 0x2C),                     # ','
    0xBD: (0x0C, 0x2D),                     # '-'
    0xBE: (0x34, 0x2E),                     # '.'
    0xBF: (0x35, 0x2F),                     # '/'
    0xC0: (0x29, 0x60),                     # '`'
    0xDB: (0x1A, 0x5B),                     # '['
    0xDC: (0x2B, 0x5C),                     # '\'
    0xDD: (0x1B, 0x5D),                     # ']'
    0xDE: (0x28, 0x27),                     # '''
}

# Letters: VK_A..VK_Z are the ASCII codes of 'A'..'Z', and their unshifted
# character is that same UPPERCASE letter (MapVirtualKey knows nothing about
# shift state — that is ToAscii's job).
_LETTER_SCAN = {
    "Q": 0x10, "W": 0x11, "E": 0x12, "R": 0x13, "T": 0x14, "Y": 0x15,
    "U": 0x16, "I": 0x17, "O": 0x18, "P": 0x19, "A": 0x1E, "S": 0x1F,
    "D": 0x20, "F": 0x21, "G": 0x22, "H": 0x23, "J": 0x24, "K": 0x25,
    "L": 0x26, "Z": 0x2C, "X": 0x2D, "C": 0x2E, "V": 0x2F, "B": 0x30,
    "N": 0x31, "M": 0x32,
}
for _letter, _scan in _LETTER_SCAN.items():
    _US_LAYOUT[ord(_letter)] = (_scan, ord(_letter))

# Function keys F1..F12: scan codes 0x3B..0x44 then 0x57/0x58; no character.
for _i in range(10):
    _US_LAYOUT[0x70 + _i] = (0x3B + _i, 0)
_US_LAYOUT[0x7A] = (0x57, 0)                # VK_F11
_US_LAYOUT[0x7B] = (0x58, 0)                # VK_F12

_VSC_TO_VK = {}
for _vk, (_sc, _ch) in _US_LAYOUT.items():
    _VSC_TO_VK.setdefault(_sc, _vk)         # first VK wins (main block over pad)


def install(api: ApiRegistry) -> None:
    @api.register("KEYBOARD", 131, args="word word")
    def MapVirtualKey(ctx: CallContext) -> int:         # (wCode, wMapType)
        # Translate between a virtual key, its scan code and its unshifted
        # character, per the LAYOUT — never per the current shift state.
        #
        # Observed contract (SimAnt's _DoKeyDown): type 2 then type 0 on the
        # WM_KEYDOWN wParam to build a (char, scancode) event record — reached
        # only for VK_SPACE and VK_INSERT — and type 2 on VK_A..VK_Z for its
        # cheat-key buffer, where the result is fed STRAIGHT into the C
        # runtime's 256-entry ctype table as an index (`test [bx+_ctype],2`).
        # That indexing is why the character must be a plain 0..255 byte and
        # why "no character" must be 0, not a guess.
        code, map_type = ctx.args
        if map_type == MAPVK_VK_TO_VSC:
            return _entry(code)[0]
        if map_type == MAPVK_VK_TO_CHAR:
            return _entry(code)[1]
        if map_type == MAPVK_VSC_TO_VK:
            vk = _VSC_TO_VK.get(code)
            if vk is None:
                raise NotImplementedError(
                    f"MapVirtualKey: scan code {code:#04x} is not on the US layout")
            return vk
        raise NotImplementedError(f"MapVirtualKey wMapType {map_type}")


def _entry(vk: int) -> tuple[int, int]:
    entry = _US_LAYOUT.get(vk)
    if entry is None:
        raise NotImplementedError(
            f"MapVirtualKey: virtual key {vk:#04x} is not on the US layout — "
            "a plausible scan code / character here is indistinguishable from "
            "a real one to the caller, so this fails instead of guessing")
    return entry
