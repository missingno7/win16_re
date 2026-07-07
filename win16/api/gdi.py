"""GDI services — objects, DCs, blitting. Implemented per observed call."""
from __future__ import annotations

from .core import ApiRegistry, CallContext
from .objects import DC, Bitmap, Brush, StockObject, Surface, _signed, blit
from .system import Win16System

STOCK_NAMES = {
    0: "WHITE_BRUSH", 1: "LTGRAY_BRUSH", 2: "GRAY_BRUSH", 3: "DKGRAY_BRUSH",
    4: "BLACK_BRUSH", 5: "NULL_BRUSH", 6: "WHITE_PEN", 7: "BLACK_PEN",
    8: "NULL_PEN", 10: "OEM_FIXED_FONT", 11: "ANSI_FIXED_FONT",
    12: "ANSI_VAR_FONT", 13: "SYSTEM_FONT", 14: "DEVICE_DEFAULT_FONT",
    15: "DEFAULT_PALETTE", 16: "SYSTEM_FIXED_FONT",
}


def _sys(ctx: CallContext) -> Win16System:
    return ctx.registry.services["system"]


def _dc_surface(sys: Win16System, hdc: int) -> Surface:
    dc = sys.handles.require(hdc, DC)
    if dc.is_memory:
        return dc.bitmap.surface
    return dc.window.surface


def _fill_rect(dst: Surface, x: int, y: int, w: int, h: int,
               rgb: tuple[int, int, int]) -> None:
    x0, y0 = max(x, 0), max(y, 0)
    x1, y1 = min(x + w, dst.w), min(y + h, dst.h)
    if x0 >= x1 or y0 >= y1:
        return
    row = bytes(rgb) * (x1 - x0)
    for yy in range(y0, y1):
        off = (yy * dst.w + x0) * 3
        dst.pixels[off:off + len(row)] = row


def _brush_rgb(sys: Win16System, hdc: int) -> tuple[int, int, int]:
    dc = sys.handles.require(hdc, DC)
    brush = dc.selected.get("brush")
    if isinstance(brush, Brush):
        c = brush.color
        return (c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF)
    kind = getattr(brush, "kind", None)
    fixed = {"WHITE_BRUSH": (255, 255, 255), "BLACK_BRUSH": (0, 0, 0),
             "LTGRAY_BRUSH": (192, 192, 192), "GRAY_BRUSH": (128, 128, 128),
             "DKGRAY_BRUSH": (64, 64, 64)}
    if kind in fixed:
        return fixed[kind]
    raise NotImplementedError(f"brush {kind!r} has no fill colour")


def install(api: ApiRegistry) -> None:
    @api.register("GDI", 66, args="long")               # CreateSolidBrush(color)
    def CreateSolidBrush(ctx: CallContext) -> int:
        return _sys(ctx).handles.add(Brush(ctx.args[0] & 0xFFFFFF))

    @api.register("GDI", 87, args="word")               # GetStockObject(index)
    def GetStockObject(ctx: CallContext) -> int:
        return _sys(ctx).stock_object(ctx.args[0])

    @api.register("GDI", 52, args="word")               # CreateCompatibleDC(hdc)
    def CreateCompatibleDC(ctx: CallContext) -> int:
        return _sys(ctx).new_dc(is_memory=True)

    @api.register("GDI", 51, args="word s_word s_word") # CreateCompatibleBitmap
    def CreateCompatibleBitmap(ctx: CallContext) -> int:
        _hdc, w, h = ctx.args
        return _sys(ctx).handles.add(Bitmap(Surface(max(w, 1), max(h, 1))))

    @api.register("GDI", 45, args="word word")          # SelectObject(hdc, hobj)
    def SelectObject(ctx: CallContext) -> int:
        sys = _sys(ctx)
        dc = sys.handles.require(ctx.args[0], DC)
        obj = sys.handles.get(ctx.args[1])
        if isinstance(obj, Bitmap):
            if not dc.is_memory:
                return 0
            prev = dc.bitmap
            dc.bitmap = obj
            return prev.handle if prev else 0
        if isinstance(obj, Brush):
            kind = "brush"
        elif isinstance(obj, StockObject):
            kind = ("brush" if "BRUSH" in obj.kind else
                    "pen" if "PEN" in obj.kind else
                    "font" if "FONT" in obj.kind else "palette")
        else:
            raise NotImplementedError(
                f"SelectObject of {type(obj).__name__} not implemented")
        prev = dc.selected.get(kind)
        dc.selected[kind] = obj
        return prev.handle if prev else 0

    @api.register("GDI", 68, args="word")               # DeleteDC(hdc)
    def DeleteDC(ctx: CallContext) -> int:
        sys = _sys(ctx)
        if sys.handles.get(ctx.args[0]) is None:
            return 0
        sys.handles.remove(ctx.args[0])
        return 1

    @api.register("GDI", 34,                            # BitBlt
                  args="word s_word s_word s_word s_word word s_word s_word long")
    def BitBlt(ctx: CallContext) -> int:
        sys = _sys(ctx)
        hdst, x, y, w, h, hsrc, sx, sy, rop = ctx.args
        dst = _dc_surface(sys, hdst)
        if hsrc:
            src = _dc_surface(sys, hsrc)
        elif rop in (0x00000042, 0x00FF0062):            # BLACKNESS/WHITENESS
            src = None
        else:
            raise NotImplementedError(f"BitBlt with NULL src and rop {rop:#010x}")
        x, y, w, h = _signed(x), _signed(y), _signed(w), _signed(h)
        if src is None:
            _fill_rect(dst, x, y, w, h,
                       (0, 0, 0) if rop == 0x00000042 else (255, 255, 255))
        else:
            blit(dst, x, y, src, _signed(sx), _signed(sy), w, h, rop)
        return 1

    @api.register("GDI", 2, args="word word")           # SetBkMode(hdc, mode)
    def SetBkMode(ctx: CallContext) -> int:
        dc = _sys(ctx).handles.require(ctx.args[0], DC)
        old = dc.bk_mode
        dc.bk_mode = ctx.args[1]
        return old

    @api.register("GDI", 9, args="word long", ret="long")
    def SetTextColor(ctx: CallContext) -> int:          # SetTextColor(hdc, color)
        dc = _sys(ctx).handles.require(ctx.args[0], DC)
        old = dc.text_color
        dc.text_color = ctx.args[1] & 0xFFFFFF
        return old

    @api.register("GDI", 7, args="word word")           # SetStretchBltMode(hdc, mode)
    def SetStretchBltMode(ctx: CallContext) -> int:
        dc = _sys(ctx).handles.require(ctx.args[0], DC)
        old = dc.stretch_mode
        dc.stretch_mode = ctx.args[1]
        return old

    @api.register("GDI", 29, args="word s_word s_word s_word s_word long")
    def PatBlt(ctx: CallContext) -> int:                # (hdc, x, y, w, h, rop)
        sys = _sys(ctx)
        hdc, x, y, w, h, rop = ctx.args
        dst = _dc_surface(sys, hdc)
        x, y, w, h = _signed(x), _signed(y), _signed(w), _signed(h)
        if rop == 0x00000042:                            # BLACKNESS
            _fill_rect(dst, x, y, w, h, (0, 0, 0))
        elif rop == 0x00FF0062:                          # WHITENESS
            _fill_rect(dst, x, y, w, h, (255, 255, 255))
        elif rop == 0x00F00021:                          # PATCOPY
            _fill_rect(dst, x, y, w, h, _brush_rgb(sys, hdc))
        else:
            raise NotImplementedError(f"PatBlt rop {rop:#010x}")
        return 1

    @api.register("GDI", 33, args="word s_word s_word ptr word")
    def TextOut(ctx: CallContext) -> int:               # (hdc, x, y, str, count)
        from win16.font8x8 import glyph_rows
        sys = _sys(ctx)
        hdc, x, y, str_ptr, count = ctx.args
        dc = sys.handles.require(hdc, DC)
        dst = _dc_surface(sys, hdc)
        x, y = _signed(x), _signed(y)
        seg, off = (str_ptr >> 16) & 0xFFFF, str_ptr & 0xFFFF
        text = bytes(ctx.mem.rb(seg, (off + i) & 0xFFFF) for i in range(count))
        c = dc.text_color
        fg = (c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF)
        b = dc.bk_color
        bg = (b & 0xFF, (b >> 8) & 0xFF, (b >> 16) & 0xFF)
        # Fixed 8x13 cell (the metrics contract); the 8x8 glyph sits 2 rows
        # below the cell top.  Presentation-layer approximation of the real
        # Windows raster fonts.
        for i, ch in enumerate(text):
            cx = x + i * 8
            if dc.bk_mode == 2:                          # OPAQUE
                _fill_rect(dst, cx, y, 8, 13, bg)
            rows = glyph_rows(ch)
            for ry, rowbits in enumerate(rows):
                for rx in range(8):
                    if rowbits & (1 << rx):
                        px, py = cx + rx, y + 2 + ry
                        if 0 <= px < dst.w and 0 <= py < dst.h:
                            o = (py * dst.w + px) * 3
                            dst.pixels[o:o + 3] = bytes(fg)
        return 1

    @api.register("GDI", 93, args="word ptr")           # GetTextMetrics(hdc, lptm)
    def GetTextMetrics(ctx: CallContext) -> int:
        sys = _sys(ctx)
        dc = sys.handles.require(ctx.args[0], DC)
        font = dc.selected.get("font")
        kind = getattr(font, "kind", None)
        if kind not in ("ANSI_FIXED_FONT", "SYSTEM_FIXED_FONT", "OEM_FIXED_FONT"):
            raise NotImplementedError(f"GetTextMetrics for font {kind!r}")
        # Classic Win3.1 VGA fixed font: 8x13, ascent 11.  The text renderer
        # must stay consistent with these numbers.
        seg, off = (ctx.args[1] >> 16) & 0xFFFF, ctx.args[1] & 0xFFFF
        words = [13, 11, 2, 3, 0, 8, 8, 400]     # height ascent descent intlead
        for i, v in enumerate(words):            # extlead avewidth maxwidth weight
            ctx.mem.ww(seg, (off + 2 * i) & 0xFFFF, v)
        tail = [0, 0, 0, 0x20, 0xFF, 0x2E, 0x20, 0x31, 0]  # italic..charset
        for i, v in enumerate(tail):
            ctx.mem.wb(seg, (off + 16 + i) & 0xFFFF, v)
        for i, v in enumerate([0, 96, 96]):      # overhang, aspect X/Y
            ctx.mem.ww(seg, (off + 25 + 2 * i) & 0xFFFF, v)
        return 1

    @api.register("GDI", 35,                            # StretchBlt
                  args="word s_word s_word s_word s_word word s_word s_word s_word s_word long")
    def StretchBlt(ctx: CallContext) -> int:
        sys = _sys(ctx)
        (hdst, dx, dy, dw, dh, hsrc, sx, sy, sw, sh, rop) = ctx.args
        if rop != 0x00CC0020:
            raise NotImplementedError(f"StretchBlt rop {rop:#010x}")
        dst, src = _dc_surface(sys, hdst), _dc_surface(sys, hsrc)
        dx, dy, dw, dh = _signed(dx), _signed(dy), _signed(dw), _signed(dh)
        sx, sy, sw, sh = _signed(sx), _signed(sy), _signed(sw), _signed(sh)
        if dw <= 0 or dh <= 0 or sw <= 0 or sh <= 0:
            raise NotImplementedError("StretchBlt with mirrored/empty extents")
        # Nearest-neighbour sampling (COLORONCOLOR semantics).  The GDI
        # default mode is BLACKONWHITE (AND-combining dropped pixels) — if
        # radar pixel evidence ever disagrees, honour dc.stretch_mode here.
        for row in range(dh):
            syy = sy + row * sh // dh
            if not (0 <= dy + row < dst.h and 0 <= syy < src.h):
                continue
            doff = ((dy + row) * dst.w + dx) * 3
            for col in range(dw):
                sxx = sx + col * sw // dw
                if 0 <= dx + col < dst.w and 0 <= sxx < src.w:
                    soff = (syy * src.w + sxx) * 3
                    dst.pixels[doff + col * 3:doff + col * 3 + 3] = \
                        src.pixels[soff:soff + 3]
        return 1

    @api.register("GDI", 69, args="word")               # DeleteObject(handle)
    def DeleteObject(ctx: CallContext) -> int:
        sys = _sys(ctx)
        obj = sys.handles.get(ctx.args[0])
        if obj is None:
            return 0
        if isinstance(obj, StockObject):
            return 1                # deleting stock objects is a no-op success
        sys.handles.remove(ctx.args[0])
        return 1