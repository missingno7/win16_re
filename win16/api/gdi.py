"""GDI services — objects, DCs, blitting. Implemented per observed call."""
from __future__ import annotations

from .core import ApiRegistry, CallContext
from .objects import (DC, Bitmap, Brush, Font, Palette, Region, StockObject,
                      Surface, _signed, blit)
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


def _dc_surface(sys: Win16System, hdc: int) -> Surface | None:
    """The DC's target pixels; None for a NULL hdc (the caller returns the
    API's documented failure).  A non-zero garbage handle still fails loud —
    that would mean OUR handle table broke, not app behaviour."""
    if hdc == 0:
        return None
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
    dst.touch()
    row = bytes(rgb) * (x1 - x0)
    for yy in range(y0, y1):
        off = (yy * dst.w + x0) * 3
        dst.pixels[off:off + len(row)] = row


_STOCK_BRUSH_RGB = {
    "WHITE_BRUSH": (255, 255, 255), "BLACK_BRUSH": (0, 0, 0),
    "LTGRAY_BRUSH": (192, 192, 192), "GRAY_BRUSH": (128, 128, 128),
    "DKGRAY_BRUSH": (64, 64, 64), "NULL_BRUSH": None, "HOLLOW_BRUSH": None,
}


def brush_object_rgb(brush) -> tuple[int, int, int] | None:
    """RGB of a Brush/StockObject (None for a hollow/null brush)."""
    if isinstance(brush, Brush):
        c = brush.color
        return (c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF)
    kind = getattr(brush, "kind", None)
    if kind in _STOCK_BRUSH_RGB:
        return _STOCK_BRUSH_RGB[kind]
    raise NotImplementedError(f"brush {kind!r} has no fill colour")


def _brush_rgb(sys: Win16System, hdc: int) -> tuple[int, int, int]:
    dc = sys.handles.require(hdc, DC)
    return brush_object_rgb(dc.selected.get("brush"))


def install(api: ApiRegistry) -> None:
    @api.register("GDI", 66, args="long")               # CreateSolidBrush(color)
    def CreateSolidBrush(ctx: CallContext) -> int:
        return _sys(ctx).handles.add(Brush(ctx.args[0] & 0xFFFFFF))

    @api.register("GDI", 56,                            # CreateFont(...14 params)
                  args="s_word s_word s_word s_word s_word word word word "
                       "word word word word word str")
    def CreateFont(ctx: CallContext) -> int:
        sys = _sys(ctx)
        height = abs(_signed(ctx.args[0]))
        face = ctx.read_string(ctx.args[13]).decode("latin-1") if ctx.args[13] else ""
        font = Font(height=height, facename=face)
        sys.handles.add(font)
        return font.handle

    @api.register("GDI", 349, args="word long", ret="long")  # SetMapperFlags(hdc,flag)
    def SetMapperFlags(ctx: CallContext) -> int:
        # Controls whether the font mapper matches aspect ratio.  Our renderer
        # uses one fixed cell, so there is nothing to match — report the
        # previous flags (0, the default) and change nothing.
        return 0

    @api.register("GDI", 119, args="str")               # AddFontResource(lpFilename)
    def AddFontResource(ctx: CallContext) -> int:
        # The custom raster font (SimAnt's FONTRES.FON) is accepted so its
        # later CreateFont(faceName) succeeds; our text renderer maps every
        # font onto the fixed 8x13 cell (a presentation approximation), so
        # nothing is actually installed.  Report one font added (success).
        return 1

    @api.register("GDI", 150, args="word")              # UnrealizeObject(hObject)
    def UnrealizeObject(ctx: CallContext) -> int:
        # For a palette: reset it so the next RealizePalette fully re-maps it.
        # Our RealizePalette already re-maps in full every call (static single-
        # app system palette), so there is nothing to reset — report success.
        return 1

    @api.register("GDI", 38,                             # Escape(hdc, esc, cb, in, out)
                  args="word s_word s_word ptr ptr")
    def Escape(ctx: CallContext) -> int:
        # Device escapes (printer/plotter control) are not modelled.  The only
        # one apps call unconditionally is QUERYESCSUPPORT (8), a capability
        # probe — reporting 0 (unsupported) for every escape is honest and
        # makes the app take its standard no-escape path.
        return 0

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
        if ctx.args[0] == 0 or ctx.args[1] == 0:
            return 0        # documented failure for NULL handles (real GDI)
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
        elif isinstance(obj, Font):
            kind = "font"
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
        if dst is None:
            return 0
        if hsrc:
            src = _dc_surface(sys, hsrc)
            if src is None:
                return 0
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
        if dst is None:
            return 0
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
        if hdc == 0:
            return 0
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
        dst.touch()
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

    def _font_metrics(dc):
        """(height, ascent, descent, avewidth, maxwidth) for the DC's font."""
        kind = getattr(dc.selected.get("font"), "kind", None)
        fixed = (13, 11, 2, 8, 8)
        metrics = {
            "ANSI_FIXED_FONT": fixed, "SYSTEM_FIXED_FONT": fixed,
            "OEM_FIXED_FONT": fixed,
            "SYSTEM_FONT": (16, 12, 3, 7, 14),      # proportional system font
            "ANSI_VAR_FONT": (13, 11, 2, 6, 12),
            "DEVICE_DEFAULT_FONT": (16, 12, 3, 7, 14),
        }.get(kind)
        if metrics is None:
            raise NotImplementedError(f"font metrics for {kind!r}")
        return metrics

    @api.register("GDI", 91, args="word ptr word", ret="long")  # GetTextExtent
    def GetTextExtent(ctx: CallContext) -> int:         # (hdc, lpString, nCount)
        sys = _sys(ctx)
        dc = sys.handles.require(ctx.args[0], DC)
        height, _asc, _desc, avew, _maxw = _font_metrics(dc)
        count = ctx.args[2] & 0xFFFF
        # Fixed-cell approximation (as TextOut renders): width = count * avg.
        return ((height & 0xFFFF) << 16) | ((count * avew) & 0xFFFF)

    @api.register("GDI", 93, args="word ptr")           # GetTextMetrics(hdc, lptm)
    def GetTextMetrics(ctx: CallContext) -> int:
        sys = _sys(ctx)
        dc = sys.handles.require(ctx.args[0], DC)
        # The text renderer treats everything as an 8x13 cell; these are the
        # documented Win3.1 VGA metrics apps query for layout.
        height, ascent, descent, avew, maxw = _font_metrics(dc)
        seg, off = (ctx.args[1] >> 16) & 0xFFFF, ctx.args[1] & 0xFFFF
        words = [height, ascent, descent, 3, 0, avew, maxw, 400]
        for i, v in enumerate(words):            # height ascent descent intlead...
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
        if dst is None or src is None:
            return 0
        dx, dy, dw, dh = _signed(dx), _signed(dy), _signed(dw), _signed(dh)
        sx, sy, sw, sh = _signed(sx), _signed(sy), _signed(sw), _signed(sh)
        if dw <= 0 or dh <= 0 or sw <= 0 or sh <= 0:
            raise NotImplementedError("StretchBlt with mirrored/empty extents")
        # Nearest-neighbour sampling (COLORONCOLOR semantics).  The GDI
        # default mode is BLACKONWHITE (AND-combining dropped pixels) — if
        # radar pixel evidence ever disagrees, honour dc.stretch_mode here.
        dst.touch()
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

    @api.register("GDI", 80, args="word s_word")        # GetDeviceCaps(hdc, index)
    def GetDeviceCaps(ctx: CallContext) -> int:
        # A 256-colour palettised VGA (640x480), the display Win3.1 games target.
        caps = {
            4: 208, 6: 156,             # HORZSIZE/VERTSIZE (mm)
            8: 640, 10: 480,            # HORZRES / VERTRES
            12: 8, 14: 1,               # BITSPIXEL / PLANES
            16: -1, 18: -1, 22: -1,     # NUMBRUSHES/PENS/FONTS (device: unlimited)
            24: 20,                     # NUMCOLORS (static system colours)
            26: 0,                      # DEVICESIZE
            38: 0x0100,                 # RASTERCAPS: RC_PALETTE
            40: 8, 42: 8,               # ASPECTX / ASPECTY
            88: 96, 90: 96,             # LOGPIXELSX / LOGPIXELSY
            104: 256, 106: 20, 108: 8,  # SIZEPALETTE / NUMRESERVED / COLORRES
        }
        idx = _signed(ctx.args[1])
        if idx not in caps:
            raise NotImplementedError(f"GetDeviceCaps index {idx}")
        return caps[idx] & 0xFFFF

    # index -> (256,3) uint8 numpy LUT, keyed on the raw colour-table bytes +
    # palette identity.  A game blits dozens of times per frame with the SAME
    # table (microman: ~40 tile blits/frame); rebuilding the LUT per call was
    # the profiled hot spot (256 mem.rw per blit).  NOTE: if SetPaletteEntries/
    # AnimatePalette are ever implemented they must invalidate this cache
    # (key on a palette version, not just identity).
    _dib_lut_cache: dict = {}

    @api.register("GDI", 443,                           # SetDIBitsToDevice
                  args="word s_word s_word s_word s_word s_word s_word "
                       "word word ptr ptr word")
    def SetDIBitsToDevice(ctx: CallContext) -> int:
        import struct
        import numpy as np
        sys = _sys(ctx)
        (hdc, xd, yd, cx, cy, xs, ys, start, lines, bits, bmi, coloruse) = ctx.args
        cx, cy, xs, ys = _signed(cx), _signed(cy), _signed(xs), _signed(ys)
        dst = _dc_surface(sys, hdc)
        if dst is None:
            return 0
        dc = sys.handles.require(hdc, DC)

        bseg, boff = (bmi >> 16) & 0xFFFF, bmi & 0xFFFF
        hdr = ctx.mem.block(bseg, boff, 40)
        size, w, h, _pl, bpp, comp = struct.unpack_from("<IiiHHI", hdr, 0)
        if comp != 0 or bpp not in (1, 4, 8):
            raise NotImplementedError(
                f"SetDIBitsToDevice bpp={bpp} comp={comp} (only 1/4/8bpp BI_RGB)")
        ncolors = 1 << bpp                          # 2 / 16 / 256
        clr_used = min(struct.unpack_from("<I", hdr, 32)[0] or ncolors, ncolors)

        # 256-entry index -> (r,g,b) LUT from the DIB colour table (cached).
        if coloruse == 1:
            # DIB_PAL_COLORS: the table is 16-bit WORD indices into the DC's
            # selected logical palette.  Microman's WAP pages use exactly this
            # (an identity table 0..255 into the 256-entry palette created
            # from the page BMP's colour table).  An earlier revision decoded
            # the table as RGBQUAD regardless of the flag — an artifact of
            # observing blits while the page LOAD was failing (SelectPalette
            # returned 0), before any real PAL_COLORS table existed.
            dc_pal = dc.palette
            if dc_pal is None:
                raise NotImplementedError(
                    "SetDIBitsToDevice DIB_PAL_COLORS with no palette "
                    "selected into the DC — map through the system palette "
                    "when a real program exercises this")
            table = ctx.mem.block(bseg, (boff + size) & 0xFFFF, clr_used * 2)
            key = (1, table, id(dc_pal.entries), len(dc_pal.entries))
            lut = _dib_lut_cache.get(key)
            if lut is None:
                entries = dc_pal.entries
                n = len(entries)
                lut = np.zeros((256, 3), dtype=np.uint8)
                words = struct.unpack_from("<%dH" % clr_used, table, 0)
                for i, word in enumerate(words):
                    if word < n:
                        lut[i] = entries[word]
                _dib_lut_cache[key] = lut
        else:
            # DIB_RGB_COLORS: RGBQUAD (B,G,R,0) — the standard 8bpp table.
            table = ctx.mem.block(bseg, (boff + size) & 0xFFFF, clr_used * 4)
            key = (0, table)
            lut = _dib_lut_cache.get(key)
            if lut is None:
                quads = np.frombuffer(table, dtype=np.uint8).reshape(-1, 4)
                lut = np.zeros((256, 3), dtype=np.uint8)
                lut[:clr_used, 0] = quads[:, 2]                 # R
                lut[:clr_used, 1] = quads[:, 1]                 # G
                lut[:clr_used, 2] = quads[:, 0]                 # B
                _dib_lut_cache[key] = lut

        stride = ((w * bpp + 31) // 32) * 4         # 4bpp packs 2 px/byte
        # The bits buffer can exceed 64K (microman: 512x320 = 160KB).  Resolve
        # the far pointer to a LINEAR base via the selector map, then read the
        # whole (contiguous) block linearly — segment-relative offsets would
        # wrap at 64K and tile/garble the image.
        base_lin = ctx.mem._xlat((bits >> 16) & 0xFFFF, bits & 0xFFFF)
        mem_np = np.frombuffer(ctx.mem.data, dtype=np.uint8)
        dst3d = np.frombuffer(dst.pixels, dtype=np.uint8).reshape(dst.h, dst.w, 3)

        # Clipping is analytic on both axes (the region is a rectangle), so
        # the whole blit is a handful of numpy ops — a per-row Python loop
        # cost 278k tiny array calls per profiled second on microman's ~40
        # small tile blits per frame.
        # x: keep i in [i_lo, i_hi) with xs+i (source) and xd+i (dest) valid.
        i_lo = max(0, -xs, -xd)
        i_hi = min(cx, w - xs, dst.w - xd)
        # y: source buffer row r(j) = r0 - j must be >= 0; dest row yd+j in range.
        top0 = h - ys - cy                      # top-down y of the source region top
        r0 = (h - 1) - start - top0
        j_lo = max(0, -yd)
        j_hi = min(cy, r0 + 1, dst.h - yd)
        if i_hi > i_lo and j_hi > j_lo:
            rows = base_lin + (r0 - np.arange(j_lo, j_hi)) * stride    # (R,) row bases
            cols = np.arange(xs + i_lo, xs + i_hi)                     # source columns
            if bpp == 8:
                idx = mem_np[rows[:, None] + cols]
            elif bpp == 4:                       # 4bpp: 2 px/byte, high nibble first
                raw = mem_np[rows[:, None] + (cols >> 1)]
                idx = np.where(cols & 1, raw & 0x0F, raw >> 4)
            else:                                # 1bpp: 8 px/byte, MSB = leftmost
                raw = mem_np[rows[:, None] + (cols >> 3)]
                idx = (raw >> (7 - (cols & 7))) & 1
            dst3d[yd + j_lo:yd + j_hi, xd + i_lo:xd + i_hi] = lut[idx]
        dst.touch()
        return cy

    @api.register("GDI", 360, args="ptr")               # CreatePalette(lpLogPalette)
    def CreatePalette(ctx: CallContext) -> int:
        seg, off = (ctx.args[0] >> 16) & 0xFFFF, ctx.args[0] & 0xFFFF
        count = ctx.mem.rw(seg, (off + 2) & 0xFFFF)
        entries = []
        for i in range(count):
            b = (off + 4 + i * 4) & 0xFFFF
            entries.append((ctx.mem.rb(seg, b), ctx.mem.rb(seg, (b + 1) & 0xFFFF),
                            ctx.mem.rb(seg, (b + 2) & 0xFFFF)))
        return _sys(ctx).handles.add(Palette(entries))

    @api.register("GDI", 363, args="word word word ptr")  # GetPaletteEntries
    def GetPaletteEntries(ctx: CallContext) -> int:     # (hpal, start, count, lppe)
        sys = _sys(ctx)
        pal = sys.handles.get(ctx.args[0])
        if not isinstance(pal, Palette):
            return 0
        start, count, ptr = ctx.args[1], ctx.args[2], ctx.args[3]
        seg, off = (ptr >> 16) & 0xFFFF, ptr & 0xFFFF
        n = 0
        for i in range(count):
            if start + i >= len(pal.entries):
                break
            r, g, b = pal.entries[start + i]
            base = (off + i * 4) & 0xFFFF
            ctx.mem.wb(seg, base, r); ctx.mem.wb(seg, (base + 1) & 0xFFFF, g)
            ctx.mem.wb(seg, (base + 2) & 0xFFFF, b); ctx.mem.wb(seg, (base + 3) & 0xFFFF, 0)
            n += 1
        return n

    @api.register("GDI", 370, args="word long")         # GetNearestPaletteIndex
    def GetNearestPaletteIndex(ctx: CallContext) -> int:  # (hpal, color)
        pal = _sys(ctx).handles.get(ctx.args[0])
        if not isinstance(pal, Palette) or not pal.entries:
            return 0
        c = ctx.args[1]
        return pal.nearest(c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF)

    @api.register("GDI", 374, args="word")              # GetSystemPaletteUse(hdc)
    def GetSystemPaletteUse(ctx: CallContext) -> int:
        return 1                    # SYSPAL_STATIC

    @api.register("GDI", 375, args="word word word ptr")  # GetSystemPaletteEntries
    def GetSystemPaletteEntries(ctx: CallContext) -> int:  # (hdc, start, count, lppe)
        # The REAL system palette of the static single-app model: whatever the
        # app last realized (RealizePalette copies its logical palette here).
        # Microman's WAP builds its DIB_PAL_COLORS word table by nearest-
        # matching these entries into its logical palette — an earlier
        # grayscale-ramp stub made that remap collapse every page to grays.
        sys = _sys(ctx)
        start, count, ptr = ctx.args[1], ctx.args[2], ctx.args[3]
        entries = sys.system_palette
        if ptr:
            seg, off = (ptr >> 16) & 0xFFFF, ptr & 0xFFFF
            for i in range(count):
                idx = start + i
                r, g, b = entries[idx] if idx < len(entries) else (0, 0, 0)
                base = (off + i * 4) & 0xFFFF
                ctx.mem.wb(seg, base, r)                       # PALETTEENTRY:
                ctx.mem.wb(seg, (base + 1) & 0xFFFF, g)        # R, G, B, flags
                ctx.mem.wb(seg, (base + 2) & 0xFFFF, b)
                ctx.mem.wb(seg, (base + 3) & 0xFFFF, 0)
        return count

    @api.register("GDI", 3, args="word word")           # SetMapMode(hdc, mode)
    def SetMapMode(ctx: CallContext) -> int:
        # Only MM_TEXT (1:1 device units) is modelled; anything else would need
        # a coordinate transform the renderer doesn't do.
        if ctx.args[1] != 1:
            raise NotImplementedError(f"SetMapMode {ctx.args[1]} (only MM_TEXT)")
        return 1                    # previous mode = MM_TEXT

    @api.register("GDI", 181, args="word ptr")          # GetRgnBox(hrgn, lpRect)
    def GetRgnBox(ctx: CallContext) -> int:
        NULLREGION, SIMPLEREGION = 1, 2
        rgn = _sys(ctx).handles.get(ctx.args[0])
        if not isinstance(rgn, Region):
            return 0                                     # ERROR
        seg, off = (ctx.args[1] >> 16) & 0xFFFF, ctx.args[1] & 0xFFFF
        for i, v in enumerate(rgn.bounds):
            ctx.mem.ww(seg, (off + 2 * i) & 0xFFFF, v & 0xFFFF)
        return NULLREGION if rgn.is_empty() else SIMPLEREGION

    @api.register("GDI", 64, args="word word word word")  # CreateRectRgn
    def CreateRectRgn(ctx: CallContext) -> int:         # (x1, y1, x2, y2)
        x1, y1, x2, y2 = (_signed(a) for a in ctx.args)
        # GDI normalizes so x1<=x2, y1<=y2.
        rgn = Region(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        return _sys(ctx).handles.add(rgn)

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