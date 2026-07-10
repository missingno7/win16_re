"""The window update region is a real region (win16/api/user.py).

InvalidateRect unions rects in as a LIST; ValidateRgn SUBTRACTS (splitting
rects); update_rect mirrors the bounding box.  SimAnt's map scroll validates
the scroll-exposed strip out of the pending region between ScrollWindow and
UpdateWindow — the single-bbox approximation left the whole union pending and
every scroll stamped stale tiles over the scrolled pixels (16x16 ghosting).
"""
from types import SimpleNamespace

from win16.api.user import _invalidate, _validate, _validate_rect


def _win(w=423, h=346):
    return SimpleNamespace(client_size=(w, h), update_rect=None,
                           update_rects=[], update_erase=False, dirty=False)


def test_invalidate_accumulates_rects_and_bbox():
    win = _win()
    _invalidate(win, (0, 330, 423, 346))            # scroll-exposed strip
    _invalidate(win, (192, 128, 256, 144))          # an object's rect
    assert win.update_rects == [(0, 330, 423, 346), (192, 128, 256, 144)]
    assert win.update_rect == (0, 128, 423, 346)    # bbox only, not the region
    assert win.dirty


def test_validate_rect_subtracts_exactly():
    win = _win()
    _invalidate(win, (0, 330, 423, 346))
    _invalidate(win, (192, 128, 256, 144))
    _validate_rect(win, (0, 330, 423, 346))         # validate the strip
    assert win.update_rects == [(192, 128, 256, 144)]   # object rect survives
    assert win.update_rect == (192, 128, 256, 144)
    assert win.dirty


def test_validate_rect_splits_partial_overlap():
    win = _win()
    _invalidate(win, (0, 0, 100, 100))
    _validate_rect(win, (40, 40, 60, 60))           # punch a hole
    assert sorted(win.update_rects) == [
        (0, 0, 100, 40),        # above
        (0, 40, 40, 60),        # left
        (0, 60, 100, 100),      # below
        (60, 40, 100, 60),      # right
    ]
    assert win.update_rect == (0, 0, 100, 100)      # bbox unchanged
    assert win.dirty


def test_validate_rect_to_empty_clears_dirty():
    win = _win()
    _invalidate(win, (10, 10, 20, 20))
    _validate_rect(win, (0, 0, 50, 50))
    assert win.update_rects == []
    assert win.update_rect is None
    assert not win.dirty


def test_validate_clears_everything():
    win = _win()
    _invalidate(win, (10, 10, 20, 20), erase=True)
    _validate(win)
    assert win.update_rects == [] and win.update_rect is None
    assert not win.dirty and not win.update_erase
