"""KERNEL services — implemented one observed call at a time.

Contracts follow the documented Win16 behaviour (Wine krnl386 + Undocumented
Windows), trimmed to what target executables prove they need; anything beyond
the implemented surface stays fail-loud.
"""
from __future__ import annotations

from .core import ApiRegistry, CallContext, ret_far
from .system import (INSTANCE_STACK_BOT, INSTANCE_STACK_MIN,
                     INSTANCE_STACK_TOP, Win16System)

WIN31_GETVERSION = 0x05000A03   # Windows 3.10 (0x0A03) on DOS 5.0 (0x0500)

# OpenFile style bits (Win16).
OF_READ = 0x0000
OF_WRITE = 0x0001
OF_READWRITE = 0x0002
OF_ACCESS_MASK = 0x0003
OF_CREATE = 0x1000
OF_EXIST = 0x4000
OF_REOPEN = 0x8000
OF_KNOWN = OF_ACCESS_MASK | OF_CREATE | OF_EXIST | OF_REOPEN


def install(api: ApiRegistry) -> None:
    @api.register_raw("KERNEL", 91)     # InitTask() — -register contract
    def InitTask(ctx: CallContext) -> None:
        sys: Win16System = ctx.registry.services["system"]
        cpu = ctx.cpu
        stack_top, sp0 = sys.stack_bounds()
        dgroup = sys.h_instance
        cpu.mem.ww(dgroup, INSTANCE_STACK_TOP, stack_top)
        cpu.mem.ww(dgroup, INSTANCE_STACK_MIN, sp0)
        cpu.mem.ww(dgroup, INSTANCE_STACK_BOT, sp0)
        # Register contract (Wine InitTask16): AX=1 ok, BX=cmdline offset,
        # CX=stack limit, DX=nCmdShow, SI=hPrevInstance, DI=hInstance,
        # ES=PSP segment.
        cpu.s.ax = 1
        cpu.s.bx = 0x81
        cpu.s.cx = ctx.registry.services["system"].machine.exe.header.stack_size
        cpu.s.dx = sys.cmd_show
        cpu.s.si = sys.h_prev_instance
        cpu.s.di = sys.h_instance
        cpu.s.es = sys.ensure_psp()
        sys.booted = True
        ret_far(cpu, 0)

    @api.register("KERNEL", 30, args="word")            # WaitEvent(hTask)
    def WaitEvent(ctx: CallContext) -> int:
        # The scheduler event KERNEL posts at task start is always "pending"
        # in this single-task world.
        return 1

    @api.register("KERNEL", 74, args="str ptr word")    # OpenFile(name, ofs, style)
    def OpenFile(ctx: CallContext) -> int:
        sys: Win16System = ctx.registry.services["system"]
        name_ptr, ofs_ptr, style = ctx.args
        if style & ~OF_KNOWN:
            raise NotImplementedError(f"OpenFile style {style:#06x} has unmodelled bits")
        dos_path = ctx.read_string(name_ptr).decode("latin-1")
        writable = bool(style & (OF_WRITE | OF_READWRITE))
        handle = sys.file_open(dos_path, writable=writable,
                               create=bool(style & OF_CREATE))
        # Fill OFSTRUCT: cBytes, fFixedDisk, nErrCode, reserved[4], szPathName[128].
        seg, off = (ofs_ptr >> 16) & 0xFFFF, ofs_ptr & 0xFFFF
        full = ("C:\\" + sys._canonical(dos_path)).encode("ascii")
        ctx.mem.wb(seg, off, 136)
        ctx.mem.wb(seg, off + 1, 1)
        err = 2 if handle < 0 else 0                     # 2 = file not found
        ctx.mem.ww(seg, off + 2, err)
        ctx.mem.load(seg, off + 8, full + b"\x00")
        if handle < 0:
            return 0xFFFF                                # HFILE_ERROR
        if style & OF_EXIST:
            sys.file_close(handle)                       # existence check only
            return 1
        return handle

    @api.register("KERNEL", 51, args="segptr word", ret="long")
    def MakeProcInstance(ctx: CallContext) -> int:      # (proc, hInstance)
        # Real KERNEL builds a DS-loading thunk; with one instance and a fixed
        # DGROUP the proc address itself is the correct thunk (Wine does the
        # same).
        return ctx.args[0]

    @api.register("KERNEL", 52, args="segptr")          # FreeProcInstance(proc)
    def FreeProcInstance(ctx: CallContext) -> int:
        return 1

    @api.register("KERNEL", 127, args="str str s_word str")
    def GetPrivateProfileInt(ctx: CallContext) -> int:  # (app, key, default, file)
        sys: Win16System = ctx.registry.services["system"]
        app, key, default, fname = ctx.args
        section = ctx.read_string(app).decode("latin-1").lower()
        keyname = ctx.read_string(key).decode("latin-1").lower()
        value = sys.profile(ctx.read_string(fname).decode("latin-1")) \
            .get(section, {}).get(keyname)
        if value is None:
            return default & 0xFFFF
        try:
            return int(value.strip() or "0", 0) & 0xFFFF
        except ValueError:
            return default & 0xFFFF

    @api.register("KERNEL", 128, args="str str str ptr word str")
    def GetPrivateProfileString(ctx: CallContext) -> int:
        # (app, key, default, buffer, size, file)
        sys: Win16System = ctx.registry.services["system"]
        app, key, default, buf, size, fname = ctx.args
        if not app or not key:
            raise NotImplementedError("profile enumeration (NULL app/key)")
        section = ctx.read_string(app).decode("latin-1").lower()
        keyname = ctx.read_string(key).decode("latin-1").lower()
        value = sys.profile(ctx.read_string(fname).decode("latin-1")) \
            .get(section, {}).get(keyname)
        if value is None:
            value = ctx.read_string(default).decode("latin-1")
        out = value.encode("latin-1")[:max(size - 1, 0)]
        ctx.mem.load((buf >> 16) & 0xFFFF, buf & 0xFFFF, out + b"\x00")
        return len(out)

    @api.register("KERNEL", 129, args="str str str str")
    def WritePrivateProfileString(ctx: CallContext) -> int:
        # (app, key, value, file) — stays in the in-memory profile store.
        sys: Win16System = ctx.registry.services["system"]
        app, key, value, fname = ctx.args
        prof = sys.profile(ctx.read_string(fname).decode("latin-1"))
        section = ctx.read_string(app).decode("latin-1").lower()
        if not key:
            prof.pop(section, None)
            return 1
        keyname = ctx.read_string(key).decode("latin-1").lower()
        if not value:
            prof.get(section, {}).pop(keyname, None)
        else:
            prof.setdefault(section, {})[keyname] = \
                ctx.read_string(value).decode("latin-1")
        return 1

    @api.register("KERNEL", 131, ret="long")            # GetDOSEnvironment()
    def GetDOSEnvironment(ctx: CallContext) -> int:
        sys: Win16System = ctx.registry.services["system"]
        return sys.ensure_environment() << 16            # seg:0000 far pointer

    @api.register("KERNEL", 85, args="str word")        # _lopen(name, mode)
    def _lopen(ctx: CallContext) -> int:
        sys: Win16System = ctx.registry.services["system"]
        name = ctx.read_string(ctx.args[0]).decode("latin-1")
        writable = bool(ctx.args[1] & 0x0003)            # OF_WRITE | OF_READWRITE
        h = sys.file_open(name, writable=writable)
        return h if h >= 0 else 0xFFFF                    # HFILE_ERROR

    @api.register("KERNEL", 83, args="str word")        # _lcreat(name, attr)
    def _lcreat(ctx: CallContext) -> int:
        sys: Win16System = ctx.registry.services["system"]
        name = ctx.read_string(ctx.args[0]).decode("latin-1")
        h = sys.file_open(name, writable=True, create=True)
        return h if h >= 0 else 0xFFFF

    @api.register("KERNEL", 81, args="word")            # _lclose(hf)
    def _lclose(ctx: CallContext) -> int:
        sys: Win16System = ctx.registry.services["system"]
        return 0 if sys.file_close(ctx.args[0]) else 0xFFFF

    @api.register("KERNEL", 82, args="word segptr word")  # _lread(hf, buf, count)
    def _lread(ctx: CallContext) -> int:
        sys: Win16System = ctx.registry.services["system"]
        vf = sys.files.get(ctx.args[0])
        if vf is None:
            return 0xFFFF
        chunk = bytes(vf.data[vf.pos:vf.pos + ctx.args[2]])
        if chunk:
            ctx.mem.load((ctx.args[1] >> 16) & 0xFFFF, ctx.args[1] & 0xFFFF, chunk)
        vf.pos += len(chunk)
        return len(chunk)

    @api.register("KERNEL", 86, args="word ptr word")   # _lwrite(hf, buf, count)
    def _lwrite(ctx: CallContext) -> int:
        sys: Win16System = ctx.registry.services["system"]
        vf = sys.files.get(ctx.args[0])
        if vf is None or not vf.writable:
            return 0xFFFF
        seg, off = (ctx.args[1] >> 16) & 0xFFFF, ctx.args[1] & 0xFFFF
        data = bytes(ctx.mem.rb(seg, (off + i) & 0xFFFF) for i in range(ctx.args[2]))
        end = vf.pos + len(data)
        if end > len(vf.data):
            vf.data.extend(b"\x00" * (end - len(vf.data)))
        vf.data[vf.pos:end] = data
        vf.pos = end
        vf.dirty = True
        return len(data)

    @api.register("KERNEL", 84, args="word long word", ret="long")  # _llseek
    def _llseek(ctx: CallContext) -> int:               # (hf, offset, origin)
        sys: Win16System = ctx.registry.services["system"]
        vf = sys.files.get(ctx.args[0])
        if vf is None:
            return 0xFFFFFFFF
        offset = ctx.args[1]
        if offset & 0x80000000:
            offset -= 1 << 32
        base = {0: 0, 1: vf.pos, 2: len(vf.data)}.get(ctx.args[2])
        if base is None:
            raise NotImplementedError(f"_llseek origin {ctx.args[2]}")
        vf.pos = max(base + offset, 0)
        return vf.pos & 0xFFFFFFFF

    @api.register("KERNEL", 88, args="segptr str", ret="long")   # lstrcpy(dst, src)
    def lstrcpy(ctx: CallContext) -> int:
        dst, src = ctx.args
        data = ctx.read_string(src)
        ctx.mem.load((dst >> 16) & 0xFFFF, dst & 0xFFFF, data + b"\x00")
        return dst

    @api.register("KERNEL", 15, args="word long")       # GlobalAlloc(flags, size)
    def GlobalAlloc(ctx: CallContext) -> int:
        sys: Win16System = ctx.registry.services["system"]
        flags, size = ctx.args
        return sys.global_alloc(size, zero=bool(flags & 0x0040))   # GMEM_ZEROINIT

    @api.register("KERNEL", 18, args="word", ret="long")  # GlobalLock(handle)
    def GlobalLock(ctx: CallContext) -> int:
        sys: Win16System = ctx.registry.services["system"]
        h = ctx.args[0]
        return (h << 16) if h in sys.global_blocks else 0     # far ptr seg:0000

    @api.register("KERNEL", 19, args="word")            # GlobalUnlock(handle)
    def GlobalUnlock(ctx: CallContext) -> int:
        return 0                    # nothing moves; unlock is a no-op success

    @api.register("KERNEL", 17, args="word")            # GlobalFree(handle)
    def GlobalFree(ctx: CallContext) -> int:
        sys: Win16System = ctx.registry.services["system"]
        sys.global_blocks.pop(ctx.args[0], None)         # not reclaimed (bump alloc)
        return 0                    # 0 = freed

    @api.register("KERNEL", 20, args="word")            # GlobalSize(handle)
    def GlobalSize(ctx: CallContext) -> int:
        sys: Win16System = ctx.registry.services["system"]
        return sys.global_blocks.get(ctx.args[0], 0)

    @api.register("KERNEL", 132, ret="long")            # GetWinFlags()
    def GetWinFlags(ctx: CallContext) -> int:
        return ctx.registry.equates.get(("KERNEL", 178), 0)

    @api.register("KERNEL", 134, args="ptr word")       # GetWindowsDirectory(buf, n)
    def GetWindowsDirectory(ctx: CallContext) -> int:
        buf, cap = ctx.args
        path = b"C:\\WINDOWS"[:max(cap - 1, 0)]
        ctx.mem.load((buf >> 16) & 0xFFFF, buf & 0xFFFF, path + b"\x00")
        return len(path)

    @api.register("KERNEL", 49, args="word ptr word")   # GetModuleFileName(h, buf, n)
    def GetModuleFileName(ctx: CallContext) -> int:
        sys: Win16System = ctx.registry.services["system"]
        _hmod, buf, cap = ctx.args
        path = sys.module_dos_path.encode("ascii")[:max(cap - 1, 0)]
        seg, off = (buf >> 16) & 0xFFFF, buf & 0xFFFF
        ctx.mem.load(seg, off, path + b"\x00")
        return len(path)

    @api.register("KERNEL", 5, args="word word")        # LocalAlloc(flags, size)
    def LocalAlloc(ctx: CallContext) -> int:
        from .localheap import LMEM_ZEROINIT
        sys: Win16System = ctx.registry.services["system"]
        flags, size = ctx.args
        ptr = sys.local_heap.alloc(size)
        if ptr and (flags & LMEM_ZEROINIT):
            dgroup = sys.h_instance
            for i in range(sys.local_heap.size_of(ptr)):
                ctx.mem.wb(dgroup, ptr + i, 0)
        return ptr

    @api.register("KERNEL", 7, args="word")             # LocalFree(handle)
    def LocalFree(ctx: CallContext) -> int:
        sys: Win16System = ctx.registry.services["system"]
        return 0 if sys.local_heap.free_block(ctx.args[0]) else ctx.args[0]

    @api.register("KERNEL", 10, args="word")            # LocalSize(handle)
    def LocalSize(ctx: CallContext) -> int:
        sys: Win16System = ctx.registry.services["system"]
        return sys.local_heap.size_of(ctx.args[0])

    @api.register("KERNEL", 23, args="word")            # LockSegment(seg)
    def LockSegment(ctx: CallContext) -> int:
        # Segments are immovable in the flat mapping; report success by
        # returning the (resolved) segment value.  0xFFFF = current DS.
        seg = ctx.args[0]
        return ctx.cpu.s.ds if seg == 0xFFFF else seg

    @api.register("KERNEL", 24, args="word")            # UnlockSegment(seg)
    def UnlockSegment(ctx: CallContext) -> int:
        # Lock counts are not modelled (nothing ever moves); 0 = "unlocked".
        return 0

    @api.register("KERNEL", 3, ret="long")              # GetVersion()
    def GetVersion(ctx: CallContext) -> int:
        return WIN31_GETVERSION

    @api.register_raw("KERNEL", 102)    # DOS3Call — INT 21h by far call
    def DOS3Call(ctx: CallContext) -> None:
        cpu = ctx.cpu
        ah = (cpu.s.ax >> 8) & 0xFF
        handler = DOS_SERVICES.get(ah)
        if handler is None:
            raise NotImplementedError(
                f"DOS3Call AH={ah:02X}h at {cpu.s.cs:04X}:{cpu.s.ip:04X} — "
                f"DOS service not implemented")
        handler(ctx)
        ret_far(cpu, 0)


def _dos_get_version(ctx: CallContext) -> None:
    # INT 21h AH=30h: AL=major, AH=minor (DOS 5.0), BX:CX = serial/OEM zeros.
    ctx.cpu.s.ax = 0x0005
    ctx.cpu.s.bx = 0
    ctx.cpu.s.cx = 0


def _dos_get_vector(ctx: CallContext) -> None:
    # AH=35h AL=int: ES:BX = current vector (Python-side table; no real IVT).
    sys: Win16System = ctx.registry.services["system"]
    seg, off = sys.int_vectors.get(ctx.cpu.s.ax & 0xFF, (0, 0))
    ctx.cpu.s.es = seg
    ctx.cpu.s.bx = off


def _dos_set_vector(ctx: CallContext) -> None:
    # AH=25h AL=int, DS:DX = new vector.
    sys: Win16System = ctx.registry.services["system"]
    sys.int_vectors[ctx.cpu.s.ax & 0xFF] = (ctx.cpu.s.ds, ctx.cpu.s.dx)


def _dos_get_date(ctx: CallContext) -> None:
    # AH=2Ah: CX=year DH=month DL=day AL=weekday.  Deterministic by default:
    # Saturday 1994-01-01 (weekday must match the date — apps recompute).
    ctx.cpu.s.cx = 1994
    ctx.cpu.s.dx = (1 << 8) | 1
    ctx.cpu.s.ax = (ctx.cpu.s.ax & 0xFF00) | 6


def _dos_get_time(ctx: CallContext) -> None:
    # AH=2Ch: CH=hour CL=min DH=sec DL=centisec.  Deterministic: midnight.
    ctx.cpu.s.cx = 0
    ctx.cpu.s.dx = 0


def _dos_file(ctx: CallContext, handle: int):
    sys: Win16System = ctx.registry.services["system"]
    vf = sys.files.get(handle)
    if vf is None:
        raise NotImplementedError(f"DOS call on unknown file handle {handle}")
    return vf


def _set_cf(ctx: CallContext, on: bool) -> None:
    if on:
        ctx.cpu.s.flags |= 0x0001
    else:
        ctx.cpu.s.flags &= ~0x0001


def _dos_read(ctx: CallContext) -> None:
    # AH=3Fh: BX=handle CX=count DS:DX=buffer -> AX=bytes read, CF clear.
    s = ctx.cpu.s
    vf = _dos_file(ctx, s.bx)
    chunk = bytes(vf.data[vf.pos:vf.pos + s.cx])
    if chunk:
        ctx.mem.load(s.ds, s.dx, chunk)
    vf.pos += len(chunk)
    s.ax = len(chunk)
    _set_cf(ctx, False)


def _dos_write(ctx: CallContext) -> None:
    # AH=40h: BX=handle CX=count DS:DX=buffer -> AX=bytes written.
    s = ctx.cpu.s
    vf = _dos_file(ctx, s.bx)
    if not vf.writable:
        raise NotImplementedError("DOS write to read-only handle")
    data = bytes(ctx.mem.rb(s.ds, (s.dx + i) & 0xFFFF) for i in range(s.cx))
    end = vf.pos + len(data)
    if end > len(vf.data):
        vf.data.extend(b"\x00" * (end - len(vf.data)))
    vf.data[vf.pos:end] = data
    vf.pos = end
    vf.dirty = True
    s.ax = len(data)
    _set_cf(ctx, False)


def _dos_close(ctx: CallContext) -> None:
    # AH=3Eh: BX=handle.
    sys: Win16System = ctx.registry.services["system"]
    ok = sys.file_close(ctx.cpu.s.bx)
    _set_cf(ctx, not ok)
    if not ok:
        ctx.cpu.s.ax = 6                                 # invalid handle


def _dos_lseek(ctx: CallContext) -> None:
    # AH=42h AL=origin CX:DX=offset -> DX:AX=new position.
    s = ctx.cpu.s
    vf = _dos_file(ctx, s.bx)
    offset = (s.cx << 16) | s.dx
    if offset & 0x80000000:
        offset -= 1 << 32
    origin = s.ax & 0xFF
    base = {0: 0, 1: vf.pos, 2: len(vf.data)}.get(origin)
    if base is None:
        raise NotImplementedError(f"lseek origin {origin}")
    vf.pos = max(base + offset, 0)
    s.ax = vf.pos & 0xFFFF
    s.dx = (vf.pos >> 16) & 0xFFFF
    _set_cf(ctx, False)


def _dos_terminate(ctx: CallContext) -> None:
    # AH=4Ch AL=exit code: the app is done — halt the machine cleanly.
    ctx.cpu.halted = True


DOS_SERVICES = {
    0x25: _dos_set_vector,
    0x4C: _dos_terminate,
    0x2A: _dos_get_date,
    0x2C: _dos_get_time,
    0x30: _dos_get_version,
    0x35: _dos_get_vector,
    0x3E: _dos_close,
    0x3F: _dos_read,
    0x40: _dos_write,
    0x42: _dos_lseek,
}
