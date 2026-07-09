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
WS_CAPTION = WS_BORDER | WS_DLGFRAME     # 0x00C00000
_FRAME_STYLES = WS_BORDER | WS_DLGFRAME  # any of these => draw a window frame

# Win 3.1 3D frame colours.
_FRAME_HI = (255, 255, 255)     # light edge (top-left)
_FRAME_LO = (128, 128, 128)     # shadow edge (bottom-right)
_FRAME_DK = (0, 0, 0)           # outer line

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
    """Visible windows the host should present as their OWN OS window: those
    parented to the desktop (parent == 0).  This includes SimAnt's in-game
    control panels ("Caste Control", the resizable "SimAnt - Quick Game" view,
    ...), which are WS_CHILD|WS_CAPTION but created with a NULL parent — i.e.
    top-level framed windows, not children of the main frame.  Windows parented
    to another window composite INTO it instead.  The desktop pseudo-window is
    never presented."""
    return [w for w in sysobj.windows
            if w.visible and w.parent == 0 and w.wndclass.name != "#desktop"]


def tree_version(sysobj, window) -> int:
    """Sum of surface versions over `window` and its visible descendants — a
    change-detect key so a host redraws the composite when any child repaints."""
    total = window.surface.version
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


def composite(sysobj, window, *, menu_bar: bool = True):
    """A NEW Surface: `window`'s pixels with its visible child windows blitted
    in at their positions (recursively), clipped to the window's client area.

    `menu_bar` paints the top-level frame's menu titles as a strip above the
    client — right for headless screenshots, but a host with a REAL menu widget
    (play.py's native tkinter menubar) passes menu_bar=False so the strip does
    not double the menu and offset the client."""
    import numpy as np

    from .api.objects import Surface

    base = window.surface
    out = Surface(base.w, base.h, bytearray(base.pixels))
    dst = np.frombuffer(out.pixels, dtype=np.uint8).reshape(base.h, base.w, 3)

    for child in child_windows(sysobj, window.handle):
        sub = composite(sysobj, child)          # grandchildren first
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

    # A top-level frame's menu bar is a presentation strip above the client.
    if not menu_bar:
        return out
    menu = getattr(window, "menu_obj", None)
    if menu is not None and menu.items and not is_child(window):
        out = _with_menu_bar(out, menu)
    return out
