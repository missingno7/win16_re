"""GetTopWindow / GetWindow / GetNextWindow z-order (win16/api/user.py).

SimAnt enumerates a parent's children with GetTopWindow + GetNextWindow (to
close/redraw them).  The window list is draw order (last = topmost), so the
top-to-bottom Z-order is the reverse — pinned here on a synthetic tree.
"""
from types import SimpleNamespace

from win16.api.objects import HandleTable, Window
from win16.api.user import _abs_origin, _get_window, _z_children

GW_HWNDFIRST, GW_HWNDLAST, GW_HWNDNEXT, GW_HWNDPREV, GW_CHILD = 0, 1, 2, 3, 5


def _win(handle, parent, x=0, y=0):
    # Window has many fields; only handle/parent/x/y matter for these helpers.
    w = Window.__new__(Window)
    w.handle = handle
    w.parent = parent
    w.x, w.y = x, y
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


def test_abs_origin_walks_the_parent_chain():
    # 276@(6,4) top-level; 280@(0,73) child; 318@(138,43) grandchild.
    # Screen origin of 318 = 6+0+138, 4+73+43 = (144, 120).  A flat (immediate
    # win.x/y only) mapping would wrongly give (138, 43) — the bug this fixes.
    handles = HandleTable()
    wins = []
    for h, p, x, y in [(276, 0, 6, 4), (280, 276, 0, 73), (318, 280, 138, 43)]:
        w = _win(h, p, x, y)
        handles._objects[h] = w
        wins.append(w)
    from types import SimpleNamespace
    sysobj = SimpleNamespace(windows=wins, handles=handles)
    assert _abs_origin(sysobj, handles.get(318)) == (144, 120)
    assert _abs_origin(sysobj, handles.get(280)) == (6, 77)
    assert _abs_origin(sysobj, handles.get(276)) == (6, 4)


def test_prev_and_child_and_bad_handle():
    sysobj = _sys_with([(10, 0), (20, 10), (21, 10), (22, 10)])
    assert _get_window(sysobj, 21, GW_HWNDPREV) == 22   # above 21
    assert _get_window(sysobj, 22, GW_HWNDPREV) == 0     # nothing above top
    assert _get_window(sysobj, 10, GW_CHILD) == 22       # top child of parent
    assert _get_window(sysobj, 999, GW_HWNDNEXT) == 0    # unknown handle


def _enumchild_ctx(sysobj, proc, lparam):
    from win16.api.core import ApiRegistry, CallContext
    from win16.api import user
    api = ApiRegistry()
    user.install(api)
    sysobj.yield_check = None
    sysobj.callback_max_steps = 20_000_000   # the system's callback policy
    api.services["system"] = sysobj
    ctx = CallContext(cpu=SimpleNamespace(), registry=api, module="USER",
                      ordinal=55, name="EnumChildWindows",
                      args=(10, proc, lparam))
    return api, ctx


def test_enumchildwindows_calls_back_each_child_top_to_bottom(monkeypatch):
    from win16 import callback as cb_mod
    sysobj = _sys_with([(10, 0), (20, 10), (21, 10), (22, 10)])
    api, ctx = _enumchild_ctx(sysobj, 0x00AB1234, 0xDEADBEEF)
    calls = []

    def fake_call_far(cpu, thunk_seg, seg, off, args, *, max_steps="MISSING",
                      yield_check=None):
        assert max_steps == sysobj.callback_max_steps    # the policy seam
        calls.append((seg, off, tuple(args)))
        return (1, 0)                                    # non-zero: continue
    monkeypatch.setattr(cb_mod, "call_far", fake_call_far)

    assert api.entries[("USER", 55)].handler(ctx) == 1
    # one callback per child, top-to-bottom (22, 21, 20); proc split seg:off;
    # lParam passed as (hi, lo) after the child handle.
    assert calls == [
        (0x00AB, 0x1234, (22, 0xDEAD, 0xBEEF)),
        (0x00AB, 0x1234, (21, 0xDEAD, 0xBEEF)),
        (0x00AB, 0x1234, (20, 0xDEAD, 0xBEEF)),
    ]


def test_enumchildwindows_stops_when_callback_returns_false(monkeypatch):
    from win16 import callback as cb_mod
    sysobj = _sys_with([(10, 0), (20, 10), (21, 10), (22, 10)])
    api, ctx = _enumchild_ctx(sysobj, 0x00AB1234, 0)
    seen = []

    def fake_call_far(cpu, thunk_seg, seg, off, args, *, max_steps="MISSING",
                      yield_check=None):
        seen.append(args[0])
        return (0, 0) if args[0] == 21 else (1, 0)       # stop at the 2nd child
    monkeypatch.setattr(cb_mod, "call_far", fake_call_far)

    assert api.entries[("USER", 55)].handler(ctx) == 1
    assert seen == [22, 21]                              # 20 never reached
