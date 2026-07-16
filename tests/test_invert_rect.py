"""InvertRect's 16-colour device-domain inversion (win16/api/gdi.py:
invert_rect_16color, used by USER.82 InvertRect).

On the original 4-bit planar display the inversion-class raster ops invert
the PHYSICAL palette index (idx ^ 0xF), not RGB channels.  The bug this
pins: a rubber-band rectangle over a grey (128,128,128) background — a
per-channel RGB invert produced (127,127,127), visually identical, making
the drag rectangle vanish (SimAnt's nest/map-view cursor drag; OTVDM shows
the same washout, so real 16-colour behaviour, not OTVDM, is the reference).
The real device showed light grey (192,192,192): grey is physical index 8,
NOT 8 = 7 = light grey.
"""
import numpy as np

from win16.api.gdi import DEVICE_PALETTE_16, invert_rect_16color
from win16.api.objects import Surface


def _px(s: Surface, x: int, y: int) -> tuple:
    o = (y * s.w + x) * 3
    return tuple(s.pixels[o:o + 3])


def _filled(rgb, w=8, h=8) -> Surface:
    s = Surface(w, h)
    s.fill(rgb)
    return s


def test_grey_inverts_to_visibly_different_light_grey():
    s = _filled((128, 128, 128))
    invert_rect_16color(s, 2, 2, 6, 6)
    got = _px(s, 3, 3)
    assert got == (192, 192, 192)               # the device-domain result
    # the regression guard itself: visibly different from the background
    assert max(abs(a - b) for a, b in zip(got, (128, 128, 128))) >= 32


def test_black_and_white_still_swap():
    s = _filled((0, 0, 0))
    invert_rect_16color(s, 0, 0, 8, 8)
    assert _px(s, 4, 4) == (255, 255, 255)
    invert_rect_16color(s, 0, 0, 8, 8)
    assert _px(s, 4, 4) == (0, 0, 0)


def test_every_device_colour_maps_to_its_complement_pair():
    for i, rgb in enumerate(DEVICE_PALETTE_16):
        s = _filled(rgb)
        invert_rect_16color(s, 0, 0, 8, 8)
        assert _px(s, 0, 0) == DEVICE_PALETTE_16[i ^ 0xF], rgb


def test_double_invert_is_identity_on_device_colours():
    # The rubber-band contract: draw + erase (two InvertRects over the same
    # rect) must restore the destination exactly.
    s = Surface(4, 4)
    for i in range(16):                          # one pixel per device colour
        o = i * 3
        s.pixels[o:o + 3] = bytes(DEVICE_PALETTE_16[i])
    before = bytes(s.pixels)
    invert_rect_16color(s, 0, 0, 4, 4)
    assert bytes(s.pixels) != before             # actually drew something
    invert_rect_16color(s, 0, 0, 4, 4)
    assert bytes(s.pixels) == before


def test_non_device_colour_snaps_then_toggles_stably():
    # A colour the 4-bit device could never hold nearest-matches into the
    # palette; after the first draw/erase pair it settles on that device
    # colour and further toggles are exact.
    s = _filled((130, 130, 130))
    invert_rect_16color(s, 0, 0, 8, 8)
    assert _px(s, 0, 0) == (192, 192, 192)       # nearest = grey(8), NOT -> 7
    invert_rect_16color(s, 0, 0, 8, 8)
    assert _px(s, 0, 0) == (128, 128, 128)       # snapped to the device grey
    invert_rect_16color(s, 0, 0, 8, 8)
    assert _px(s, 0, 0) == (192, 192, 192)       # stable from here on


def test_pixels_outside_rect_untouched_and_rect_clipped():
    s = _filled((128, 128, 128))
    invert_rect_16color(s, 4, 4, 100, 100)       # spills past the surface
    arr = np.frombuffer(bytes(s.pixels), dtype=np.uint8).reshape(8, 8, 3)
    assert (arr[4:, 4:] == 192).all()            # inside (clipped) inverted
    assert (arr[:4, :] == 128).all()             # outside untouched
    assert (arr[:, :4] == 128).all()


def test_degenerate_or_offsurface_rect_is_a_noop():
    s = _filled((128, 128, 128))
    v = s.version
    invert_rect_16color(s, 5, 5, 5, 9)           # empty width
    invert_rect_16color(s, -8, -8, -1, -1)       # fully off-surface
    assert _px(s, 0, 0) == (128, 128, 128)
    assert s.version == v                        # not even touched
