"""The BeginPaint/EndPaint clip-restore (win16/api/user.py: _apply_paint_clip).

EndPaint restores the pixels OUTSIDE the update region from a pre-paint
snapshot, so an unclipped wndproc can't leak outside the region it invalidated
(the fix for SimAnt's 16x16 tile-ghosting).  But a wndproc may RESIZE the client
surface between BeginPaint and EndPaint — then the snapshot of the old shape no
longer maps onto the surface and the numpy reshape used to crash
(`cannot reshape array of size N into shape (h,w,3)`).  The restore must no-op on
that mismatch, not crash.
"""
import numpy as np

from win16.api.user import _apply_paint_clip


class _FakeSurface:
    def __init__(self, w, h, fill=0):
        self.w, self.h = w, h
        self.pixels = bytearray([fill]) * (w * h * 3)
        self.touched = 0

    def touch(self):
        self.touched += 1


def test_clip_restores_outside_region():
    # surface fully repainted to 0xFF; snapshot (before) was all 0x11
    s = _FakeSurface(8, 4, fill=0xFF)
    before = bytes([0x11]) * (8 * 4 * 3)
    _apply_paint_clip(s, [(2, 1, 5, 3)], before)          # keep this box painted
    out = np.frombuffer(bytes(s.pixels), dtype=np.uint8).reshape(4, 8, 3)
    assert (out[1:3, 2:5] == 0xFF).all()                  # inside region: painted
    mask = np.ones((4, 8), bool)
    mask[1:3, 2:5] = False
    assert (out[mask] == 0x11).all()                      # outside: restored
    assert s.touched == 1


def test_clip_noop_on_mismatched_snapshot_size():
    # the wndproc resized 8x4 -> 8x3 mid-paint; the old snapshot is 8x4-sized
    s = _FakeSurface(8, 3, fill=0xFF)
    stale_before = bytes([0x11]) * (8 * 4 * 3)            # wrong shape (bigger)
    _apply_paint_clip(s, [(0, 0, 8, 3)], stale_before)    # must NOT raise
    out = np.frombuffer(bytes(s.pixels), dtype=np.uint8).reshape(3, 8, 3)
    assert (out == 0xFF).all()                            # surface left as painted
    assert s.touched == 0                                 # restore skipped


def test_clip_noop_on_smaller_stale_snapshot():
    s = _FakeSurface(8, 4, fill=0xFF)
    stale_before = bytes([0x11]) * (8 * 3 * 3)            # wrong shape (smaller)
    _apply_paint_clip(s, [(0, 0, 8, 4)], stale_before)    # must NOT raise
    assert s.touched == 0
