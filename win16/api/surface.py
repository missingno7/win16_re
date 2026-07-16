"""The full Win16 API surface as a registry factory — loader-free.

``build_registry`` constructs the complete KERNEL/USER/GDI/SOUND/MMSYSTEM/
dialog/win87em API registry.  It lives here (not in ``win16.app``) so an
EXE-independent runtime (``win16.bootimage`` — dos_re_2.0 §1a') can rebuild
the API surface WITHOUT importing the NE loader: ``win16.app`` composes this
factory with ``load_ne``/``parse_ne`` for the normal EXE boot path, while the
strict-VMless boot path composes it with a generated data-only boot image.
The independence lint proves the strict runner's import graph never reaches a
loader symbol — this module is what keeps that graph closed.
"""
from __future__ import annotations

from win16.api import dialogs, gdi, kernel, mmsystem, sound, user, win87em
from win16.api.core import ApiRegistry

# __WINFLAGS (KERNEL.178 equate) default: WF_PMODE | WF_CPU286 | WF_STANDARD,
# no WF_80x87 — the loader leaves FP OSFIXUPs unapplied, so a program's INT
# 34h..3Dh emulator forms stay live.  A game with real x87 (no OSFIXUPs) can
# pass a value with WF_80x87 set.
WINFLAGS_NO_FPU = 0x0013


def build_registry(*, winflags: int = WINFLAGS_NO_FPU) -> ApiRegistry:
    api = ApiRegistry()
    api.register_equate("KERNEL", 178, winflags)       # __WINFLAGS
    # Huge-pointer stride: apps add __AHINCR to a selector to reach the next
    # 64K.  With the selector model (win16/hugeheap.py) selectors step by 8 and
    # consecutive ones map to consecutive 64K, so these are the real
    # protected-mode values — a >64K buffer walk lands on the right descriptor.
    api.register_equate("KERNEL", 113, 3)              # __AHSHIFT
    api.register_equate("KERNEL", 114, 8)              # __AHINCR
    kernel.install(api)
    user.install(api)
    gdi.install(api)
    sound.install(api)
    mmsystem.install(api)
    dialogs.install(api)
    win87em.install(api)
    return api
