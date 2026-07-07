"""Parse Win16 DIALOG resource templates (DLGTEMPLATE, 16-bit).  Pure.

Template layout:
    DWORD style; BYTE item_count; WORD x, y, cx, cy;
    menu name (ASCIIZ / 0xFF+ord / empty); class name; caption;
    if DS_SETFONT: WORD point_size; ASCIIZ font_name;
Then item_count control entries:
    WORD x, y, cx, cy, id; DWORD style;
    class: BYTE 0x80..0x85 (Button/Edit/Static/ListBox/ScrollBar/ComboBox)
           or ASCIIZ custom class;
    text: ASCIIZ (or 0xFF + WORD resource ordinal);
    BYTE extra_len (creation data, skipped).

Coordinates are dialog units: px_x = du_x * base_w / 4, px_y = du_y * base_h / 8
with (base_w, base_h) from the dialog font — (8, 13) for our fixed system font.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

DS_SETFONT = 0x40

CONTROL_CLASSES = {
    0x80: "Button", 0x81: "Edit", 0x82: "Static",
    0x83: "ListBox", 0x84: "ScrollBar", 0x85: "ComboBox",
}

# Button styles (low nibble of the control style).
BS_PUSHBUTTON = 0x0
BS_DEFPUSHBUTTON = 0x1
BS_CHECKBOX = 0x2
BS_AUTOCHECKBOX = 0x3
BS_RADIOBUTTON = 0x4
BS_GROUPBOX = 0x7
BS_AUTORADIOBUTTON = 0x9
# Static styles.
SS_LEFT, SS_CENTER, SS_RIGHT, SS_ICON = 0x0, 0x1, 0x2, 0x3


@dataclass
class DialogControl:
    x: int
    y: int
    cx: int
    cy: int
    ctrl_id: int
    style: int
    cls: str                    # "Button", "Edit", ... or a custom class name
    text: str


@dataclass
class DialogTemplate:
    style: int
    x: int
    y: int
    cx: int
    cy: int
    menu: str
    cls: str
    caption: str
    point_size: int | None
    font: str | None
    controls: list[DialogControl] = field(default_factory=list)


def _name(data: bytes, pos: int) -> tuple[str, int]:
    if data[pos] == 0xFF:                       # ordinal reference
        (ordv,) = struct.unpack_from("<H", data, pos + 1)
        return f"#{ordv}", pos + 3
    end = data.index(b"\x00", pos)
    return data[pos:end].decode("latin-1"), end + 1


def parse_dialog(data: bytes) -> DialogTemplate:
    (style,) = struct.unpack_from("<I", data, 0)
    count = data[4]
    x, y, cx, cy = struct.unpack_from("<HHHH", data, 5)
    pos = 13
    menu, pos = _name(data, pos)
    cls, pos = _name(data, pos)
    caption, pos = _name(data, pos)
    point_size = font = None
    if style & DS_SETFONT:
        (point_size,) = struct.unpack_from("<H", data, pos)
        font, pos = _name(data, pos + 2)

    tpl = DialogTemplate(style, x, y, cx, cy, menu, cls, caption,
                         point_size, font)
    for _ in range(count):
        ix, iy, icx, icy, cid = struct.unpack_from("<HHHHH", data, pos)
        (istyle,) = struct.unpack_from("<I", data, pos + 10)
        pos += 14
        if data[pos] & 0x80:
            ccls = CONTROL_CLASSES.get(data[pos])
            if ccls is None:
                raise ValueError(f"unknown control class byte {data[pos]:#04x}")
            pos += 1
        else:
            ccls, pos = _name(data, pos)
        text, pos = _name(data, pos)
        extra = data[pos]
        pos += 1 + extra
        tpl.controls.append(DialogControl(ix, iy, icx, icy, cid, istyle,
                                          ccls, text))
    return tpl


def du_to_px(du_x: int, du_y: int, base: tuple[int, int] = (8, 13)) -> tuple[int, int]:
    """Dialog units -> pixels for the dialog's font base size."""
    return du_x * base[0] // 4, du_y * base[1] // 8
