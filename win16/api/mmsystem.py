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

# -- waveOut ------------------------------------------------------------------
MMSYSERR_NOERROR = 0
MMSYSERR_BADDEVICEID = 2
MMSYSERR_INVALHANDLE = 5
MMSYSERR_NOMEM = 7
WAVERR_UNPREPARED = 34
WAVERR_STILLPLAYING = 33

WAVE_MAPPER = 0xFFFF
WAVE_FORMAT_PCM = 1
CALLBACK_TYPEMASK = 0x00070000
CALLBACK_NULL = 0x00000000
CALLBACK_WINDOW = 0x00010000

MM_WOM_DONE = 0x03BD                # the buffer-finished notification

# WAVEHDR (16-bit): lpData is a far pointer, the rest DWORDs/WORDs.
_WH_LPDATA = 0
_WH_BUFFERLENGTH = 4
_WH_BYTESRECORDED = 8
_WH_USER = 12
_WH_FLAGS = 16
_WH_LOOPS = 20
_WH_NEXT = 24
SIZEOF_WAVEHDR = 32

WHDR_DONE = 0x00000001
WHDR_PREPARED = 0x00000002
WHDR_INQUEUE = 0x00000010

# PCMWAVEFORMAT (16-bit) field offsets
_WF_FORMATTAG = 0           # WORD
_WF_CHANNELS = 2            # WORD
_WF_SAMPLESPERSEC = 4       # DWORD
_WF_AVGBYTESPERSEC = 8      # DWORD
_WF_BLOCKALIGN = 12         # WORD
_WF_BITSPERSAMPLE = 14      # WORD  (PCM only)

# MMTIME (16-bit): UINT wType; union { DWORD ms; ... } at +2
_MMTIME_TYPE = 0
_MMTIME_VALUE = 2
TIME_MS = 0x0001
TIME_SAMPLES = 0x0002
TIME_BYTES = 0x0004

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

    # -- the waveOut device ---------------------------------------------------
    # A digitized-audio output device on the VIRTUAL clock.  A program opens it
    # with a wave format, prepares + writes buffers of PCM, and learns that a
    # buffer finished when MM_WOM_DONE arrives (CALLBACK_WINDOW) — see
    # _wave_write for the completion model.  Playback itself is presentation
    # (`services["sound_backend"]`); nothing the program can observe depends on
    # it, so a machine with no audio device behaves identically.
    @api.register_proc("MMSYSTEM", "waveOutGetNumDevs", ret="word")
    def waveOutGetNumDevs(ctx: CallContext) -> int:
        # We provide one wave output device (the wave mapper).  Deterministic:
        # this reports the DEVICE MODEL, not whether the host has a sound card.
        return 1

    @api.register_proc("MMSYSTEM", "waveOutOpen",
                       args="ptr word ptr long long long", ret="word")
    def waveOutOpen(ctx: CallContext) -> int:
        # (lphWaveOut, uDeviceID, lpFormat, dwCallback, dwInstance, fdwOpen)
        lphwo, dev_id, lpfmt, callback, instance, flags = ctx.args
        fmt = _read_wave_format(ctx, lpfmt)
        if fmt["tag"] != WAVE_FORMAT_PCM:
            raise NotImplementedError(
                f"waveOutOpen: only PCM (tag 1) is implemented, got format tag "
                f"{fmt['tag']:#x} — implement the codec against this call site")
        cb_type = flags & CALLBACK_TYPEMASK
        if cb_type not in (CALLBACK_NULL, CALLBACK_WINDOW):
            raise NotImplementedError(
                f"waveOutOpen: callback type {cb_type:#x} is not implemented "
                f"(only CALLBACK_NULL / CALLBACK_WINDOW observed)")
        if flags & ~(CALLBACK_TYPEMASK):
            raise NotImplementedError(
                f"waveOutOpen: unimplemented open flags {flags & ~CALLBACK_TYPEMASK:#x}")
        if dev_id != WAVE_MAPPER and dev_id != 0:
            return MMSYSERR_BADDEVICEID
        sysobj = ctx.registry.services["system"]
        state = _wave_state(ctx)
        hwo = state["next_id"]
        state["next_id"] += 1
        state["devices"][hwo] = {
            "format": fmt,
            # CALLBACK_WINDOW: the low word of dwCallback is the notify window.
            "hwnd": (callback & 0xFFFF) if cb_type == CALLBACK_WINDOW else 0,
            "instance": instance,
            "opened_ms": sysobj.tick_count(),
            "queue_end_ms": sysobj.tick_count(),   # when the queued audio drains
            "pending": [],
        }
        if lphwo:
            _write_far_word(ctx, lphwo, hwo)
        _wave_log(ctx, "open", hwo, fmt["rate"], fmt["bits"], fmt["channels"])
        return MMSYSERR_NOERROR

    @api.register_proc("MMSYSTEM", "waveOutPrepareHeader",
                       args="word ptr word", ret="word")
    def waveOutPrepareHeader(ctx: CallContext) -> int:
        hwo, lpwh, cbwh = ctx.args
        _settle(ctx)
        if _wave_dev(ctx, hwo) is None:
            return MMSYSERR_INVALHANDLE
        _check_wavehdr_size(cbwh)
        flags = _far_dword(ctx, lpwh + _WH_FLAGS)
        _write_far_dword(ctx, lpwh + _WH_FLAGS, flags | WHDR_PREPARED)
        return MMSYSERR_NOERROR

    @api.register_proc("MMSYSTEM", "waveOutUnprepareHeader",
                       args="word ptr word", ret="word")
    def waveOutUnprepareHeader(ctx: CallContext) -> int:
        hwo, lpwh, cbwh = ctx.args
        _settle(ctx)
        if _wave_dev(ctx, hwo) is None:
            return MMSYSERR_INVALHANDLE
        _check_wavehdr_size(cbwh)
        flags = _far_dword(ctx, lpwh + _WH_FLAGS)
        if flags & WHDR_INQUEUE:
            return WAVERR_STILLPLAYING      # real MMSYSTEM refuses; so do we
        _write_far_dword(ctx, lpwh + _WH_FLAGS, flags & ~WHDR_PREPARED)
        return MMSYSERR_NOERROR

    @api.register_proc("MMSYSTEM", "waveOutWrite",
                       args="word ptr word", ret="word")
    def waveOutWrite(ctx: CallContext) -> int:
        _settle(ctx)
        return _wave_write(ctx, *ctx.args)

    @api.register_proc("MMSYSTEM", "waveOutReset", args="word", ret="word")
    def waveOutReset(ctx: CallContext) -> int:
        hwo = ctx.args[0]
        _settle(ctx)
        dev = _wave_dev(ctx, hwo)
        if dev is None:
            return MMSYSERR_INVALHANDLE
        # Reset abandons every queued buffer and restarts the play position.
        _abandon_pending(ctx, hwo, dev)
        sysobj = ctx.registry.services["system"]
        dev["opened_ms"] = dev["queue_end_ms"] = sysobj.tick_count()
        backend = ctx.registry.services.get("sound_backend")
        if backend is not None and hasattr(backend, "stop_pcm"):
            backend.stop_pcm()
        _wave_log(ctx, "reset", hwo)
        return MMSYSERR_NOERROR

    @api.register_proc("MMSYSTEM", "waveOutClose", args="word", ret="word")
    def waveOutClose(ctx: CallContext) -> int:
        hwo = ctx.args[0]
        _settle(ctx)
        state = _wave_state(ctx)
        dev = state["devices"].get(hwo)
        if dev is None:
            return MMSYSERR_INVALHANDLE
        if dev["pending"]:
            # Real MMSYSTEM refuses to close a device with buffers still queued
            # — the program is expected to waveOutReset first.
            return WAVERR_STILLPLAYING
        del state["devices"][hwo]
        _wave_log(ctx, "close", hwo)
        return MMSYSERR_NOERROR

    @api.register_proc("MMSYSTEM", "waveOutGetPosition",
                       args="word ptr word", ret="word")
    def waveOutGetPosition(ctx: CallContext) -> int:
        hwo, lpmmt, cbmmt = ctx.args
        _settle(ctx)
        dev = _wave_dev(ctx, hwo)
        if dev is None:
            return MMSYSERR_INVALHANDLE
        if cbmmt < _MMTIME_VALUE + 4:
            raise NotImplementedError(
                f"waveOutGetPosition: MMTIME size {cbmmt} is too small for "
                f"wType + a DWORD value — an unknown struct layout")
        sysobj = ctx.registry.services["system"]
        want = ctx.mem.rw((lpmmt >> 16) & 0xFFFF,
                          (lpmmt + _MMTIME_TYPE) & 0xFFFF)
        # Position = how much of the written stream has PLAYED, on the virtual
        # clock: elapsed since open/reset, never past what has been queued.
        elapsed = (sysobj.tick_count() - dev["opened_ms"]) & 0xFFFFFFFF
        queued = (dev["queue_end_ms"] - dev["opened_ms"]) & 0xFFFFFFFF
        played_ms = min(elapsed, queued)
        fmt = dev["format"]
        if want == TIME_MS:
            value = played_ms
        elif want == TIME_BYTES:
            value = played_ms * fmt["avg_bytes"] // 1000
        elif want == TIME_SAMPLES:
            value = played_ms * fmt["rate"] // 1000
        else:
            raise NotImplementedError(
                f"waveOutGetPosition: MMTIME type {want:#x} is not implemented "
                f"(only TIME_MS / TIME_BYTES / TIME_SAMPLES observed)")
        _write_far_dword(ctx, lpmmt + _MMTIME_VALUE, value)
        return MMSYSERR_NOERROR

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
            print(f"[mci] open dev={dev_id} ({element})", flush=True)
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
        # Console trace of every state-changing MCI command so a live music
        # toggle shows exactly what the game sends (open/play/stop/pause/resume/
        # close) — the direct way to see whether a "disable then re-enable" even
        # issues a play, and on which device/song.
        if msg in (MCI_PLAY, MCI_STOP, MCI_PAUSE, MCI_RESUME, MCI_CLOSE):
            song = state["devices"].get(dev)
            print(f"[mci] {name} dev={dev} flags={flags:#x}"
                  f"{f' ({song})' if song else ''}", flush=True)
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

    # sndPlaySound is reachable BOTH ways: as a static import by ordinal, and
    # by name through GetProcAddress on the dynamically-loaded mmsystem.dll
    # (a program that falls back to it after waveOutOpen fails resolves it the
    # same way it resolved the waveOut procs).  One handler, both entry points
    # — registered by name too, or GetProcAddress hands back NULL and the
    # fallback silently plays nothing.
    @api.register_proc("MMSYSTEM", "sndPlaySound", args="ptr word")
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


# -- waveOut helpers ----------------------------------------------------------
def _wave_log(ctx, event: str, *args) -> None:
    """Append to the digitized-audio record (`services["sound_log"]`), the
    authoritative event log for this path — the MCI command log is a different
    device's record and stays that way."""
    sysobj = ctx.registry.services["system"]
    ctx.registry.services.setdefault("sound_log", []).append(
        (sysobj.clock_ms, "wave_" + event, args))


def _wave_state(ctx) -> dict:
    return ctx.registry.services.setdefault(
        "wave_state", {"devices": {}, "next_id": 1})


def _wave_dev(ctx, hwo: int):
    return _wave_state(ctx)["devices"].get(hwo)


def _check_wavehdr_size(cbwh: int) -> None:
    if cbwh != SIZEOF_WAVEHDR:
        raise NotImplementedError(
            f"WAVEHDR size {cbwh} is not the 16-bit sizeof(WAVEHDR) "
            f"({SIZEOF_WAVEHDR}) — an unknown header layout")


def _far_word(ctx, fp: int) -> int:
    return ctx.mem.rw((fp >> 16) & 0xFFFF, fp & 0xFFFF)


def _far_dword(ctx, fp: int) -> int:
    seg, off = (fp >> 16) & 0xFFFF, fp & 0xFFFF
    return ctx.mem.rw(seg, off) | (ctx.mem.rw(seg, (off + 2) & 0xFFFF) << 16)


def _write_far_word(ctx, fp: int, value: int) -> None:
    ctx.mem.ww((fp >> 16) & 0xFFFF, fp & 0xFFFF, value & 0xFFFF)


def _write_far_dword(ctx, fp: int, value: int) -> None:
    seg, off = (fp >> 16) & 0xFFFF, fp & 0xFFFF
    ctx.mem.ww(seg, off, value & 0xFFFF)
    ctx.mem.ww(seg, (off + 2) & 0xFFFF, (value >> 16) & 0xFFFF)


def _read_wave_format(ctx, lpfmt: int) -> dict:
    """Read a PCMWAVEFORMAT out of guest memory, faithfully — every field the
    device model needs comes from what the PROGRAM declared, never assumed."""
    if not lpfmt:
        raise NotImplementedError("waveOutOpen with a NULL format pointer")
    tag = _far_word(ctx, lpfmt + _WF_FORMATTAG)
    fmt = {
        "tag": tag,
        "channels": _far_word(ctx, lpfmt + _WF_CHANNELS),
        "rate": _far_dword(ctx, lpfmt + _WF_SAMPLESPERSEC),
        "avg_bytes": _far_dword(ctx, lpfmt + _WF_AVGBYTESPERSEC),
        "block_align": _far_word(ctx, lpfmt + _WF_BLOCKALIGN),
        # wBitsPerSample exists in PCMWAVEFORMAT; only read it for PCM.
        "bits": _far_word(ctx, lpfmt + _WF_BITSPERSAMPLE) if tag == WAVE_FORMAT_PCM else 0,
    }
    return fmt


def _buffer_duration_ms(fmt: dict, nbytes: int) -> int:
    """How long `nbytes` of this format take to play, in whole milliseconds.

    The program's OWN declared nAvgBytesPerSec is the timebase (that is what
    the device would use); rounding UP means a buffer is never reported
    finished before its last sample has been played."""
    rate = fmt["avg_bytes"]
    if rate <= 0:
        raise NotImplementedError(
            f"wave format declares nAvgBytesPerSec={rate} — cannot time a "
            f"buffer against a zero byte rate")
    return -(-nbytes * 1000 // rate)        # ceil


def _finish_buffer(ctx, lpwh: int) -> None:
    """Mark one WAVEHDR finished: out of the queue, done."""
    flags = _far_dword(ctx, lpwh + _WH_FLAGS)
    _write_far_dword(ctx, lpwh + _WH_FLAGS,
                     (flags & ~WHDR_INQUEUE) | WHDR_DONE)


def _settle(ctx) -> None:
    """Retire every queued buffer whose play time has passed, on the virtual
    clock — the header's WHDR_DONE/WHDR_INQUEUE flags and the device's
    in-flight list catch up to the same instant the scheduled MM_WOM_DONE
    becomes due.

    Called at the top of every waveOut entry point.  The notification and this
    bookkeeping are two views of one virtual-clock fact and are derived from
    the same `tick_count()`, so they can never disagree: a program that is
    told a buffer finished always finds the header marked done, whether it
    learns it from the message or by asking the device."""
    sysobj = ctx.registry.services["system"]
    now = sysobj.tick_count()
    for dev in _wave_state(ctx)["devices"].values():
        still = []
        for due_ms, lpwh in dev["pending"]:
            if _tick_ahead(due_ms, now):
                still.append((due_ms, lpwh))
            else:
                _finish_buffer(ctx, lpwh)
        dev["pending"] = still


def _abandon_pending(ctx, hwo: int, dev: dict) -> None:
    """waveOutReset: drop every queued buffer without playing the rest of it —
    each header comes back marked done and its pending MM_WOM_DONE is
    cancelled (the program is told once, by the reset returning).  Cancelling
    is scoped to THIS device (wParam), so resetting one leaves any other
    device notifying through the same window untouched."""
    sysobj = ctx.registry.services["system"]
    if dev["hwnd"]:
        sysobj.cancel_scheduled_messages(dev["hwnd"], MM_WOM_DONE, wparam=hwo)
    for _due_ms, lpwh in dev["pending"]:
        _finish_buffer(ctx, lpwh)
    dev["pending"] = []


def _wave_write(ctx, hwo: int, lpwh: int, cbwh: int) -> int:
    """waveOutWrite: queue one PCM buffer for playback.

    THE COMPLETION MODEL.  The buffer's duration follows from its size and the
    format's byte rate, so the instant it finishes is a VIRTUAL-clock instant:
    we schedule MM_WOM_DONE for it (win16 system.schedule_message) and the
    existing message machinery delivers it at that instant.  Buffers queue
    back-to-back — a write while audio is still playing starts when the queue
    ahead of it drains, exactly like a real device.

    Deliberately NOT driven by a host-audio callback: the notification's
    timing is guest-observable state (programs poll for it and free the buffer
    on it), so it must depend only on the virtual clock.  That keeps replay
    bit-reproducible and makes a machine with no audio device behave
    identically to one with speakers — host output is a pure sink."""
    dev = _wave_dev(ctx, hwo)
    if dev is None:
        return MMSYSERR_INVALHANDLE
    _check_wavehdr_size(cbwh)
    flags = _far_dword(ctx, lpwh + _WH_FLAGS)
    if not flags & WHDR_PREPARED:
        return WAVERR_UNPREPARED
    sysobj = ctx.registry.services["system"]
    fmt = dev["format"]
    lpdata = _far_dword(ctx, lpwh + _WH_LPDATA)
    nbytes = _far_dword(ctx, lpwh + _WH_BUFFERLENGTH)
    pcm = ctx.mem.block((lpdata >> 16) & 0xFFFF, lpdata & 0xFFFF, nbytes) \
        if (lpdata and nbytes) else b""

    now = sysobj.tick_count()
    # Queue back-to-back: this buffer starts when the audio already queued has
    # drained (or now, if the device has gone idle).
    start = dev["queue_end_ms"]
    if not _tick_ahead(start, now):
        start = now
    done_at = (start + _buffer_duration_ms(fmt, nbytes)) & 0xFFFFFFFF
    dev["queue_end_ms"] = done_at
    _write_far_dword(ctx, lpwh + _WH_FLAGS,
                     (flags | WHDR_INQUEUE) & ~WHDR_DONE)
    dev["pending"].append((done_at, lpwh))
    if dev["hwnd"]:
        # CALLBACK_WINDOW: the completion arrives as MM_WOM_DONE at `done_at`
        # on the virtual clock (wParam = the device, lParam = the header).
        sysobj.schedule_message(done_at, dev["hwnd"], MM_WOM_DONE, hwo, lpwh)

    _wave_log(ctx, "write", hwo, nbytes, fmt["rate"], fmt["bits"],
              fmt["channels"])
    backend = ctx.registry.services.get("sound_backend")
    if backend is not None and hasattr(backend, "play_pcm") and pcm:
        backend.play_pcm(pcm, rate=fmt["rate"], bits=fmt["bits"],
                         channels=fmt["channels"])
    return MMSYSERR_NOERROR


def _tick_ahead(a: int, b: int) -> bool:
    """Is 32-bit tick `a` strictly later than `b` (wrap-safe)?"""
    return 0 < ((a - b) & 0xFFFFFFFF) < 0x8000_0000



#: Chunk ids are four printable characters; anything else is not a chunk
#: header, which is how the walk below knows it has run off the end.
_CHUNK_ID_OK = bytes(range(0x20, 0x7F))
#: A sanity bound on the walk, so a corrupt size cannot run away.
_MAX_RIFF = 16 << 20


def riff_image_length(read_at) -> int | None:
    """The true length of the RIFF image at `read_at(pos, n) -> bytes`.

    A RIFF's own size field is not trustworthy: a program can under-declare it
    (SimAnt's SFX builder counts only the data chunk and its header, omitting
    the "WAVE" tag and the whole "fmt " chunk — 28 bytes short), yet the file
    plays correctly under Windows because the MMIO reader FINDS the chunks by
    walking them rather than by trusting the parent size.  So do the same: walk
    the subchunks and take whichever reaches further, the declared size or the
    end of the last chunk that parses.  Returns None if this is not a RIFF."""
    header = read_at(0, 12)
    if header[:4] != b"RIFF":
        return None
    declared = 8 + int.from_bytes(header[4:8], "little")
    if header[8:12] != b"WAVE":
        return declared              # some other RIFF form: take it as declared
    pos = 12
    while pos + 8 <= _MAX_RIFF:
        chunk = read_at(pos, 8)
        if not all(c in _CHUNK_ID_OK for c in chunk[:4]):
            break                    # not a chunk header: the image ended here
        size = int.from_bytes(chunk[4:8], "little")
        if size > _MAX_RIFF:
            break
        pos += 8 + size + (size & 1)          # chunks are word-aligned
    return max(declared, pos)


def _read_wav_image(ctx, ptr: int) -> bytes | None:
    """Copy a RIFF/WAV blob out of VM memory (huge-pointer safe via the
    selector-translated linear base)."""
    seg, off = (ptr >> 16) & 0xFFFF, ptr & 0xFFFF

    def read_at(pos, n):
        return ctx.mem.block(seg, (off + pos) & 0xFFFF, n)

    length = riff_image_length(read_at)
    if length is None:
        return None
    return ctx.mem.block(seg, off, length)
