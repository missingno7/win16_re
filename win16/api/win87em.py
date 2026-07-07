"""WIN87EM — the floating-point emulator module.

The executable's FP OSFIXUPs are unapplied, so its x87 instructions remain in
their INT 34h..3Dh emulator forms; the actual math is serviced by the
machine's interrupt handler (win16/fpu.py, grown per observed instruction).
__fpMath here only has to honour the install/deinstall protocol the MSC
startup drives (dispatch on BX — observed: BX=0 install).
"""
from __future__ import annotations

from .core import ApiRegistry, CallContext, ret_far


def install(api: ApiRegistry) -> None:
    @api.register_raw("WIN87EM", 1)     # __fpMath — -register, dispatch on BX
    def __fpMath(ctx: CallContext) -> None:
        cpu = ctx.cpu
        bx = cpu.s.bx & 0xFFFF
        if bx == 0:
            # Install: real win87em hooks INT 34h..3Dh here.  Our machine
            # services those interrupts natively; nothing to set up.
            cpu.s.ax = 0
        elif bx == 2:
            # Deinstall (task exit path).
            cpu.s.ax = 0
        elif bx == 3:
            # Set FP error handler: DX:AX = far pointer to the app's handler.
            ctx.registry.services["fp_error_handler"] = (cpu.s.dx, cpu.s.ax)
        else:
            raise NotImplementedError(
                f"__fpMath subfunction BX={bx:04X}h — not yet observed/implemented")
        ret_far(cpu, 0)
