"""COLORREF resolution (win16/api/gdi.py).

A Win16 COLORREF's high byte selects its type: 0x00 literal RGB, 0x02
PALETTERGB (nearest match), 0x01 PALETTEINDEX(i) -> the i-th palette entry.
SimAnt fills its meter bars with CreateSolidBrush(PALETTEINDEX(8)); resolving
that against the DC palette (light grey) instead of masking it to RGB (8,0,0)
≈ black is what makes the bars show up.
"""
from win16.api.gdi import brush_object_rgb, colorref_rgb
from win16.api.objects import Brush

PAL = [(i * 3, i * 3, i * 3) for i in range(16)]
PAL[8] = (192, 192, 192)


def test_literal_rgb_is_bgr_low_bytes():
    # 0x00BBGGRR
    assert colorref_rgb(0x00FF8040) == (0x40, 0x80, 0xFF)
    assert colorref_rgb(0x00000000) == (0, 0, 0)


def test_palettergb_uses_same_low_bytes():
    assert colorref_rgb(0x02FF8040, PAL) == (0x40, 0x80, 0xFF)


def test_paletteindex_resolves_against_palette():
    assert colorref_rgb(0x01000008, PAL) == (192, 192, 192)
    assert colorref_rgb(0x0100000A, PAL) == PAL[10]


def test_paletteindex_out_of_range_or_no_palette_is_black():
    assert colorref_rgb(0x01000008, None) == (0, 0, 0)
    assert colorref_rgb(0x010000FF, PAL) == (0, 0, 0)


def test_solid_brush_keeps_flag_and_resolves():
    # The bug: CreateSolidBrush stored 0x01000008 & 0xFFFFFF = 8 -> (8,0,0).
    # With the flag preserved and a palette, it is light grey.
    brush = Brush(0x01000008)
    assert brush_object_rgb(brush, PAL) == (192, 192, 192)
    # A plain RGB brush is palette-independent.
    assert brush_object_rgb(Brush(0x00204060), PAL) == (0x60, 0x40, 0x20)
