"""Deferred posts on the virtual clock (Win16System.schedule_message).

A device whose work finishes after a length of TIME (audio output being the
first) notifies the program with a message that must arrive at a computed
GUEST instant — never when a host thread happens to get around to it.  These
tests pin the retrieval contract: nothing arrives early, everything arrives
through the ordinary queue, and the 32-bit tick wrap is handled.
"""
from collections import deque
from types import SimpleNamespace

import pytest

from win16.api.system import Win16System, _tick_reached

WM_TIMER = 0x0113
MSG = 0x03BD            # MM_WOM_DONE, the first real user of this


def _sys(now=1000, **kw):
    ns = SimpleNamespace(
        demo_driver=None, input_drainer=None,
        interactive=True, clock_ms=now,
        msg_queue=deque(), windows=[], timers={}, timer_due={}, timer_procs={},
        scheduled_messages=[], quit_posted=None,
        _note_input=lambda m: None,
        tick_count=lambda: ns.clock_ms & 0xFFFFFFFF,
        **kw)
    for name in ("_release_due_messages", "cancel_scheduled_messages",
                 "schedule_message", "post_message", "_due_timer"):
        setattr(ns, name, getattr(Win16System, name).__get__(ns))
    return ns


def peek(sysobj, hwnd=0, lo=0, hi=0, remove=True):
    return Win16System.peek_message(sysobj, hwnd, lo, hi, remove)


# -- the arrival instant ------------------------------------------------------
def test_nothing_is_delivered_before_its_due_time():
    s = _sys(now=1000)
    s.schedule_message(1500, 0x40, MSG, 1, 0xABCD)
    assert peek(s, 0, MSG, MSG) is None
    s.clock_ms = 1499
    assert peek(s, 0, MSG, MSG) is None


def test_it_arrives_once_the_clock_reaches_the_due_time():
    s = _sys(now=1000)
    s.schedule_message(1500, 0x40, MSG, 7, 0xABCD)
    s.clock_ms = 1500
    m = peek(s, 0, MSG, MSG)
    assert m is not None
    assert (m[0], m[1], m[2], m[3]) == (0x40, MSG, 7, 0xABCD)


def test_a_delivered_message_is_a_normal_queued_message():
    """Released into msg_queue — so every existing filter, removal and
    ordering rule applies to it with no special casing."""
    s = _sys(now=2000)
    s.schedule_message(1000, 0x40, MSG, 1, 2)       # already due
    assert peek(s, 0, 0x0001, 0x0002) is None       # filtered out by range
    assert peek(s, 0x99, MSG, MSG) is None          # filtered out by hwnd
    assert len(s.msg_queue) == 1                    # ...but it IS queued
    assert peek(s, 0x40, MSG, MSG, True) is not None
    assert list(s.msg_queue) == []                  # PM_REMOVE consumed it


def test_due_messages_are_released_in_due_order():
    s = _sys(now=0)
    for due in (300, 100, 200):
        s.schedule_message(due, 0x40, MSG, due, 0)
    s.clock_ms = 1000
    s._release_due_messages()
    assert [m[2] for m in s.msg_queue] == [100, 200, 300]


def test_a_peek_only_pump_drains_completions_and_then_stops():
    """The shape a polling program relies on: PeekMessage(MSG, MSG, PM_REMOVE)
    in a loop consumes each completion and then reports none — the loop ends."""
    s = _sys(now=0)
    for i in range(3):
        s.schedule_message(10 * i, 0x40, MSG, i, 0)
    s.clock_ms = 100
    drained = []
    while (m := peek(s, 0, MSG, MSG, True)) is not None:
        drained.append(m[2])
        assert len(drained) <= 3, "the drain loop did not terminate"
    assert drained == [0, 1, 2]


# -- cancellation -------------------------------------------------------------
def test_cancel_drops_pending_and_only_the_named_sender():
    s = _sys(now=0)
    s.schedule_message(100, 0x40, MSG, 1, 0)
    s.schedule_message(100, 0x40, MSG, 2, 0)
    s.schedule_message(100, 0x40, WM_TIMER, 1, 0)
    assert s.cancel_scheduled_messages(0x40, MSG, wparam=1) == 1
    assert [(x[2], x[3]) for x in s.scheduled_messages] == [(MSG, 2), (WM_TIMER, 1)]
    assert s.cancel_scheduled_messages(0x40, MSG) == 1       # any sender
    assert [x[2] for x in s.scheduled_messages] == [WM_TIMER]


def test_a_cancelled_message_never_arrives():
    s = _sys(now=0)
    s.schedule_message(100, 0x40, MSG, 1, 0)
    s.cancel_scheduled_messages(0x40, MSG)
    s.clock_ms = 10_000
    assert peek(s, 0, MSG, MSG) is None


# -- GetMessage blocks for it -------------------------------------------------
def test_get_message_runs_the_clock_forward_to_a_pending_completion():
    """An idle pump with a completion still in the future must BLOCK for it,
    the same way it blocks for an armed timer — not raise "no messages"."""
    s = _sys(now=1000)
    s.schedule_message(4000, 0x40, MSG, 1, 2)
    m = Win16System.next_message(s)
    assert m is not None and m[1] == MSG
    assert s.clock_ms == 4000               # the clock advanced to the arrival


def test_an_idle_pump_with_nothing_pending_still_raises():
    s = _sys(now=1000)
    with pytest.raises(RuntimeError, match="empty queue"):
        Win16System.next_message(s)


# -- the 32-bit tick wrap -----------------------------------------------------
def test_tick_reached_handles_the_32_bit_wrap():
    assert _tick_reached(1000, 999)
    assert _tick_reached(1000, 1000)
    assert not _tick_reached(1000, 1001)
    # due computed just past the wrap: now has wrapped, due has not
    assert not _tick_reached(0xFFFF_FF00, 0x0000_0050)
    assert _tick_reached(0x0000_0050, 0xFFFF_FF00)
    # ...and a due time far ahead is still "not reached" across the wrap
    assert not _tick_reached(0xFFFF_FFF0, 0x0000_1000)


def test_a_completion_scheduled_across_the_wrap_still_arrives():
    s = _sys(now=0xFFFF_FF00)
    s.schedule_message((0xFFFF_FF00 + 0x200) & 0xFFFFFFFF, 0x40, MSG, 1, 2)
    assert peek(s, 0, MSG, MSG) is None         # not yet
    s.clock_ms = 0x0000_0100                    # the clock wrapped past it
    assert peek(s, 0, MSG, MSG) is not None
