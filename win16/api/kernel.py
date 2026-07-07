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

    @api.register("KERNEL", 131, ret="long")            # GetDOSEnvironment()
    def GetDOSEnvironment(ctx: CallContext) -> int:
        sys: Win16System = ctx.registry.services["system"]
        return sys.ensure_environment() << 16            # seg:0000 far pointer

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


DOS_SERVICES = {
    0x25: _dos_set_vector,
    0x30: _dos_get_version,
    0x35: _dos_get_vector,
}
