"""Child-window compositing for presentation.

A Win16 app draws into a TREE of windows: a top-level frame plus WS_CHILD
children (toolbars, canvases, MDI-style client areas) positioned inside the
parent's client area.  SimAnt, for example, is a top-level ``AntRoot`` frame
containing a ``RibbonWindow`` toolbar and a child ``AntRoot`` canvas that the
game actually renders into.

Each window keeps its OWN surface — per-window byte-exact verification is
untouched by this module.  `composite()` walks the tree and blits each visible
child onto a COPY of the parent at the child's (x, y), recursively, clipped to
the parent bounds, producing one image for display.  This is a presentation
helper only (play.py, screenshots); it never mutates a game surface.
"""
from __future__ import annotations

WS_CHILD = 0x40000000
WS_BORDER = 0x00800000
WS_DLGFRAME = 0x00400000
WS_CAPTION = WS_BORDER | WS_DLGFRAME     # 0x00C00000 (both bits => title bar)
WS_SYSMENU = 0x00080000
WS_MAXIMIZEBOX = 0x00010000
WS_MINIMIZEBOX = 0x00020000
_FRAME_STYLES = WS_BORDER | WS_DLGFRAME  # any of these => draw a window frame

# Win 3.1 3D frame colours.
_FRAME_HI = (255, 255, 255)     # light edge (top-left)
_FRAME_LO = (128, 128, 128)     # shadow edge (bottom-right)
_FRAME_DK = (0, 0, 0)           # outer line

# Win 3.1 caption bar (a composited child's title bar — a top-level window gets
# its caption from the host window manager, so only WS_CHILD framed windows draw
# one here).  Classic look: deep-blue active bar, white title, a grey system box
# at the left and (when the style asks) grey min/max boxes at the right.  Drawn
# as an overlay on the top of the child's own rect — we don't inset the client,
# so it covers the child's outermost caption-height strip.
_CAP_H = 15
_CAP_BG = (0, 0, 128)           # active title bar
_CAP_TEXT = (255, 255, 255)
_BOX_BG = (192, 192, 192)       # system / min / max box face
_BOX_MARK = (0, 0, 0)           # the glyph inside a box

# --- menu bar (presentation) -----------------------------------------------
# Our Window surface IS the client area (no non-client modelling), so a top
# level frame's menu bar is drawn as an 18px strip ABOVE the client in the
# composite — the faithful placement, and it never disturbs the game's own
# coordinate system.  Classic Win3.1 look: light-grey bar, black titles, a
# darker shadow line beneath.  Only real frames (menu_obj set, not a child)
# get one; the game already stored the exact popup titles via AppendMenu.
MENU_BAR_H = 18
_MENU_BG = (192, 192, 192)
_MENU_FG = (0, 0, 0)
_MENU_SHADOW = (128, 128, 128)
_MENU_PAD_X = 8            # left margin before the first title
_MENU_GAP = 12            # gap between one title and the next


def _draw_menu_text(dst, x: int, y: int, text: str) -> None:
    from .font8x8 import glyph_rows
    h, w = dst.shape[0], dst.shape[1]
    for i, ch in enumerate(text):
        cx = x + i * 8
        for ry, rowbits in enumerate(glyph_rows(ord(ch))):
            py = y + ry
            if not 0 <= py < h:
                continue
            for rx in range(8):
                if rowbits & (1 << rx):
                    px = cx + rx
                    if 0 <= px < w:
                        dst[py, px] = _MENU_FG


def _with_menu_bar(content, menu):
    """Return a NEW Surface: `content` with an 18px menu bar drawn on top,
    showing `menu`'s top-level popup titles (the '&' accelerator marker is
    stripped for display)."""
    import numpy as np

    from .api.objects import Surface

    titles = [(it.text or "").replace("&", "") for it in menu.items]
    mh = MENU_BAR_H
    out = Surface(content.w, content.h + mh)
    dst = np.frombuffer(out.pixels, dtype=np.uint8).reshape(content.h + mh,
                                                            content.w, 3)
    dst[0:mh] = _MENU_BG
    dst[mh - 1] = _MENU_SHADOW                       # 3D shadow line under the bar
    src = np.frombuffer(content.pixels, dtype=np.uint8).reshape(content.h,
                                                               content.w, 3)
    dst[mh:mh + content.h] = src
    ty = (mh - 8) // 2
    x = _MENU_PAD_X
    for title in titles:
        _draw_menu_text(dst, x, ty, title)
        x += len(title) * 8 + _MENU_GAP
    return out


def child_windows(sysobj, parent_handle: int) -> list:
    """Visible direct children of a window, in Z-order (creation order)."""
    return [w for w in sysobj.windows
            if w.parent == parent_handle and w.visible]


def is_child(window) -> bool:
    return bool(window.style & WS_CHILD)


def top_level_windows(sysobj) -> list:
    """Visible windows parented to the desktop (parent == 0) — genuine
    top-level frames.  Windows parented to another window composite INTO it.
    The desktop pseudo-window is never presented."""
    return [w for w in sysobj.windows
            if w.visible and w.parent == 0 and w.wndclass.name != "#desktop"]


def presents_standalone(window) -> bool:
    """True for a composited child the host can PROMOTE to its own real OS
    window: a WS_CHILD with a full caption bar (both WS_CAPTION bits).  SimAnt's
    in-game panels ("Caste Control", "Behavior Control", "Black Nest View") are
    such children of the main frame.  A host that presents them as their own
    Toplevel (native title bar + close box) skips them in the parent composite
    (see `standalone` in composite()); headless/screenshot rendering leaves them
    composited-in with a painted caption instead."""
    return (bool(window.style & WS_CHILD)
            and (window.style & WS_CAPTION) == WS_CAPTION)


def own_windows(sysobj) -> list:
    """Every window the host presents as its OWN OS window: genuine top-level
    frames (parent == 0) PLUS captioned children promoted to real windows."""
    return [w for w in sysobj.windows
            if w.visible and w.wndclass.name != "#desktop"
            and (w.parent == 0 or presents_standalone(w))]


def tree_version(sysobj, window) -> int:
    """Sum of surface PRESENT versions over `window` and its visible
    descendants — the frame-boundary change-detect key a host redraws on.  Uses
    ``present_version`` (advances only at a complete frame) rather than the
    per-primitive ``version``, so the host never presents the half-built
    intermediates of a multi-step BeginPaint..EndPaint (the nest-view blink)."""
    total = window.surface.present_version
    for child in child_windows(sysobj, window.handle):
        total += tree_version(sysobj, child)
    return total


def _draw_frame(dst, x: int, y: int, w: int, h: int) -> None:
    """Paint a Win3.1 raised 3D window frame on `dst` (an H×W×3 numpy view) for
    the window rect at (x, y, w, h): an outer dark line, then a raised bevel
    (white top-left, grey bottom-right).  Clipped to dst; a no-op if degenerate."""
    H, W = dst.shape[0], dst.shape[1]
    x0, y0, x1, y1 = x, y, x + w, y + h            # right/bottom are exclusive
    if x1 - x0 < 3 or y1 - y0 < 3:
        return

    def hline(yy, xa, xb, rgb):
        if 0 <= yy < H:
            a, b = max(xa, 0), min(xb, W)
            if b > a:
                dst[yy, a:b] = rgb

    def vline(xx, ya, yb, rgb):
        if 0 <= xx < W:
            a, b = max(ya, 0), min(yb, H)
            if b > a:
                dst[a:b, xx] = rgb

    # outer 1px black rectangle
    hline(y0, x0, x1, _FRAME_DK); hline(y1 - 1, x0, x1, _FRAME_DK)
    vline(x0, y0, y1, _FRAME_DK); vline(x1 - 1, y0, y1, _FRAME_DK)
    # inner 1px raised bevel
    hline(y0 + 1, x0 + 1, x1 - 1, _FRAME_HI); vline(x0 + 1, y0 + 1, y1 - 1, _FRAME_HI)
    hline(y1 - 2, x0 + 1, x1 - 1, _FRAME_LO); vline(x1 - 2, y0 + 1, y1 - 1, _FRAME_LO)


def _fill(dst, x0, y0, x1, y1, rgb) -> None:
    H, W = dst.shape[0], dst.shape[1]
    a, b = max(x0, 0), min(x1, W)
    c, d = max(y0, 0), min(y1, H)
    if b > a and d > c:
        dst[c:d, a:b] = rgb


def _draw_box(dst, x, y, size, glyph) -> None:
    """A grey caption box (system / min / max) with a bevel and a black `glyph`
    ('sys' horizontal bar, 'min' down-triangle, 'max' up-triangle)."""
    _fill(dst, x, y, x + size, y + size, _BOX_BG)
    # simple 1px bevel so the box reads as a raised button
    _fill(dst, x, y, x + size, y + 1, _FRAME_HI)
    _fill(dst, x, y, x + 1, y + size, _FRAME_HI)
    _fill(dst, x, y + size - 1, x + size, y + size, _FRAME_LO)
    _fill(dst, x + size - 1, y, x + size, y + size, _FRAME_LO)
    cx, cy = x + size // 2, y + size // 2
    if glyph == "sys":                       # Win3.1 system-menu box: a bar
        _fill(dst, x + 3, cy - 1, x + size - 3, cy + 1, _BOX_MARK)
    elif glyph in ("min", "max"):            # a small solid triangle
        H, W = dst.shape[0], dst.shape[1]
        for i in range(4):
            row = cy + (i - 2 if glyph == "max" else 2 - i)
            if 0 <= row < H:
                a, b = max(cx - i, 0), min(cx + i + 1, W)
                if b > a:
                    dst[row, a:b] = _BOX_MARK


def _draw_caption(dst, x, y, w, h, title, style) -> None:
    """Paint a title bar on the top strip of the child rect (x, y, w, h)."""
    from .font8x8 import glyph_rows
    H, W = dst.shape[0], dst.shape[1]
    if w < 3 * _CAP_H or h < _CAP_H + 4:
        return                               # too small to carry a caption
    bx0, by0 = x + 2, y + 2                   # inside the 2px frame
    bx1 = x + w - 2
    _fill(dst, bx0, by0, bx1, by0 + _CAP_H, _CAP_BG)
    left = bx0
    if style & WS_SYSMENU:                    # system box hugs the left edge
        _draw_box(dst, bx0 + 1, by0 + 1, _CAP_H - 2, "sys")
        left = bx0 + _CAP_H + 2
    right = bx1
    for bit, glyph in ((WS_MAXIMIZEBOX, "max"), (WS_MINIMIZEBOX, "min")):
        if style & bit:
            right -= _CAP_H
            _draw_box(dst, right, by0 + 1, _CAP_H - 2, glyph)
    if title:                                 # centred-ish white title text
        ty = by0 + (_CAP_H - 8) // 2
        tx = left + 2
        for i, ch in enumerate(title):
            cx = tx + i * 8
            if cx + 8 > right:
                break
            for ry, rowbits in enumerate(glyph_rows(ord(ch))):
                py = ty + ry
                if not 0 <= py < H:
                    continue
                for rx in range(8):
                    if rowbits & (1 << rx):
                        px = cx + rx
                        if 0 <= px < W:
                            dst[py, px] = _CAP_TEXT


def composite(sysobj, window, *, menu_bar: bool = True, standalone=()):
    """A NEW Surface: `window`'s pixels with its visible child windows blitted
    in at their positions (recursively), clipped to the window's client area.

    `menu_bar` paints the top-level frame's menu titles as a strip above the
    client — right for headless screenshots, but a host with a REAL menu widget
    (play.py's native tkinter menubar) passes menu_bar=False so the strip does
    not double the menu and offset the client.

    `standalone` is a set of handles the host is presenting as their OWN OS
    window (see own_windows()); any child in it is SKIPPED here — its real
    native chrome replaces the painted caption, and it is drawn by its own view.
    Applied at every depth so a promoted panel nested under a plain child window
    (e.g. SimAnt's body panel) is skipped too."""
    import numpy as np

    from .api.objects import Surface

    base = window.surface
    out = Surface(base.w, base.h, bytearray(base.pixels))
    dst = np.frombuffer(out.pixels, dtype=np.uint8).reshape(base.h, base.w, 3)

    for child in child_windows(sysobj, window.handle):
        if child.handle in standalone:          # promoted to its own OS window
            continue
        sub = composite(sysobj, child, standalone=standalone)   # grandkids first
        x0, y0 = max(child.x, 0), max(child.y, 0)
        x1 = min(child.x + sub.w, base.w)
        y1 = min(child.y + sub.h, base.h)
        if x1 <= x0 or y1 <= y0:
            continue
        src = np.frombuffer(sub.pixels, dtype=np.uint8).reshape(sub.h, sub.w, 3)
        dst[y0:y1, x0:x1] = src[y0 - child.y:y1 - child.y,
                                x0 - child.x:x1 - child.x]
        # A framed child (WS_DLGFRAME/WS_BORDER/WS_CAPTION) gets a window frame so
        # it reads as a real window, not a flat rectangle painted on the parent.
        # Drawn as an inset 3D edge on the child's own rect (we don't model
        # non-client insets, so it overlays the outermost 2px of the client).
        if child.style & _FRAME_STYLES:
            _draw_frame(dst, child.x, child.y, sub.w, sub.h)
            if (child.style & WS_CAPTION) == WS_CAPTION:
                _draw_caption(dst, child.x, child.y, sub.w, sub.h,
                              child.title, child.style)

    # A top-level frame's menu bar is a presentation strip above the client.
    if not menu_bar:
        return out
    menu = getattr(window, "menu_obj", None)
    if menu is not None and menu.items and not is_child(window):
        out = _with_menu_bar(out, menu)
    return out
