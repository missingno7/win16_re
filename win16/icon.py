"""Decode Win16 icon resources (GROUP_ICON + ICON) to RGBA pixels.  Pure.

An ICON resource is a DIB whose BITMAPINFOHEADER height is 2x the icon height:
the top half is the colour (XOR) image, the bottom half a 1bpp AND mask (1 =
transparent).  A GROUP_ICON (RT_GROUP_ICON) is a directory of icon ids at
different sizes/depths.
"""
from __future__ import annotations

import struct


def group_icon_entries(data: bytes):
    """[(width, height, bpp, icon_resource_id), ...] from a GROUP_ICON."""
    _res, rtype, count = struct.unpack_from("<HHH", data, 0)
    entries = []
    pos = 6
    for _ in range(count):
        width, height, _colors, _res2, _planes, bitcount = \
            struct.unpack_from("<BBBBHH", data, pos)
        (_bytes,) = struct.unpack_from("<I", data, pos + 8)
        (icon_id,) = struct.unpack_from("<H", data, pos + 12)
        entries.append((width or 256, height or 256, bitcount, icon_id))
        pos += 14
    return entries


def decode_icon(data: bytes) -> tuple[int, int, bytearray]:
    """An ICON resource -> (width, height, RGBA bytes, top-down)."""
    (size, w, full_h, planes, bpp, comp) = struct.unpack_from("<IiiHHI", data, 0)
    if size != 40 or comp != 0:
        raise ValueError(f"icon: unsupported header size={size} comp={comp}")
    h = full_h // 2
    clr_used = struct.unpack_from("<I", data, 32)[0]
    pal_count = clr_used or (1 << bpp if bpp <= 8 else 0)

    off = 40
    palette = []
    for _ in range(pal_count):
        b, g, r, _a = struct.unpack_from("<BBBB", data, off)
        palette.append((r, g, b))
        off += 4

    xor_row = ((w * bpp + 31) // 32) * 4
    and_row = ((w + 31) // 32) * 4
    xor = off
    and_ = off + xor_row * h

    rgba = bytearray(w * h * 4)
    for row in range(h):
        src_xor = xor + (h - 1 - row) * xor_row       # bottom-up
        src_and = and_ + (h - 1 - row) * and_row
        dst = row * w * 4
        for x in range(w):
            if bpp == 4:
                byte = data[src_xor + (x >> 1)]
                idx = (byte >> 4) if (x & 1) == 0 else (byte & 0x0F)
                r, g, b = palette[idx]
            elif bpp == 8:
                r, g, b = palette[data[src_xor + x]]
            elif bpp == 1:
                idx = (data[src_xor + (x >> 3)] >> (7 - (x & 7))) & 1
                r, g, b = palette[idx]
            elif bpp in (24, 32):
                p = src_xor + x * (bpp // 8)
                b, g, r = data[p], data[p + 1], data[p + 2]
            else:
                raise ValueError(f"icon: unsupported bpp {bpp}")
            mask = (data[src_and + (x >> 3)] >> (7 - (x & 7))) & 1
            rgba[dst:dst + 4] = bytes((r, g, b, 0 if mask else 255))
            dst += 4
    return w, h, rgba


def load_named_icon(exe, name) -> tuple[int, int, bytearray] | None:
    """Resolve a GROUP_ICON by name (LoadIcon-style) and decode its best image."""
    grp = exe.lookup_resource("GROUP_ICON", name)
    if grp is None:
        return None
    entries = group_icon_entries(grp.data)
    if not entries:
        return None
    entries.sort(key=lambda e: (e[0] * e[1], e[2]))   # prefer larger/deeper
    _w, _h, _bpp, icon_id = entries[-1]
    icon = exe.lookup_resource("ICON", icon_id)
    if icon is None:
        return None
    return decode_icon(icon.data)
