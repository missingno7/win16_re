"""WM_PAINT is deliverable through PeekMessage on an INTERACTIVE host.

Real USER generates WM_PAINT at message-retrieval time (lowest priority) for
a window with a non-empty update region — through GetMessage AND PeekMessage.
A PeekMessage-only pump (SimAnt's title/scenario loops never call GetMessage)
otherwise never sees a paint: a host resize posts WM_SIZE, the game re-lays
its children out via SetWindowPos (invalidating them), and the windows then
sit dirty forever with their surfaces never repainted — the "window does not
refresh when resized" symptom, identical on the interpreted and the strict
lifted runners (not a lifter bug; a presentation-layer gap).

Headless/replay paths (interactive=False: demo replay, verify)
keep the old fall-through — their instruction-keyed baselines were recorded
without peek-time paints.
"""
from types import SimpleNamespace

from win16.api.system import Win16System

WM_PAINT, WM_TIMER = 0x000F, 0x0113


def _sys(*, interactive=True, windows=(), queue=()):
    ns = SimpleNamespace(
        demo_driver=None, input_drainer=None,
        interactive=interactive, clock_ms=1234,
        msg_queue=list(queue), windows=list(windows),
        timers={}, timer_due={}, timer_procs={},
        scheduled_messages=[],
        _note_input=lambda m: None,
    )
    ns._due_timer = Win16System._due_timer.__get__(ns)     # the real one
    ns._release_due_messages = Win16System._release_due_messages.__get__(ns)
    return ns


def _win(handle=0x114, visible=True, dirty=True):
    return SimpleNamespace(handle=handle, visible=visible, dirty=dirty)


def peek(sysobj, hwnd=0, lo=0, hi=0, remove=True):
    return Win16System.peek_message(sysobj, hwnd, lo, hi, remove)


def test_interactive_any_scan_returns_paint_for_dirty_window():
    sysobj = _sys(windows=[_win()])
    m = peek(sysobj)
    assert m is not None and m[0] == 0x114 and m[1] == WM_PAINT


def test_paint_repeats_until_validated_then_stops():
    # WM_PAINT is not consumed by retrieval: it is regenerated while the
    # update region is non-empty (BeginPaint/ValidateRect clear it).
    win = _win()
    sysobj = _sys(windows=[win])
    assert peek(sysobj)[1] == WM_PAINT
    assert peek(sysobj)[1] == WM_PAINT
    win.dirty = False                       # BeginPaint validated
    assert peek(sysobj) is None


def test_posted_message_has_priority_over_paint():
    posted = (0x114, 0x0005, 0, 0, 0, 0)    # WM_SIZE ahead of the paint
    sysobj = _sys(windows=[_win()], queue=[posted])
    assert peek(sysobj) == posted


def test_filter_must_admit_wm_paint():
    sysobj = _sys(windows=[_win()])
    assert peek(sysobj, lo=WM_TIMER, hi=WM_TIMER) is None   # sim-tick spin
    assert peek(sysobj, lo=0x0001, hi=WM_PAINT)[1] == WM_PAINT


def test_hwnd_filter_selects_the_dirty_window():
    sysobj = _sys(windows=[_win(handle=0x114), _win(handle=0x118)])
    assert peek(sysobj, hwnd=0x118)[0] == 0x118
    sysobj.windows[1].dirty = False
    assert peek(sysobj, hwnd=0x118) is None


def test_hidden_or_clean_windows_never_paint():
    sysobj = _sys(windows=[_win(visible=False), _win(dirty=False)])
    assert peek(sysobj) is None


def test_headless_replay_path_unchanged():
    # interactive=False (demo replay, verify): NO peek-time paint —
    # the recorded instruction-keyed baselines depend on this.
    sysobj = _sys(interactive=False, windows=[_win()])
    assert peek(sysobj) is None
