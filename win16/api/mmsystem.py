r"""MMSYSTEM — the Windows multimedia extension (WAV / MIDI / MCI).

Two consumers in SimAnt:
  * WAV sound effects via sndPlaySound (a static import), and
  * the MIDI music engine, which LoadLibrary's mmsystem.dll at runtime and
    GetProcAddress-es midiOutGetNumDevs + mciSendCommand, then plays the game's
    `sound\NAME.mid` files through the MCI sequencer.  (SimAnt only takes that
    path once mmsystem.dll is found on disk — we mark it a provided DLL so the
    game's existence probe passes, see kernel.LoadLibrary / system.file_open.)

Like the SOUND.DRV layer this is event-exact + presentation-split:
  * every MCI command / WAV request is appended to a deterministic log
    (`services["mci_log"]` / `services["sound_log"]`), the authoritative record
    replay and tests assert on; and
  * an optional host backend (`services["music_backend"]` for MIDI,
    `services["sound_backend"]` for WAV) turns the requests into real audio.
The deterministic model never depends on host audio, so replay stays exact.
"""
from __future__ import annotations

from .core import ApiRegistry, CallContext

SND_ASYNC = 0x0001
SND_NODEFAULT = 0x0002
SND_MEMORY = 0x0004
SND_LOOP = 0x0008
SND_NOSTOP = 0x0010

# MCI command messages (mmsystem.h)
MCI_OPEN = 0x0803
MCI_CLOSE = 0x0804
MCI_ESCAPE = 0x0805
MCI_PLAY = 0x0806
MCI_SEEK = 0x0809
MCI_STOP = 0x0808
MCI_PAUSE = 0x080A
MCI_INFO = 0x080B
MCI_GETDEVCAPS = 0x080C
MCI_SET = 0x080D
MCI_STATUS = 0x0814
MCI_RESUME = 0x0855
_MCI_NAMES = {
    MCI_OPEN: "open", MCI_CLOSE: "close", MCI_PLAY: "play", MCI_SEEK: "seek",
    MCI_STOP: "stop", MCI_PAUSE: "pause", MCI_INFO: "info",
    MCI_GETDEVCAPS: "getdevcaps", MCI_SET: "set", MCI_STATUS: "status",
    MCI_RESUME: "resume", MCI_ESCAPE: "escape",
}

# MCI_OPEN flags + MCI_OPEN_PARMS (16-bit) field offsets
MCI_OPEN_ELEMENT = 0x0200
MCI_OPEN_TYPE = 0x2000
_OPEN_WDEVICEID = 4         # WORD  (out): the opened device id
_OPEN_DEVTYPE = 8           # LPCSTR
_OPEN_ELEMENT = 12          # LPCSTR: the .mid file
# MCI_STATUS_PARMS: +4 dwReturn (out), +8 dwItem (in)
_STATUS_RETURN = 4
_STATUS_ITEM = 8
MCI_STATUS_MODE = 0x0004
MCI_MODE_STOP = 525
MCI_MODE_PLAY = 526


def _log(ctx: CallContext, event: str, *args) -> None:
    sysobj = ctx.registry.services["system"]
    ctx.registry.services.setdefault("mci_log", []).append(
        (sysobj.clock_ms, event, args))


def _far_str(ctx: CallContext, seg: int, off: int):
    fp = ctx.mem.rw(seg, off) | (ctx.mem.rw(seg, (off + 2) & 0xFFFF) << 16)
    if not fp:
        return None
    return ctx.read_string(fp).decode("latin-1")


def install(api: ApiRegistry) -> None:
    # mmsystem.dll is provided (its file "exists" + LoadLibrary succeeds).
    api.provided_dlls.add("MMSYSTEM.DLL")

    # -- dynamically-resolved procs (GetProcAddress after LoadLibrary) --------
    @api.register_proc("MMSYSTEM", "midiOutGetNumDevs", ret="word")
    def midiOutGetNumDevs(ctx: CallContext) -> int:
        # We provide the MIDI Mapper: report one output device so the game takes
        # its MCI music path.  Deterministic (not host-dependent) — actual audio
        # is presentation via the optional music backend.
        return 1

    # WAV sound-effects path (waveOut*): we implement the MIDI music route, not
    # the digitized-wave route, so report NO wave devices — the game keeps its
    # own effect fallback.  The individual waveOut* procs are still resolved (so
    # a stray call hits a safe stub, not a NULL far-call crash) and report a
    # bad-device error (2 = MMSYSERR_BADDEVICEID) so the game backs off.
    @api.register_proc("MMSYSTEM", "waveOutGetNumDevs", ret="word")
    def waveOutGetNumDevs(ctx: CallContext) -> int:
        return 0

    # Each waveOut* proc gets its real pascal arg spec so the callee-cleans
    # far-return pops the correct byte count even for the safe stub.
    _WAVE_STUBS = {
        "waveOutOpen": "ptr word ptr long long long",
        "waveOutClose": "word",
        "waveOutReset": "word",
        "waveOutWrite": "word ptr word",
        "waveOutPrepareHeader": "word ptr word",
        "waveOutUnprepareHeader": "word ptr word",
        "waveOutGetPosition": "word ptr word",
    }
    for _wave_proc, _spec in _WAVE_STUBS.items():
        api.register_proc("MMSYSTEM", _wave_proc, args=_spec, ret="word")(
            lambda ctx: 2)          # MMSYSERR_BADDEVICEID

    @api.register_proc("MMSYSTEM", "mciSendCommand",
                       args="word word long long", ret="long")
    def mciSendCommand(ctx: CallContext) -> int:  # (wDeviceID, wMsg, dwFlags, dwParam)
        dev, msg, flags, parm = ctx.args
        state = ctx.registry.services.setdefault(
            "mci_state", {"devices": {}, "next_id": 1})
        backend = ctx.registry.services.get("music_backend")
        pseg, poff = (parm >> 16) & 0xFFFF, parm & 0xFFFF

        if msg == MCI_OPEN:
            element = _far_str(ctx, pseg, (poff + _OPEN_ELEMENT) & 0xFFFF) if parm else None
            dev_id = state["next_id"]
            state["next_id"] += 1
            state["devices"][dev_id] = element
            if parm:                                    # return the device id
                ctx.mem.ww(pseg, (poff + _OPEN_WDEVICEID) & 0xFFFF, dev_id)
            _log(ctx, "open", dev_id, element)
            if backend is not None and element is not None:
                sysobj = ctx.registry.services["system"]
                host = sysobj.resolve_host_path(element)
                backend.open(dev_id, str(host) if host is not None else None)
            return 0

        if msg == MCI_STATUS:
            # Answer a mode query (playing? stopped?) from the backend; other
            # items report a benign 0.  dwReturn is the out field.
            item = (ctx.mem.rw(pseg, (poff + _STATUS_ITEM) & 0xFFFF)
                    | (ctx.mem.rw(pseg, (poff + _STATUS_ITEM + 2) & 0xFFFF) << 16)) if parm else 0
            ret = 0
            if item == MCI_STATUS_MODE:
                playing = backend.is_playing(dev) if backend is not None else False
                ret = MCI_MODE_PLAY if playing else MCI_MODE_STOP
            if parm:
                ctx.mem.ww(pseg, (poff + _STATUS_RETURN) & 0xFFFF, ret & 0xFFFF)
                ctx.mem.ww(pseg, (poff + _STATUS_RETURN + 2) & 0xFFFF, (ret >> 16) & 0xFFFF)
            _log(ctx, "status", dev, item, ret)
            return 0

        # play / stop / close / set / seek / pause / resume — log + drive backend
        name = _MCI_NAMES.get(msg, f"#{msg:#x}")
        _log(ctx, name, dev, flags)
        if backend is not None:
            if msg == MCI_PLAY:
                backend.play(dev)
            elif msg in (MCI_STOP, MCI_PAUSE):
                backend.stop(dev)
            elif msg == MCI_RESUME:
                backend.play(dev)
            elif msg == MCI_CLOSE:
                backend.close(dev)
                state["devices"].pop(dev, None)
        return 0                                        # MMSYSERR_NOERROR

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
