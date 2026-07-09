"""Child-window compositing (win16/compositor.py).

A top-level frame with child windows composites into one image; each child's
pixels land at its (x, y), clipped, recursively — without mutating any game
surface.  This is the model Win16 apps like SimAnt need (a child canvas inside
a top-level frame).
"""
from win16 import compositor
from win16.api.objects import Menu, MenuItem, Surface, Window, WndClass

WS_CHILD = 0x40000000
MF_POPUP = 0x0010


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


def test_menu_bar_drawn_above_client():
    frame = _win(1, "Frame", 0, 0, 120, 60, rgb=(0, 0, 0))
    frame.menu_obj = Menu(None, items=[
        MenuItem(flags=MF_POPUP, id=0x10, text="&File", submenu=Menu(None)),
        MenuItem(flags=MF_POPUP, id=0x11, text="&View", submenu=Menu(None)),
    ])
    out = compositor.composite(_Sys([frame]), frame)
    # The output grows by exactly the menu-bar height; client sits below it.
    assert out.h == 60 + compositor.MENU_BAR_H
    assert out.w == 120
    # Top strip is the menu-bar background; a title glyph paints black on it.
    assert _px(out, 0, 0) == compositor._MENU_BG
    ty = (compositor.MENU_BAR_H - 8) // 2
    strip = [_px(out, x, ty + r) for x in range(compositor._MENU_PAD_X,
             compositor._MENU_PAD_X + 4 * 8) for r in range(8)]
    assert compositor._MENU_FG in strip            # "File" rendered
    # The client content is shifted down by the bar, unmodified.
    assert _px(out, 0, compositor.MENU_BAR_H) == (0, 0, 0)
    # A childless / menuless frame is unchanged (no strip).
    plain = _win(2, "Plain", 0, 0, 40, 40, rgb=(9, 9, 9))
    assert compositor.composite(_Sys([plain]), plain).h == 40


def test_top_level_selection_excludes_children():
    frame = _win(1, "Frame", 0, 0, 40, 40, rgb=(1, 1, 1))
    child = _win(2, "Kid", 0, 0, 40, 40, parent=1, child=True)
    sysobj = _Sys([frame, child])
    tops = compositor.top_level_windows(sysobj)
    assert [w.handle for w in tops] == [1]


def test_captioned_child_gets_title_bar():
    frame = _win(1, "Frame", 0, 0, 240, 160, rgb=(160, 170, 150))
    panel = _win(2, "Panel", 20, 20, 180, 110, parent=1, child=True,
                 rgb=(192, 192, 192))
    panel.title = "Caste Control"
    panel.style |= compositor.WS_CAPTION | compositor.WS_SYSMENU
    out = compositor.composite(_Sys([frame, panel]), frame)

    # Caption bar: a deep-blue strip inside the child's top, past the sys box.
    bar = [_px(out, x, 20 + 6) for x in range(20 + compositor._CAP_H + 4,
                                              20 + 170)]
    assert bar.count(compositor._CAP_BG) > len(bar) // 2
    # White title glyphs sit on the bar.
    title_px = [_px(out, x, 20 + 4 + r)
                for x in range(20 + compositor._CAP_H + 4, 20 + 160)
                for r in range(8)]
    assert compositor._CAP_TEXT in title_px
    # A system box (grey) hugs the left edge inside the frame.
    assert _px(out, 20 + 5, 20 + 8) == compositor._BOX_BG
    # A plain WS_BORDER child (no full caption) gets no blue bar.
    plain = _win(3, "Plain", 20, 20, 180, 110, parent=1, child=True,
                 rgb=(200, 200, 200))
    plain.style |= compositor.WS_BORDER
    out2 = compositor.composite(_Sys([frame, plain]), frame)
    assert _px(out2, 20 + compositor._CAP_H + 6, 20 + 6) != compositor._CAP_BG
