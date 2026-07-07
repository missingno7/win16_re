"""SOUND.DRV — the Windows 3.0 voice/note API.

Modelled as a deterministic event log: every call is recorded with the
virtual clock so the melody stream is replayable evidence.  Host audio output
is a later, separable layer (the dos_re "audio last" doctrine).
"""
from __future__ import annotations

from .core import ApiRegistry, CallContext
from .system import Win16System


def _log(ctx: CallContext, event: str, *args: int) -> None:
    sys: Win16System = ctx.registry.services["system"]
    ctx.registry.services.setdefault("sound_log", []).append(
        (sys.clock_ms, event, args))


def install(api: ApiRegistry) -> None:
    @api.register("SOUND", 1)                           # OpenSound()
    def OpenSound(ctx: CallContext) -> int:
        _log(ctx, "open")
        return 1                    # one voice available (PC-speaker model)

    @api.register("SOUND", 2)                           # CloseSound()
    def CloseSound(ctx: CallContext) -> int:
        _log(ctx, "close")
        return 0

    @api.register("SOUND", 3, args="word word")         # SetVoiceQueueSize(v, n)
    def SetVoiceQueueSize(ctx: CallContext) -> int:
        _log(ctx, "queue_size", *ctx.args)
        return 0

    @api.register("SOUND", 4, args="word word word word")
    def SetVoiceNote(ctx: CallContext) -> int:          # (voice, note, len, cdots)
        _log(ctx, "note", *ctx.args)
        return 0

    @api.register("SOUND", 5, args="word word word word word")
    def SetVoiceAccent(ctx: CallContext) -> int:        # (voice, tempo, vol, mode, pitch)
        _log(ctx, "accent", *ctx.args)
        return 0

    @api.register("SOUND", 9)                           # StartSound()
    def StartSound(ctx: CallContext) -> int:
        _log(ctx, "start")
        return 0

    @api.register("SOUND", 10)                          # StopSound()
    def StopSound(ctx: CallContext) -> int:
        _log(ctx, "stop")
        return 0