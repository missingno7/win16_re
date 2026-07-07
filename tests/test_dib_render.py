"""SetDIBitsToDevice colour decoding — both fuColorUse modes, pinned.

DIB_RGB_COLORS(0): the colour table is RGBQUAD (B,G,R,0).
DIB_PAL_COLORS(1): the colour table is 16-bit WORD indices into the DC's
selected logical palette — microman's WAP pages use exactly this (an identity
index table into the 256-entry palette created from the page BMP).  An earlier
revision decoded PAL_COLORS tables as RGBQUAD; that pin was an artifact of
observing blits while the page load was failing (SelectPalette returned 0 for
a fresh DC), before any real PAL_COLORS table existed.
No game boot needed — the DIBs are crafted directly in VM memory.
"""
import struct

import pytest

from ppython import runtime
from win16.api.core import CallContext
from win16.api.objects import DC, Palette, Surface, Window, WndClass

pytestmark = pytest.mark.skipif(not runtime.assets_present(),
                                reason="game assets not present")


def _make_target(m, w=4, h=2):
    sysobj = m.api.services["system"]
    cls = WndClass(name="t", style=0, wndproc=(0, 0), cls_extra=0, wnd_extra=0,
                   h_instance=0, h_icon=0, h_cursor=0, h_background=0,
                   menu_name=None)
    sysobj.handles.add(cls)
    win = Window(wndclass=cls, title="", style=0, x=0, y=0, w=w, h=h,
                 parent=0, menu=0)
    win._surface = Surface(w, h)
    sysobj.handles.add(win)
    dc = DC(window=win)
    sysobj.handles.add(dc)
    return sysobj, win, dc


def _write_header(mem, seg, clr_used):
    hdr = struct.pack("<IiiHHIIiiII", 40, 4, 2, 1, 8, 0, 0, 0, 0, clr_used, 0)
    for i, byteval in enumerate(hdr):
        mem.wb(seg, i, byteval)


def _write_bits(mem, seg, bits):
    for x in range(4):
        mem.wb(seg, bits + x, 1)             # buffer row 0 (bottom) = index 1
        mem.wb(seg, bits + 4 + x, 0)         # buffer row 1 (top)    = index 0


def _blit(m, dc, seg, bits, coloruse):
    ctx = CallContext(m.cpu, m.api, "GDI", 443, "SetDIBitsToDevice",
                      args=(dc.handle, 0, 0, 4, 2, 0, 0, 0, 2,
                            (seg << 16) | bits, seg << 16, coloruse))
    m.api.entries[("GDI", 443)].handler(ctx)


def _px(surf, x, y):
    o = (y * surf.w + x) * 3
    return tuple(surf.pixels[o:o + 3])


def test_rgb_colors_decodes_rgbquad():
    m = runtime.create_machine()            # loaded, not run — fast
    _sysobj, win, dc = _make_target(m)
    seg = m.free_para
    m.free_para += 64
    mem = m.mem
    _write_header(mem, seg, clr_used=2)
    ct = 40                                  # RGBQUAD table (B,G,R,0)
    for i, quad in enumerate([(0, 0, 0), (0, 0, 192)]):   # black, red (R=192)
        for k in range(3):
            mem.wb(seg, ct + i * 4 + k, quad[k])
        mem.wb(seg, ct + i * 4 + 3, 0)
    bits = 0x100
    _write_bits(mem, seg, bits)
    _blit(m, dc, seg, bits, coloruse=0)

    surf = win.surface
    # Bottom-up: top dest row is black, bottom dest row is RED (not gray).
    assert _px(surf, 0, 0) == (0, 0, 0)
    assert _px(surf, 0, 1) == (192, 0, 0)
    assert _px(surf, 3, 1) == (192, 0, 0)


def test_pal_colors_maps_words_through_dc_palette():
    m = runtime.create_machine()
    sysobj, win, dc = _make_target(m)
    # A logical palette selected into the DC; entry 5 is green.
    pal = Palette(entries=[(0, 0, 0)] * 5 + [(0, 200, 0)])
    pal.handle = sysobj.handles.add(pal)
    dc.palette = pal
    seg = m.free_para
    m.free_para += 64
    mem = m.mem
    _write_header(mem, seg, clr_used=2)
    ct = 40                                  # WORD index table: [0, 5]
    mem.ww(seg, ct + 0, 0)
    mem.ww(seg, ct + 2, 5)
    bits = 0x100
    _write_bits(mem, seg, bits)
    _blit(m, dc, seg, bits, coloruse=1)

    surf = win.surface
    # Index 1 -> palette word 5 -> GREEN via the DC's logical palette.
    assert _px(surf, 0, 0) == (0, 0, 0)
    assert _px(surf, 0, 1) == (0, 200, 0)
    assert _px(surf, 3, 1) == (0, 200, 0)


def test_pal_colors_without_palette_fails_loud():
    m = runtime.create_machine()
    _sysobj, _win, dc = _make_target(m)
    assert dc.palette is None
    seg = m.free_para
    m.free_para += 64
    mem = m.mem
    _write_header(mem, seg, clr_used=2)
    mem.ww(seg, 40, 0)
    mem.ww(seg, 42, 1)
    bits = 0x100
    _write_bits(mem, seg, bits)
    with pytest.raises(NotImplementedError):
        _blit(m, dc, seg, bits, coloruse=1)
