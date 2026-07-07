"""Parse a Win16 MENU resource template into a tree.  Pure, stdlib-only.

Template: a MENUITEMTEMPLATEHEADER (WORD version, WORD offset — both 0 here)
followed by items.  Each item is a WORD of MF_* flags; a popup then carries an
ASCIIZ label and nested items (terminated by MF_END at that level), a command
item carries a WORD id then an ASCIIZ label.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

MF_GRAYED = 0x0001
MF_DISABLED = 0x0002
MF_CHECKED = 0x0008
MF_POPUP = 0x0010
MF_MENUBREAK = 0x0040
MF_END = 0x0080
MF_SEPARATOR = 0x0800


@dataclass
class MenuItem:
    label: str                      # raw, with '&' mnemonics and '\t' accel text
    item_id: int | None             # command id, or None for a popup
    flags: int
    children: list["MenuItem"] = field(default_factory=list)

    @property
    def is_popup(self) -> bool:
        return bool(self.flags & MF_POPUP)

    @property
    def is_separator(self) -> bool:
        return (self.flags & MF_SEPARATOR) or (
            self.item_id in (0, None) and not self.label and not self.is_popup)

    @property
    def enabled(self) -> bool:
        return not (self.flags & (MF_GRAYED | MF_DISABLED))

    @property
    def checked(self) -> bool:
        return bool(self.flags & MF_CHECKED)

    def text_and_accel(self) -> tuple[str, str]:
        """Display label (mnemonic '&' stripped) and its accelerator hint."""
        label, _, accel = self.label.partition("\t")
        return label.replace("&", ""), accel


def _asciiz(data: bytes, pos: int) -> tuple[str, int]:
    end = data.index(b"\x00", pos)
    return data[pos:end].decode("latin-1"), end + 1


def _parse_items(data: bytes, pos: int) -> tuple[list[MenuItem], int]:
    items: list[MenuItem] = []
    while pos < len(data):
        (flags,) = struct.unpack_from("<H", data, pos)
        pos += 2
        if flags & MF_POPUP:
            label, pos = _asciiz(data, pos)
            children, pos = _parse_items(data, pos)
            items.append(MenuItem(label, None, flags, children))
        else:
            (item_id,) = struct.unpack_from("<H", data, pos)
            pos += 2
            label, pos = _asciiz(data, pos)
            items.append(MenuItem(label, item_id, flags))
        if flags & MF_END:
            break
    return items, pos


def parse_menu(data: bytes) -> list[MenuItem]:
    """Top-level menu items (the menu bar) of a MENU resource."""
    items, _ = _parse_items(data, 4)    # skip the 4-byte template header
    return items
