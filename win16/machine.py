"""The Win16 machine record + memory map constants — CPU-CARRIER-FREE.

Split out of :mod:`win16.loader` so a **CPUless** runtime can hold a Win16
machine without importing the interpreter.  ``win16.loader`` imports
``dos_re.cpu`` at module level (it builds one), which the CPUless import wall
(``dos_re.lift.standalone.install_import_guard``) forbids outright — so every
consumer that needs only the *record* and the *memory map* takes them from
here instead, and ``win16.loader`` re-exports them unchanged for the VM path.

Nothing in this module executes an instruction: ``Win16Machine.cpu`` is
whatever carrier the host bound (a ``CPU8086`` under the VM, a
``win16.cpuless.CpuFreeCarrier`` under the CPUless runtime), and the INT
service reads and writes it through ``.s`` / ``.mem`` alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .api.core import ApiRegistry, Win16ApiGap

if TYPE_CHECKING:                       # annotations only — no runtime import
    from .ne import NEExecutable

#: The data-only boot image's manifest schema.  It lives on the CPU-free side
#: because BOTH loaders gate on it: ``win16.bootimage`` (the VM path, which
#: builds a CPU8086 and is therefore behind the CPUless wall) and
#: ``win16.cpuless.load_cpuless_image``.
BOOT_MANIFEST_SCHEMA = "win16_vmless_boot_manifest/v1"

THUNK_SEG = 0x0060          # import thunk slots live here (hooked CS:IP values)
IMAGE_BASE_PARA = 0x0100    # first NE segment maps at this paragraph

# Win16 uses selector translation to lift the 1MB real-mode ceiling: the loaded
# program's own segments stay in low real-mode memory (< 1MB); GlobalAlloc
# blocks live above it as selectors mapping into the linear space
# [GLOBAL_LIN_START, WIN16_MEM_SIZE).  The gap below GLOBAL_LIN_START avoids the
# dos_re EGA shadow region at 0x100000 (unused by Win16 but reserved).
WIN16_MEM_SIZE = 0x400000       # 4 MB
GLOBAL_LIN_START = 0x140000     # global heap starts here (after EGA shadow)


class LoaderError(RuntimeError):
    pass


@dataclass
class Win16Machine:
    exe: "NEExecutable"
    cpu: object                             # CPU8086, or a CPU-free carrier
    mem: object                             # dos_re Memory
    api: ApiRegistry
    seg_bases: list[int]                    # 1-based NE segment -> paragraph base
    free_para: int                          # bump allocator frontier (paragraphs)
    osfixups: list[tuple[int, int, int]] = field(default_factory=list)  # (seg, off, kind)

    def seg_base(self, ne_segment: int) -> int:
        return self.seg_bases[ne_segment]

    def alloc_paragraphs(self, paras: int) -> int:
        """Allocate a paragraph-aligned block; returns its segment value."""
        base = self.free_para
        if (base + paras) << 4 > self.mem.size:
            raise LoaderError("out of VM memory")
        self.free_para += paras
        return base

    def interrupt(self, cpu, num: int) -> None:
        """The Win16 INT surface.  Reads/writes ``cpu.s`` and ``cpu.mem`` only,
        so it services a CPU-free carrier identically (the CPUless runtime's
        ``plat.intr`` routes here)."""
        if num == 0x21:
            # Windows services INT 21h for apps — the same DOS surface as
            # KERNEL's DOS3Call (SimAnt's C runtime startup calls DOS raw).
            from .api.core import CallContext
            from .api.kernel import DOS_SERVICES
            ah = (cpu.s.ax >> 8) & 0xFF
            handler = DOS_SERVICES.get(ah)
            if handler is None:
                raise Win16ApiGap(
                    f"INT 21h AH={ah:02X}h at {cpu.s.cs:04X}:{cpu.s.ip:04X} — "
                    f"DOS service not implemented")
            handler(CallContext(cpu, self.api, "DOS", num,
                                f"int21_{ah:02X}", args=()))
            return
        if num == 0x2F:
            # DOS multiplex.  SimAnt probes AH=45h subfunctions for a companion
            # TSR/driver that is not present in this VM.  With no handler
            # installed, the default IVT handler is an IRET that leaves the
            # registers as-is — an honest "no such service", which lets the game
            # take its no-TSR fallback path.  (No faking: the service genuinely
            # is not there.)  CF is left clear.
            return
        raise Win16ApiGap(
            f"INT {num:02X}h at {cpu.s.cs:04X}:{cpu.s.ip:04X} — no Win16 service installed")
