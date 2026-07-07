"""MMSYSTEM — the Windows multimedia extension (WAV/MIDI/MCI).

Implemented per observed call.  Nothing is registered until a program proves
it needs it; unimplemented MMSYSTEM imports fail loud with their names.
"""
from __future__ import annotations

from .core import ApiRegistry, CallContext


def install(api: ApiRegistry) -> None:
    # No handlers yet — MICROMAN's sndPlaySound (MMSYSTEM.2) will land here as
    # the first slice when we start hardening the layer against it.
    return
