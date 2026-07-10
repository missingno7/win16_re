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
    # -- dynamically-resolved procs (GetProcAddress on a LoadLibrary'd
    #    mmsystem.dll) — SimAnt's MIDI music engine resolves these by name.
    @api.register_proc("MMSYSTEM", "midiOutGetNumDevs", ret="word")
    def midiOutGetNumDevs(ctx: CallContext) -> int:
        # Number of MIDI output devices.  Phase 1 reports 0 (no MIDI) so the
        # game keeps its SOUND.DRV fallback; Phase 2 (the mciSendCommand engine)
        # flips this to a real device once .mid playback is wired.
        return 0

    @api.register("MMSYSTEM", 2, args="ptr word")       # sndPlaySound(lpszSound, flags)
    def sndPlaySound(ctx: CallContext) -> int:
        # Event-exact audio model (mirrors SOUND.DRV): every request is logged
        # (the authoritative record); host WAV output happens when a backend is
        # installed.  lpszSound may be a filename (SND_MEMORY clear) or a far
        # pointer to a RIFF/WAV image in memory (SND_MEMORY set — microman's
        # SFX: fire/hit sounds are WAV blobs it built in a global buffer).
        # NULL ptr == stop the current sound.
        ptr, flags = ctx.args[0], ctx.args[1]
        sysobj = ctx.registry.services["system"]
        name = None
        data = None
        if ptr and (flags & SND_MEMORY):
            data = _read_wav_image(ctx, ptr)
        elif ptr:
            name = ctx.read_string(ptr).decode("latin-1")
        ctx.registry.services.setdefault("sound_log", []).append(
            (sysobj.clock_ms, "wav",
             (name if name is not None
              else (f"<memory {len(data)}B>" if data else None), flags)))
        backend = ctx.registry.services.get("sound_backend")
        if backend is not None and hasattr(backend, "play_wav"):
            if not ptr:
                backend.stop_wav()
            elif data is not None:
                backend.play_wav(data, loop=bool(flags & SND_LOOP))
            elif name is not None:
                handle = sysobj.file_open(name)
                if handle >= 0:
                    file_data = bytes(sysobj.files[handle].data)
                    sysobj.file_close(handle)
                    backend.play_wav(file_data, loop=bool(flags & SND_LOOP))
        return 1                    # TRUE — sound "played"


def _read_wav_image(ctx, ptr: int) -> bytes | None:
    """Copy a RIFF/WAV blob out of VM memory: read the 8-byte RIFF header,
    take its declared chunk size, and pull the whole file (huge-pointer safe
    via the selector-translated linear base)."""
    seg, off = (ptr >> 16) & 0xFFFF, ptr & 0xFFFF
    header = ctx.mem.block(seg, off, 8)
    if header[:4] != b"RIFF":
        return None
    riff_size = int.from_bytes(header[4:8], "little")
    return ctx.mem.block(seg, off, 8 + riff_size)
