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
