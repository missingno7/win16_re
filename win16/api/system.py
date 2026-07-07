"""Win16System — the OS-side state behind the API handlers.

Owns task identity (hInstance == DGROUP selector, Win16 convention), the
PSP-style command-line block, and grows handle tables as the API surface
grows.  Game-agnostic: configured entirely from the loaded machine.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SW_SHOWNORMAL = 1

# INSTANCEDATA offsets in DGROUP (the 16 reserved bytes at seg:0000).
INSTANCE_STACK_TOP = 0x0A     # lowest stack address (stack grows down)
INSTANCE_STACK_MIN = 0x0C     # lowest SP observed
INSTANCE_STACK_BOT = 0x0E     # initial SP (bottom of the stack)


@dataclass
class Win16System:
    machine: object                     # win16.loader.Win16Machine
    cmd_show: int = SW_SHOWNORMAL
    command_line: bytes = b""
    module_dos_path: str = ""           # virtual DOS path of the EXE
    psp_seg: int = 0
    h_prev_instance: int = 0
    booted: bool = False                # set once InitTask has run
    int_vectors: dict[int, tuple[int, int]] = field(default_factory=dict)
    env_seg: int = 0
    _local_heap: object = None

    def ensure_environment(self) -> int:
        """DOS environment block: ASCIIZ vars, double zero, WORD 1, exe path."""
        if not self.env_seg:
            block = b"PATH=C:\\\x00" + b"\x00"
            block += (1).to_bytes(2, "little")
            block += self.module_dos_path.encode("ascii") + b"\x00"
            paras = (len(block) + 15) >> 4
            self.env_seg = self.machine.alloc_paragraphs(paras)
            self.machine.mem.load(self.env_seg, 0, block)
        return self.env_seg

    @property
    def local_heap(self):
        """DGROUP local heap: [static data + stack, end of DGROUP allocation)."""
        if self._local_heap is None:
            from .localheap import LocalHeap
            hdr = self.machine.exe.header
            _, sp0 = self.stack_bounds()
            self._local_heap = LocalHeap(sp0, sp0 + hdr.heap_size)
        return self._local_heap

    def __post_init__(self) -> None:
        self.machine.api.services["system"] = self
        if not self.module_dos_path:
            self.module_dos_path = "C:\\" + self.machine.exe.path.name.upper()

    @property
    def h_instance(self) -> int:
        return self.machine.seg_bases[self.machine.exe.header.auto_data_seg]

    def ensure_psp(self) -> int:
        """Allocate a PSP-style paragraph block holding the command tail."""
        if not self.psp_seg:
            tail = self.command_line[:126]
            self.psp_seg = self.machine.alloc_paragraphs(16)  # 256 bytes
            mem = self.machine.mem
            mem.wb(self.psp_seg, 0x80, len(tail))
            mem.load(self.psp_seg, 0x81, tail + b"\x0d")
        return self.psp_seg

    def stack_bounds(self) -> tuple[int, int]:
        """(lowest stack address, initial SP) within DGROUP."""
        hdr = self.machine.exe.header
        data_len = self.machine.exe.segments[hdr.auto_data_seg - 1].alloc_size
        sp0 = (data_len + hdr.stack_size) & ~1
        return data_len, sp0
