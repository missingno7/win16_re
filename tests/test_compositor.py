"""Child-window compositing (win16/compositor.py).

A top-level frame with child windows composites into one image; each child's
pixels land at its (x, y), clipped, recursively — without mutating any game
surface.  This is the model Win16 apps like SimAnt need (a child canvas inside
a top-level frame).
"""
from win16 import compositor
from win16.api.objects import Surface, Window, WndClass

WS_CHILD = 0x40000000


class _Sys:
    """Minimal stand-in for Win16System: just a window list."""
    def __init__(self, windows):
        self.windows = windows


def _win(handle, name, x, y, w, h, parent=0, child=False, rgb=(0, 0, 0)):
    cls = WndClass(name=name, style=0, wndproc=(0, 0), cls_extra=0, wnd_extra=0,
                   h_instance=0, h_icon=0, h_cursor=0, h_background=0,
                   menu_name=None)
    win = Window(wndclass=cls, title="", style=(WS_CHILD if child else 0),
                 x=x, y=y, w=w, h=h, parent=parent, menu=0, visible=True)
    win._surface = Surface(w, h)
    win._surface.fill(rgb)
    win.handle = handle
    return win


def _px(surf, x, y):
    o = (y * surf.w + x) * 3
    return tuple(surf.pixels[o:o + 3])


def test_child_composites_into_parent_at_offset():
    frame = _win(1, "Frame", 0, 0, 100, 100, rgb=(10, 10, 10))
    child = _win(2, "Canvas", 20, 30, 40, 40, parent=1, child=True,
                 rgb=(200, 100, 50))
    sysobj = _Sys([frame, child])

    out = compositor.composite(sysobj, frame)
    # Outside the child: the frame's own colour.
    assert _px(out, 0, 0) == (10, 10, 10)
    assert _px(out, 19, 30) == (10, 10, 10)
    # Inside the child rect [20,60) x [30,70): the child's colour.
    assert _px(out, 20, 30) == (200, 100, 50)
    assert _px(out, 59, 69) == (200, 100, 50)
    assert _px(out, 60, 70) == (10, 10, 10)
    # The game surfaces are untouched (composite works on a copy).
    assert _px(frame.surface, 20, 30) == (10, 10, 10)


def test_nested_children_and_clipping():
    frame = _win(1, "Frame", 0, 0, 80, 80, rgb=(0, 0, 0))
    canvas = _win(2, "Canvas", 10, 10, 60, 60, parent=1, child=True,
                  rgb=(0, 128, 0))
    # A grandchild that partly overhangs the canvas (must clip).
    inner = _win(3, "Inner", 40, 40, 40, 40, parent=2, child=True,
                 rgb=(0, 0, 255))
    sysobj = _Sys([frame, canvas, inner])

    out = compositor.composite(sysobj, frame)
    assert _px(out, 5, 5) == (0, 0, 0)          # frame
    assert _px(out, 15, 15) == (0, 128, 0)      # canvas
    # inner at canvas-local (40,40) -> frame (50,50); clipped to canvas edge 70.
    assert _px(out, 55, 55) == (0, 0, 255)
    assert _px(out, 69, 69) == (0, 0, 255)


def test_top_level_selection_excludes_children():
    frame = _win(1, "Frame", 0, 0, 40, 40, rgb=(1, 1, 1))
    child = _win(2, "Kid", 0, 0, 40, 40, parent=1, child=True)
    sysobj = _Sys([frame, child])
    tops = compositor.top_level_windows(sysobj)
    assert [w.handle for w in tops] == [1]
