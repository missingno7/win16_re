"""MMSYSTEM — the Windows multimedia extension (WAV/MIDI/MCI).

Implemented per observed call.  Nothing is registered until a program proves
it needs it; unimplemented MMSYSTEM imports fail loud with their names.
"""
from __future__ import annotations

from .core import ApiRegistry, CallContext

SND_ASYNC = 0x0001
SND_NODEFAULT = 0x0002
SND_MEMORY = 0x0004
SND_LOOP = 0x0008
SND_NOSTOP = 0x0010


def install(api: ApiRegistry) -> None:
    @api.register("MMSYSTEM", 2, args="ptr word")       # sndPlaySound(lpszSound, flags)
    def sndPlaySound(ctx: CallContext) -> int:
        # Event-exact audio model (mirrors SOUND.DRV): log the request; host
        # WAV output is a later, separable slice.  NULL ptr == stop sound.
        flags = ctx.args[1]
        sysobj = ctx.registry.services["system"]
        name = None
        if ctx.args[0] and not (flags & SND_MEMORY):
            name = ctx.read_string(ctx.args[0]).decode("latin-1")
        ctx.registry.services.setdefault("sound_log", []).append(
            (sysobj.clock_ms, "wav", (name, flags)))
        return 1                    # TRUE — sound "played"
