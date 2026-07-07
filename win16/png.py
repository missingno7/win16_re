"""Minimal PNG writer for Surface evidence dumps.  Stdlib only."""
from __future__ import annotations

import struct
import zlib


def write_png(path, w: int, h: int, rgb: bytes) -> None:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload +
                struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))
    data = (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw, 6)) +
            chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(data)
