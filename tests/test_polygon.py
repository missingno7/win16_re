"""GDI.36 Polygon fill + outline (win16/api/gdi.py).

SimAnt's _TrapFill draws filled trapezoids (nest cross-sections / terrain) via
Polygon; the fill is an even-odd scanline fill, the edges a 1px pen outline.
Tests the pure helpers on a Surface (no machine, no assets).
"""
from win16.api.gdi import _draw_line, _fill_polygon
from win16.api.objects import Surface


def _surf(w, h):
    return Surface(w, h, bytearray(w * h * 3))


def _px(s, x, y):
    o = (y * s.w + x) * 3
    return tuple(s.pixels[o:o + 3])


def test_fill_rectangle_polygon():
    s = _surf(12, 10)
    _fill_polygon(s, [(2, 2), (9, 2), (9, 7), (2, 7)], (255, 0, 0))
    assert _px(s, 5, 4) == (255, 0, 0)          # interior filled
    assert _px(s, 2, 4) == (255, 0, 0)          # left edge column (>= ceil(1.5))
    assert _px(s, 0, 0) == (0, 0, 0)            # far outside untouched
    assert _px(s, 5, 9) == (0, 0, 0)            # below the polygon untouched
    assert s.version > 0


def test_fill_triangle_is_bounded_and_tapers():
    s = _surf(20, 20)
    _fill_polygon(s, [(10, 2), (2, 17), (18, 17)], (0, 200, 0))
    # near the apex only a couple of pixels are inside; near the base many are.
    top = sum(_px(s, x, 4) == (0, 200, 0) for x in range(20))
    bot = sum(_px(s, x, 15) == (0, 200, 0) for x in range(20))
    assert 0 < top < bot                        # tapered fill, not a full rect


def test_fill_clips_to_surface_no_crash():
    s = _surf(8, 8)
    # a polygon that pokes outside every edge must clip, not raise/overrun
    _fill_polygon(s, [(-5, -5), (20, -3), (18, 20), (-4, 15)], (1, 2, 3))
    assert _px(s, 4, 4) == (1, 2, 3)            # covered interior filled


def test_draw_line_horizontal_and_clipped():
    s = _surf(10, 10)
    _draw_line(s, 1, 5, 8, 5, (9, 9, 9))
    assert all(_px(s, x, 5) == (9, 9, 9) for x in range(1, 9))
    assert _px(s, 0, 5) == (0, 0, 0)
    _draw_line(s, -3, 2, 3, 2, (7, 7, 7))       # partly off-surface: clip, no crash
    assert _px(s, 0, 2) == (7, 7, 7)


def test_null_fill_is_noop():
    s = _surf(6, 6)
    _fill_polygon(s, [(1, 1), (4, 1), (4, 4)], None)   # NULL brush -> nothing
    assert all(b == 0 for b in s.pixels)
