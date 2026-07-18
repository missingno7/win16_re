"""`Surface.version` must advance only AFTER the pixels of that version exist.

`Surface.version` is the change-detect key a host uses to decide when to
re-read a surface ("increments on every mutation — hosts use it to redraw only
when pixels actually changed").  That contract is an ORDERING contract, not
just a counting one: a bump published before the pixels are written tells a
reader "a new frame is ready" while the buffer is still half-written.

It matters because a real host reads surfaces CONCURRENTLY with the code that
draws them (play.py runs the CPU on a worker thread and composites on the GUI
thread).  A drawing primitive here is a Python loop over rows, so the reader
can interleave anywhere inside it.  If the bump came first, the reader copies a
half-drawn buffer, sees an UNCHANGED version around the copy, concludes the
frame is coherent — and, because no further bump follows, keeps showing that
torn frame until something unrelated repaints.  That is persistent ghosting:
stale pixels left behind, not a transient flicker.

The invariant tested: for any drawing primitive, the pixel state captured at
the LAST `touch()` equals the pixel state when the primitive returns.  Nothing
may be written after the version that describes it has been published.
"""
import pytest

from win16.api.gdi import _fill_polygon, _fill_rect, invert_rect_16color
from win16.api.objects import Surface, blit


class _OrderProbe:
    """Snapshots the pixels at every `touch()` so the last published version
    can be compared with what the primitive finally left behind."""

    def __init__(self, *surfaces):
        self.surfaces = surfaces
        self._orig = {}
        self.last = {}

    def __enter__(self):
        probe = self
        for s in self.surfaces:
            orig = s.touch

            def touch(_s=s, _orig=orig):
                _orig()
                probe.last[id(_s)] = bytes(_s.pixels)

            self._orig[id(s)] = orig
            s.touch = touch
        return self

    def __exit__(self, *exc):
        for s in self.surfaces:
            s.touch = self._orig[id(s)]
        return False

    def check(self, surface):
        published = self.last.get(id(surface))
        assert published is not None, "primitive never bumped the version"
        assert published == bytes(surface.pixels), (
            "pixels were written AFTER the version that describes them was "
            "published - a concurrent reader can latch a half-drawn frame")


def _surf(w, h, fill=0):
    return Surface(w, h, bytearray([fill]) * (w * h * 3))


def test_fill_rect_publishes_after_writing():
    s = _surf(40, 30)
    with _OrderProbe(s) as p:
        _fill_rect(s, 2, 3, 30, 20, (255, 0, 0))
    p.check(s)


def test_fill_polygon_publishes_after_writing():
    s = _surf(40, 30)
    with _OrderProbe(s) as p:
        _fill_polygon(s, [(2, 2), (35, 4), (30, 25), (4, 20)], (0, 200, 0))
    p.check(s)


@pytest.mark.parametrize("rop, name", [
    (0x00CC0020, "SRCCOPY"),
    (0x008800C6, "SRCAND"),
    (0x00EE0086, "SRCPAINT"),
    (0x00660046, "SRCINVERT"),
    (0x00330008, "NOTSRCCOPY"),
])
def test_blit_publishes_after_writing(rop, name):
    dst = _surf(40, 30, 0x11)
    src = _surf(40, 30, 0xA5)
    with _OrderProbe(dst) as p:
        blit(dst, 0, 0, src, 0, 0, 40, 30, rop)
    p.check(dst)


def test_self_overlapping_blit_publishes_after_writing():
    """The map scroll: a window BitBlt-ing itself shifted.  The longest write
    loop in the layer and the one a scroll ghost was reported on."""
    s = _surf(64, 48)
    for y in range(48):                          # distinguishable rows
        o = y * 64 * 3
        s.pixels[o:o + 64 * 3] = bytes([y & 0xFF]) * (64 * 3)
    with _OrderProbe(s) as p:
        blit(s, 0, 0, s, 0, 16, 64, 32, 0x00CC0020)
    p.check(s)


def test_invert_rect_publishes_after_writing():
    """A control: this primitive already published last.  It must stay that
    way, and it proves the probe passes on correct code."""
    s = _surf(24, 24, 0x80)
    with _OrderProbe(s) as p:
        invert_rect_16color(s, 2, 2, 20, 20)
    p.check(s)


def test_surface_fill_publishes_after_writing():
    s = _surf(16, 16)
    with _OrderProbe(s) as p:
        s.fill((1, 2, 3))
    p.check(s)
