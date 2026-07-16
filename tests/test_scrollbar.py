"""win16.scrollbar — the Win16 scroll notification contract.

Every test here fails on the pre-fix behaviour it pins: the Win32-style wParam
packing that silently loses the thumb position on a 16-bit wParam, and the
position-sized thumb that vanishes at the end of its range.
"""
import pytest

from win16.scrollbar import (DEFAULT_THUMB_FRACTION, SB_ENDSCROLL, SB_HORZ,
                             SB_LINEDOWN, SB_LINEUP, SB_PAGEDOWN, SB_PAGEUP,
                             SB_THUMBPOSITION, SB_THUMBTRACK, SB_VERT,
                             WM_HSCROLL, WM_VSCROLL, ScrollTracker,
                             scroll_message, thumb_fraction, thumb_metrics,
                             thumb_position)

# ---------------------------------------------------------------------------
# the message shape (Win16, not Win32)
# ---------------------------------------------------------------------------


def test_the_bar_selects_the_message():
    assert scroll_message(SB_VERT, SB_LINEUP)[0] == WM_VSCROLL
    assert scroll_message(SB_HORZ, SB_LINEUP)[0] == WM_HSCROLL


def test_the_code_goes_in_wparam_and_fits_a_16_bit_word():
    for code in range(0, 9):
        msg, wparam, lparam = scroll_message(SB_VERT, code, pos=1234)
        assert wparam == code
        assert wparam == wparam & 0xFFFF


def test_the_thumb_position_rides_in_the_low_word_of_lparam():
    # The bug this pins: packing pos into HIWORD(wParam) (the Win32 shape) is
    # dropped by the 16-bit wParam, and the owner reads LOWORD(lParam) == 0.
    msg, wparam, lparam = scroll_message(SB_VERT, SB_THUMBPOSITION, pos=21)
    assert (msg, wparam) == (WM_VSCROLL, SB_THUMBPOSITION)
    assert lparam & 0xFFFF == 21
    assert wparam >> 16 == 0, "the position must not be packed into wParam"


def test_thumbtrack_carries_its_position_too():
    assert scroll_message(SB_VERT, SB_THUMBTRACK, pos=7)[2] & 0xFFFF == 7


@pytest.mark.parametrize("code", [SB_LINEUP, SB_LINEDOWN, SB_PAGEUP,
                                  SB_PAGEDOWN, SB_ENDSCROLL])
def test_non_thumb_codes_carry_no_position(code):
    # Real USER leaves the position half zero for these; an owner reads
    # LOWORD(lParam) regardless of the code, so a stale value would be acted on.
    assert scroll_message(SB_VERT, code, pos=999)[2] & 0xFFFF == 0


def test_hwnd_ctl_rides_in_the_high_word_and_is_zero_for_window_bars():
    assert scroll_message(SB_VERT, SB_THUMBPOSITION, 5)[2] >> 16 == 0
    assert scroll_message(SB_VERT, SB_THUMBPOSITION, 5, hwnd_ctl=0x1234)[2] >> 16 == 0x1234


# ---------------------------------------------------------------------------
# thumb geometry: Win 3.x's FIXED-size box
# ---------------------------------------------------------------------------


def test_the_thumb_keeps_its_size_across_the_whole_range():
    lo, hi, size = 0, 42, 0.15
    for pos in range(lo, hi + 1):
        first, last = thumb_metrics(lo, hi, pos, size)
        assert last - first == pytest.approx(size), f"thumb resized at pos={pos}"


def test_the_thumb_does_not_vanish_at_the_end_of_the_range():
    # The live in-game state that provoked the report: vert bar (0, 42, 42).
    # Sizing the box from the position gives first == last == 1.0 -> no thumb.
    first, last = thumb_metrics(0, 42, 42)
    assert last - first == pytest.approx(DEFAULT_THUMB_FRACTION)
    assert last == pytest.approx(1.0)
    assert first < last, "the thumb must still be grabbable at pos == max"


def test_the_thumb_parks_at_both_ends():
    size = 0.2
    assert thumb_metrics(0, 42, 0, size) == pytest.approx((0.0, 0.2))
    assert thumb_metrics(0, 42, 42, size) == pytest.approx((0.8, 1.0))
    mid = thumb_metrics(0, 42, 21, size)
    assert mid[0] == pytest.approx(0.4)      # 0.5 of the 0.8 travel


def test_an_empty_range_fills_the_trough():
    # Nothing to scroll: USER shows a full-length thumb, not a vanished one.
    assert thumb_metrics(0, 0, 0) == (0.0, 1.0)
    assert thumb_metrics(5, 5, 5) == (0.0, 1.0)
    assert thumb_metrics(10, 3, 5) == (0.0, 1.0)


def test_position_is_clamped_into_the_range():
    size = 0.1
    assert thumb_metrics(0, 10, -5, size)[0] == pytest.approx(0.0)
    assert thumb_metrics(0, 10, 99, size)[1] == pytest.approx(1.0)


def test_thumb_position_inverts_thumb_metrics_exactly():
    lo, hi, size = 0, 42, 0.15
    for pos in range(lo, hi + 1):
        first, _ = thumb_metrics(lo, hi, pos, size)
        assert thumb_position(lo, hi, first, size) == pos


def test_a_thumb_dropped_at_the_extremes_reaches_them():
    # Travel, not the trough, carries the range: dropping the thumb at the far
    # end must mean `hi`, not hi minus the thumb's own size.
    assert thumb_position(0, 42, 1.0, 0.15) == 42
    assert thumb_position(0, 42, 0.0, 0.15) == 0
    assert thumb_position(0, 42, 0.425, 0.15) == 21     # 0.5 of the 0.85 travel


def test_thumb_position_survives_a_degenerate_range():
    assert thumb_position(0, 0, 0.5) == 0
    assert thumb_position(7, 7, 1.0) == 7


def test_thumb_fraction_is_the_box_over_the_trough_with_a_usable_floor():
    assert thumb_fraction(200, 20) == pytest.approx(0.1)
    assert thumb_fraction(10_000, 16) >= 0.04       # never a hairline
    assert thumb_fraction(0, 16) == DEFAULT_THUMB_FRACTION


# ---------------------------------------------------------------------------
# the notification SEQUENCE (order is part of the contract)
# ---------------------------------------------------------------------------


def codes(msgs):
    return [w for (_m, w, _l) in msgs]


def positions(msgs):
    return [lp & 0xFFFF for (_m, _w, lp) in msgs]


def test_arrow_click_is_a_line_notification_then_endscroll():
    t = ScrollTracker(SB_VERT)
    assert codes(t.line(+1)) == [SB_LINEDOWN]
    assert codes(t.line(-1)) == [SB_LINEUP]
    assert codes(t.end()) == [SB_ENDSCROLL]


def test_arrow_auto_repeat_streams_lines_and_ends_once():
    # Auto-repeat is the host's clock: one `line` per repeat, ONE SB_ENDSCROLL
    # when the button finally comes up.
    t = ScrollTracker(SB_VERT)
    out = []
    for _ in range(4):                       # initial delay + 3 repeats
        out += t.line(+1)
    out += t.end()
    assert codes(out) == [SB_LINEDOWN] * 4 + [SB_ENDSCROLL]


def test_trough_click_is_a_page_notification_then_endscroll():
    t = ScrollTracker(SB_VERT)
    assert codes(t.page(-1)) == [SB_PAGEUP]
    assert codes(t.page(+1)) == [SB_PAGEDOWN]
    assert codes(t.end()) == [SB_ENDSCROLL]


def test_a_full_thumb_drag_tracks_then_positions_then_ends():
    t = ScrollTracker(SB_VERT)
    out = t.begin_drag(0)
    assert out == [], "the grab itself notifies nothing"
    for pos in (5, 11, 20):
        out += t.drag_to(pos)
    out += t.end_drag()
    assert codes(out) == [SB_THUMBTRACK, SB_THUMBTRACK, SB_THUMBTRACK,
                          SB_THUMBPOSITION, SB_ENDSCROLL]
    assert positions(out) == [5, 11, 20, 20, 0]


def test_the_track_stream_carries_the_live_position():
    t = ScrollTracker(SB_HORZ)
    t.begin_drag(0)
    msg, wparam, lparam = t.drag_to(13)[0]
    assert (msg, wparam) == (WM_HSCROLL, SB_THUMBTRACK)
    assert lparam & 0xFFFF == 13


def test_a_thumb_that_does_not_move_notifies_nothing():
    t = ScrollTracker(SB_VERT)
    t.begin_drag(7)
    assert t.drag_to(7) == []
    assert codes(t.drag_to(8)) == [SB_THUMBTRACK]
    assert t.drag_to(8) == []


def test_end_drag_can_be_told_the_final_position():
    t = ScrollTracker(SB_VERT)
    t.begin_drag(0)
    t.drag_to(9)
    out = t.end_drag(12)
    assert codes(out) == [SB_THUMBPOSITION, SB_ENDSCROLL]
    assert positions(out) == [12, 0]


def test_a_cancelled_drag_reports_the_origin_then_ends():
    t = ScrollTracker(SB_VERT)
    t.begin_drag(30)
    t.drag_to(4)                             # the owner followed us down to 4...
    out = t.cancel_drag()                    # ...ESC: snap back
    assert codes(out) == [SB_THUMBPOSITION, SB_ENDSCROLL]
    assert positions(out) == [30, 0], "the owner must be left at the origin"
    assert not t.dragging


def test_cancel_without_a_drag_just_ends_an_open_interaction():
    t = ScrollTracker(SB_VERT)
    t.page(+1)
    assert codes(t.cancel_drag()) == [SB_ENDSCROLL]


def test_a_move_with_no_grab_is_a_position_not_a_track():
    # A toolkit that only reports "the thumb is now here" (no press/release),
    # a wheel, a keyboard jump: that is a completed move.
    t = ScrollTracker(SB_VERT)
    out = t.drag_to(17)
    assert codes(out) == [SB_THUMBPOSITION, SB_ENDSCROLL]
    assert positions(out) == [17, 0]


def test_endscroll_is_sent_once_per_interaction():
    t = ScrollTracker(SB_VERT)
    assert t.end() == [], "no interaction open => nothing owed"
    t.line(+1)
    assert codes(t.end()) == [SB_ENDSCROLL]
    assert t.end() == [], "a second button-up must not double the ENDSCROLL"


def test_a_drag_ends_with_exactly_one_endscroll():
    t = ScrollTracker(SB_VERT)
    t.begin_drag(0)
    t.drag_to(3)
    out = t.end_drag()
    assert codes(out).count(SB_ENDSCROLL) == 1
    assert t.end() == []


def test_the_tracker_uses_its_bar_for_every_message():
    t = ScrollTracker(SB_HORZ)
    t.begin_drag(0)
    stream = t.line(+1) + t.page(+1) + t.drag_to(4) + t.end_drag()
    assert {m for (m, _w, _l) in stream} == {WM_HSCROLL}


def test_a_control_tracker_names_its_control_in_every_message():
    t = ScrollTracker(SB_VERT, hwnd_ctl=0x0A0B)
    stream = t.line(+1) + t.end()
    assert all((lp >> 16) == 0x0A0B for (_m, _w, lp) in stream)
