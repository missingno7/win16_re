"""The Win16 scroll-bar protocol: WM_H/VSCROLL notifications and Win 3.x thumb
geometry.  Pure, stdlib-only — no VM, no toolkit, no game knowledge.

A window with WS_H/VSCROLL has scroll bars in its NON-CLIENT area, driven by
SetScrollRange/SetScrollPos and read back by GetScrollRange/GetScrollPos
(win16.api.user keeps that state on ``Window.scroll[bar] = (min, max, pos)``).
Real USER owns the bars themselves: it hit-tests the pointer, moves the thumb,
and notifies the OWNER window.  This module is the half of that contract a host
cannot guess:

**The message shape.**  Win16 is *not* Win32 here, and the difference is
silent rather than loud::

    Win16:  wParam        = SB_* code            (a 16-bit WORD)
            LOWORD(lParam) = position            (SB_THUMB* only)
            HIWORD(lParam) = hwndCtl             (0 for a window scroll bar)

    Win32:  wParam        = MAKEWPARAM(code, position)   <- 32-bit wParam
            lParam        = hwndCtl

Packing the position Win32-style into HIWORD(wParam) loses it entirely on a
16-bit wParam, and the owner then reads LOWORD(lParam) == 0 — i.e. every thumb
drag reads as "go to the very top".  It fails as a *plausible* value, never as
an error, which is why the packing lives here once rather than at each caller.

**The thumb geometry.**  Win 3.x has no page size: SetScrollRange/SetScrollPos
carry only (min, max, pos), so the thumb is a FIXED-SIZE box that slides along
the trough — proportional thumbs arrived with Win95's SetScrollInfo(nPage).
The thumb therefore never shrinks and never vanishes, not even at either end of
the range; it is the travel, not the box, that the position selects.

**The notification sequence.**  Order is part of the contract — a Win3.x app
may act on the stream, and every interaction is terminated by SB_ENDSCROLL::

    arrow  : SB_LINEUP/SB_LINEDOWN     (auto-repeat while held) ... SB_ENDSCROLL
    trough : SB_PAGEUP/SB_PAGEDOWN     (auto-repeat while held) ... SB_ENDSCROLL
    thumb  : SB_THUMBTRACK (live pos, streamed) ... SB_THUMBPOSITION ... SB_ENDSCROLL

An owner that ignores a code simply falls through its switch — SB_ENDSCROLL in
particular is very commonly ignored, and sending it is still correct.
"""
from __future__ import annotations

WM_HSCROLL = 0x0114
WM_VSCROLL = 0x0115

# Which bar of a window (the SetScrollRange/SetScrollPos `nBar` argument).
SB_HORZ = 0
SB_VERT = 1

# Notification codes (wParam).  The horizontal and vertical names are aliases:
# the bar is identified by the MESSAGE, not by the code.
SB_LINEUP = SB_LINELEFT = 0
SB_LINEDOWN = SB_LINERIGHT = 1
SB_PAGEUP = SB_PAGELEFT = 2
SB_PAGEDOWN = SB_PAGERIGHT = 3
SB_THUMBPOSITION = 4
SB_THUMBTRACK = 5
SB_TOP = SB_LEFT = 6
SB_BOTTOM = SB_RIGHT = 7
SB_ENDSCROLL = 8

#: Codes that carry a position in LOWORD(lParam).  For every other code real
#: USER leaves lParam's position half zero, so an owner reading it regardless
#: (they do) sees 0 rather than a stale value.
THUMB_CODES = (SB_THUMBPOSITION, SB_THUMBTRACK)

#: The fixed thumb, as a fraction of the trough.  Win 3.x sizes the box to the
#: bar's own breadth (a square thumb, an arrow-button wide), so as a fraction it
#: depends on the trough's length in pixels — `thumb_fraction` derives it where
#: the pixel geometry is known, and this is the fallback for a host that only
#: speaks fractions.
DEFAULT_THUMB_FRACTION = 0.10

#: A thumb thinner than this is unusable as a drag target; real USER keeps the
#: box a fixed size for exactly this reason, and a host whose trough is very
#: long should not end up with a hairline either.
MIN_THUMB_FRACTION = 0.04


def _clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else (hi if value > hi else value)


def scroll_message(bar: int, code: int, pos: int = 0,
                   hwnd_ctl: int = 0) -> tuple[int, int, int]:
    """One scroll notification as ``(msg, wParam, lParam)``, packed for Win16.

    `bar` selects the message (SB_VERT -> WM_VSCROLL, SB_HORZ -> WM_HSCROLL);
    `pos` is carried only by the SB_THUMB* codes and ignored otherwise, exactly
    as real USER does.  `hwnd_ctl` is 0 for a window scroll bar (the non-client
    kind) and the control's handle for a SCROLLBAR-class child.
    """
    msg = WM_VSCROLL if bar == SB_VERT else WM_HSCROLL
    if code not in THUMB_CODES:
        pos = 0
    lparam = ((hwnd_ctl & 0xFFFF) << 16) | (pos & 0xFFFF)
    return msg, code & 0xFFFF, lparam


def thumb_fraction(trough_px: int, thumb_px: int) -> float:
    """The fixed thumb's size as a fraction of the trough it slides in."""
    if trough_px <= 0:
        return DEFAULT_THUMB_FRACTION
    return _clamp(thumb_px / trough_px, MIN_THUMB_FRACTION, 1.0)


def thumb_metrics(lo: int, hi: int, pos: int,
                  size: float = DEFAULT_THUMB_FRACTION) -> tuple[float, float]:
    """Where the FIXED-size thumb sits: ``(first, last)`` trough fractions.

    The box keeps its size at every position and the travel absorbs the range::

        pos == lo  ->  (0, size)          thumb parked at the start
        pos == hi  ->  (1 - size, 1)      thumb parked at the end, SAME size

    An empty range (hi <= lo — nothing to scroll) gives ``(0.0, 1.0)``: the
    thumb fills the trough, which is how USER shows a bar with nowhere to go.

    Sizing the thumb from the position instead (``last = first + size`` with
    `first` taken straight from the range fraction, then clamped to 1.0) makes
    the box shrink as it approaches the end and vanish exactly at `hi` — no
    thumb left to grab, which is the bug this function exists to not have.
    """
    span = hi - lo
    if span <= 0:
        return 0.0, 1.0
    size = _clamp(size, MIN_THUMB_FRACTION, 1.0)
    travel = 1.0 - size
    first = _clamp((pos - lo) / span, 0.0, 1.0) * travel
    return first, first + size


def thumb_position(lo: int, hi: int, first: float,
                   size: float = DEFAULT_THUMB_FRACTION) -> int:
    """The scroll position a thumb dragged to trough fraction `first` means.

    The exact inverse of `thumb_metrics`, through the same travel — so a thumb
    dropped where `thumb_metrics` would draw it round-trips to the position it
    was drawn for, and dropping it at either extreme reaches `lo`/`hi` rather
    than falling short by the thumb's own size.
    """
    span = hi - lo
    if span <= 0:
        return lo
    size = _clamp(size, MIN_THUMB_FRACTION, 1.0)
    travel = 1.0 - size
    if travel <= 0:
        return lo
    frac = _clamp(first / travel, 0.0, 1.0)
    return lo + int(round(frac * span))


class ScrollTracker:
    """The interaction -> notification-sequence state machine for one bar.

    A host drives this from its own pointer events (or from a toolkit scroll
    bar's callbacks) and posts what comes back, in order.  Every method returns
    a list of ready-to-post ``(msg, wParam, lParam)`` tuples — possibly empty.

    It is deliberately ignorant of pixels and of when a repeat is due: auto-
    repeat is the *host's* clock (call `line`/`page` once per repeat), and the
    thumb's pixel hit box is the host's geometry.  What lives here is the part
    the owner window observes: which code, carrying which position, in which
    order, and terminated by SB_ENDSCROLL exactly once per interaction.
    """

    def __init__(self, bar: int, hwnd_ctl: int = 0) -> None:
        self.bar = bar
        self.hwnd_ctl = hwnd_ctl
        self.dragging = False
        self.drag_pos: int | None = None    # last position streamed, if dragging
        self.origin_pos: int | None = None  # where the drag started (for cancel)
        self._active = False                # an interaction is open => owes ENDSCROLL

    def _msg(self, code: int, pos: int = 0) -> tuple[int, int, int]:
        return scroll_message(self.bar, code, pos, self.hwnd_ctl)

    # -- discrete interactions (the host repeats these on its own clock) ------
    def line(self, delta: int) -> list[tuple[int, int, int]]:
        """One arrow-button notification: `delta` < 0 is up/left."""
        self._active = True
        return [self._msg(SB_LINEUP if delta < 0 else SB_LINEDOWN)]

    def page(self, delta: int) -> list[tuple[int, int, int]]:
        """One trough (channel) notification: `delta` < 0 is up/left."""
        self._active = True
        return [self._msg(SB_PAGEUP if delta < 0 else SB_PAGEDOWN)]

    def to_end(self, delta: int) -> list[tuple[int, int, int]]:
        """Jump to an extreme (SB_TOP/SB_BOTTOM) — the keyboard's Home/End."""
        self._active = True
        return [self._msg(SB_TOP if delta < 0 else SB_BOTTOM)]

    # -- the thumb -----------------------------------------------------------
    def begin_drag(self, pos: int) -> list[tuple[int, int, int]]:
        """The thumb was grabbed at `pos`.  Real USER notifies nothing yet — the
        stream starts with the first movement — but the origin is remembered so
        the drag can be cancelled back to it."""
        self.dragging = True
        self._active = True
        self.origin_pos = pos
        self.drag_pos = pos
        return []

    def drag_to(self, pos: int) -> list[tuple[int, int, int]]:
        """The thumb moved to `pos`: stream SB_THUMBTRACK.

        Repeats at the same position are dropped — real USER notifies on thumb
        MOVEMENT, and an owner that redraws its view per track message should
        not be asked to do it for a mouse that jiggled in place.
        """
        if not self.dragging:
            # No grab was reported (a toolkit that only says "the thumb is now
            # here", a wheel, a keyboard jump): that is a completed move, not a
            # track — SB_THUMBPOSITION is what an owner expects to act on.
            return self.jump_to(pos)
        if pos == self.drag_pos:
            return []
        self.drag_pos = pos
        return [self._msg(SB_THUMBTRACK, pos)]

    def jump_to(self, pos: int) -> list[tuple[int, int, int]]:
        """A completed move to `pos` with no drag in progress."""
        self._active = True
        return self.end_drag(pos)

    def end_drag(self, pos: int | None = None) -> list[tuple[int, int, int]]:
        """The button came up: SB_THUMBPOSITION at the final position, then
        SB_ENDSCROLL.  `pos` defaults to the last position streamed."""
        if pos is None:
            pos = self.drag_pos
        out: list[tuple[int, int, int]] = []
        if pos is not None:
            out.append(self._msg(SB_THUMBPOSITION, pos))
        self.dragging = False
        self.drag_pos = None
        self.origin_pos = None
        out += self.end()
        return out

    def cancel_drag(self) -> list[tuple[int, int, int]]:
        """The drag was abandoned (ESC, or the pointer strayed out of the bar's
        tracking margin): the thumb snaps back, and the owner — which has been
        following the SB_THUMBTRACK stream — is told where it really ended with
        SB_THUMBPOSITION at the ORIGIN, then SB_ENDSCROLL."""
        if not self.dragging:
            return self.end()
        origin = self.origin_pos
        self.dragging = False
        self.drag_pos = None
        self.origin_pos = None
        out = [] if origin is None else [self._msg(SB_THUMBPOSITION, origin)]
        out += self.end()
        return out

    # -- termination ---------------------------------------------------------
    def end(self) -> list[tuple[int, int, int]]:
        """Close the interaction with SB_ENDSCROLL — once, and only if one is
        open, so a host that calls this on every button-up cannot double it."""
        if not self._active:
            return []
        self._active = False
        return [self._msg(SB_ENDSCROLL)]
