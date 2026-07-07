"""DIB (device-independent bitmap) decoding — the format inside NE BITMAP
resources.  Decodes to a top-down RGB surface (3 bytes/pixel).  Only the
formats real executables have presented are implemented; anything else fails
loud."""
from __future__ import annotations

import struct


def decode_dib(data: bytes) -> tuple[int, int, bytearray]:
    """-> (width, height, rgb bytes, top-down, 3 bytes/pixel)."""
    (size, w, h, planes, bpp, comp) = struct.unpack_from("<IiiHHI", data, 0)
    if size != 40:
        raise ValueError(f"DIB: unsupported header size {size}")
    if planes != 1 or comp != 0:
        raise ValueError(f"DIB: unsupported planes={planes} compression={comp}")
    if h < 0:
        raise ValueError("DIB: top-down bitmaps not yet needed")
    clr_used = struct.unpack_from("<I", data, 32)[0]

    if bpp == 4:
        pal_count = clr_used or 16
    elif bpp == 8:
        pal_count = clr_used or 256
    elif bpp == 1:
        pal_count = clr_used or 2
    else:
        raise ValueError(f"DIB: unsupported bit depth {bpp}")

    palette = []
    off = 40
    for _ in range(pal_count):
        b, g, r, _res = struct.unpack_from("<BBBB", data, off)
        palette.append((r, g, b))
        off += 4

    row_bytes = ((w * bpp + 31) // 32) * 4
    rgb = bytearray(w * h * 3)
    for row in range(h):
        src = off + (h - 1 - row) * row_bytes    # bottom-up storage
        dst = row * w * 3
        for x in range(w):
            if bpp == 4:
                byte = data[src + (x >> 1)]
                idx = (byte >> 4) if (x & 1) == 0 else (byte & 0x0F)
            elif bpp == 8:
                idx = data[src + x]
            else:  # bpp == 1
                idx = (data[src + (x >> 3)] >> (7 - (x & 7))) & 1
            r, g, b = palette[idx]
            rgb[dst:dst + 3] = bytes((r, g, b))
            dst += 3
    return w, h, rgb
