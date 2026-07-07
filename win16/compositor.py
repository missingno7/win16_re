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


def child_windows(sysobj, parent_handle: int) -> list:
    """Visible direct children of a window, in Z-order (creation order)."""
    return [w for w in sysobj.windows
            if w.parent == parent_handle and w.visible]


def is_child(window) -> bool:
    return bool(window.style & WS_CHILD)


def top_level_windows(sysobj) -> list:
    """Visible windows that are NOT children of another window (the frames a
    host should present; their children composite into them)."""
    return [w for w in sysobj.windows
            if w.visible and not is_child(w)]


def tree_version(sysobj, window) -> int:
    """Sum of surface versions over `window` and its visible descendants — a
    change-detect key so a host redraws the composite when any child repaints."""
    total = window.surface.version
    for child in child_windows(sysobj, window.handle):
        total += tree_version(sysobj, child)
    return total


def composite(sysobj, window):
    """A NEW Surface: `window`'s pixels with its visible child windows blitted
    in at their positions (recursively), clipped to the window's client area."""
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
    return out
