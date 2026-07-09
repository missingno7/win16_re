"""Recovered SimAnt rendering primitives — VM-free, byte-exact.

Reconstructed from the shipped code (names from SIMANTW.SYM), verified against
the original ASM by the A/B oracle in simant/tests/test_hooks.py.
"""
from __future__ import annotations


def windows_make_table_4x4(tiles, table):
    """Expand a row of terrain tiles into a 4-scanline pixel band.

    Each source byte is a colour index; `table[row][tile]` is the 16-bit fill
    word (four packed 4bpp pixels) that colour draws as on scanline `row`.  The
    band is four scanlines tall, each `len(tiles)` words wide, and every column
    is the same tile's word repeated down the four rows.

    Returns four rows, each a list of `len(tiles)` words.

    Recovered from `_Windows_MakeTable4x4` (SIMANTW.SYM, seg4:4674): the ASM
    loops per column doing one `lodsb` (the tile) then four `stosw`, each row's
    word read from a 4x32-word table at `SS:0x1A56` with a 0x40-byte row stride
    (`ss:[0x1A56 + row*0x40 + tile*2]`).
    """
    return [[table[row][tile] for tile in tiles] for row in range(4)]


def windows_make_table_1x1(tiles, table):
    """Pack pairs of terrain tiles into 4bpp pixel bytes, 1:1 (no zoom).

    For each consecutive pair `(t0, t1)` the output byte is
    `table[t0] | table[0x10 + t1]` — the even tile contributes the high nibble
    (its `table` entry), the odd tile the low nibble (from the +0x10 half of the
    table).  Returns `len(tiles) // 2` bytes (a trailing odd tile is dropped, as
    the ASM's `count >> 1` loop count does).

    `table` is the 256+-byte XLAT table at `SS:0x1B56`.  Recovered from
    `_Windows_MakeTable1x1` (SIMANTW.SYM, seg4:46BB): per iteration two `lodsb`
    + two `ss:xlat` (the second with BX bumped by 0x10) OR'd into one `stosb`.
    """
    return bytes(table[tiles[2 * i]] | table[0x10 + tiles[2 * i + 1]]
                 for i in range(len(tiles) // 2))
