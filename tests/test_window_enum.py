"""GetTopWindow / GetWindow / GetNextWindow z-order (win16/api/user.py).

SimAnt enumerates a parent's children with GetTopWindow + GetNextWindow (to
close/redraw them).  The window list is draw order (last = topmost), so the
top-to-bottom Z-order is the reverse — pinned here on a synthetic tree.
"""
from types import SimpleNamespace

from win16.api.objects import HandleTable, Window
from win16.api.user import _get_window, _z_children

GW_HWNDFIRST, GW_HWNDLAST, GW_HWNDNEXT, GW_HWNDPREV, GW_CHILD = 0, 1, 2, 3, 5


def _win(handle, parent):
    # Window has many fields; only handle/parent matter for enumeration.
    w = Window.__new__(Window)
    w.handle = handle
    w.parent = parent
    return w


def _sys_with(children_order):
    """A stub system whose .windows is the given creation/draw order."""
    handles = HandleTable()
    wins = []
    for h, p in children_order:
        w = _win(h, p)
        handles._objects[h] = w
        wins.append(w)
    return SimpleNamespace(windows=wins, handles=handles)


def test_top_to_bottom_is_reversed_draw_order():
    # Parent 10 with children created 20, 21, 22 (22 drawn last = topmost).
    sysobj = _sys_with([(10, 0), (20, 10), (21, 10), (22, 10)])
    kids = _z_children(sysobj, 10)
    assert [w.handle for w in kids] == [22, 21, 20]     # top-to-bottom


def test_getwindow_enumeration_walks_all_children_once():
    sysobj = _sys_with([(10, 0), (20, 10), (21, 10), (22, 10)])
    # GetTopWindow == GW_HWNDFIRST sibling of any child.
    assert _get_window(sysobj, 22, GW_HWNDFIRST) == 22
    assert _get_window(sysobj, 20, GW_HWNDLAST) == 20
    # Walk top-to-bottom with GW_HWNDNEXT, terminating at 0.
    order, hw = [], 22
    while hw:
        order.append(hw)
        hw = _get_window(sysobj, hw, GW_HWNDNEXT)
    assert order == [22, 21, 20]


def test_prev_and_child_and_bad_handle():
    sysobj = _sys_with([(10, 0), (20, 10), (21, 10), (22, 10)])
    assert _get_window(sysobj, 21, GW_HWNDPREV) == 22   # above 21
    assert _get_window(sysobj, 22, GW_HWNDPREV) == 0     # nothing above top
    assert _get_window(sysobj, 10, GW_CHILD) == 22       # top child of parent
    assert _get_window(sysobj, 999, GW_HWNDNEXT) == 0    # unknown handle
