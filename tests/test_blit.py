"""blit() ROP + overlap semantics (win16/api/objects.py).

A Win16 app scrolls a window/bitmap by BitBlt-ing it onto itself shifted.  When
source and destination are the same surface and the rects overlap, the copy must
behave like memmove, not a naive top-to-bottom memcpy — otherwise a downward
scroll reads rows it has already overwritten and smears them into vertical
trails (the "ghosting" seen scrolling SimAnt's map view).
"""
from win16.api.objects import Surface, blit

SRCCOPY = 0x00CC0020


def _col(surf, x):
    """The column of R-channel bytes at x (one per row)."""
    return [surf.pixels[(y * surf.w + x) * 3] for y in range(surf.h)]


def test_overlapping_self_blit_scroll_down_no_smear():
    # A 1-wide, 10-tall surface; row y has R = y (10,11,... to stay distinct).
    surf = Surface(1, 10)
    for y in range(10):
        surf.pixels[(y * 1 + 0) * 3] = 10 + y
    # Scroll the top 7 rows down by 3 (dst rows 3..9 <- src rows 0..6).
    blit(surf, 0, 3, surf, 0, 0, 1, 7, SRCCOPY)
    # Correct memmove result: rows 0..2 untouched, rows 3..9 == old 0..6.
    assert _col(surf, 0) == [10, 11, 12, 10, 11, 12, 13, 14, 15, 16]


def test_overlapping_self_blit_scroll_up_no_smear():
    surf = Surface(1, 10)
    for y in range(10):
        surf.pixels[(y * 1 + 0) * 3] = 10 + y
    # Scroll bottom 7 rows up by 3 (dst rows 0..6 <- src rows 3..9).
    blit(surf, 0, 0, surf, 0, 3, 1, 7, SRCCOPY)
    assert _col(surf, 0) == [13, 14, 15, 16, 17, 18, 19, 17, 18, 19]


def test_distinct_surface_blit_still_copies():
    src = Surface(2, 2)
    for i in range(2 * 2 * 3):
        src.pixels[i] = i + 1
    dst = Surface(2, 2)
    blit(dst, 0, 0, src, 0, 0, 2, 2, SRCCOPY)
    assert bytes(dst.pixels) == bytes(src.pixels)
