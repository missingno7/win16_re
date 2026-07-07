"""SetDIBitsToDevice colour decoding — the microman grayscale bug regression.

Verifies an 8bpp DIB with an RGBQUAD colour table renders in COLOUR (not the
grayscale we produced when we mis-read the table as palette indices), even when
the call passes fuColorUse=DIB_PAL_COLORS (microman does exactly this).
No game boot needed — the DIB is crafted directly in VM memory.
"""
import struct

import pytest

from ppython import runtime
from win16.api.core import CallContext
from win16.api.objects import DC, Surface, Window, WndClass

pytestmark = pytest.mark.skipif(not runtime.assets_present(),
                                reason="game assets not present")


def test_setdibitstodevice_decodes_rgbquad_as_colour():
    m = runtime.create_machine()            # loaded, not run — fast
    sysobj = m.api.services["system"]

    cls = WndClass(name="t", style=0, wndproc=(0, 0), cls_extra=0, wnd_extra=0,
                   h_instance=0, h_icon=0, h_cursor=0, h_background=0, menu_name=None)
    sysobj.handles.add(cls)
    win = Window(wndclass=cls, title="", style=0, x=0, y=0, w=4, h=2,
                 parent=0, menu=0)
    win._surface = Surface(4, 2)
    sysobj.handles.add(win)
    dc = DC(window=win)
    sysobj.handles.add(dc)

    seg = m.free_para                        # scratch paragraph past the image
    m.free_para += 64
    mem = m.mem
    # BITMAPINFOHEADER: 8bpp, 4x2, BI_RGB, clrUsed=2.
    hdr = struct.pack("<IiiHHIIiiII", 40, 4, 2, 1, 8, 0, 0, 0, 0, 2, 0)
    for i, byteval in enumerate(hdr):
        mem.wb(seg, i, byteval)
    ct = 40                                  # RGBQUAD table (B,G,R,0)
    for i, quad in enumerate([(0, 0, 0), (0, 0, 192)]):   # black, then red (R=192)
        for k in range(3):
            mem.wb(seg, ct + i * 4 + k, quad[k])
        mem.wb(seg, ct + i * 4 + 3, 0)
    bits = 0x100                             # 8bpp, stride 4; bottom-up
    for x in range(4):
        mem.wb(seg, bits + x, 1)             # buffer row 0 (bottom) = red index
        mem.wb(seg, bits + 4 + x, 0)         # buffer row 1 (top)    = black index

    bmi_ptr = seg << 16
    bits_ptr = (seg << 16) | bits
    ctx = CallContext(m.cpu, m.api, "GDI", 443, "SetDIBitsToDevice",
                      args=(dc.handle, 0, 0, 4, 2, 0, 0, 0, 2, bits_ptr, bmi_ptr, 1))
    m.api.entries[("GDI", 443)].handler(ctx)

    surf = win.surface

    def px(x, y):
        o = (y * surf.w + x) * 3
        return tuple(surf.pixels[o:o + 3])

    # Bottom-up: top dest row is black, bottom dest row is RED (not gray).
    assert px(0, 0) == (0, 0, 0)
    assert px(0, 1) == (192, 0, 0)
    assert px(3, 1) == (192, 0, 0)
