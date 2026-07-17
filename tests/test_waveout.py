"""The MMSYSTEM waveOut device: format handling, the RIFF wrapper, fail-loud
gaps, and — the load-bearing property — DETERMINISTIC completion.

A wave buffer finishes after a length of TIME.  If that instant came from a
host-audio callback, the moment MM_WOM_DONE lands (and therefore when the
program frees the buffer and what it does next) would depend on host audio
scheduling, and replay would stop being reproducible.  It comes from the
virtual clock instead: duration = bytes / byte-rate, and the notification is
scheduled on the same GetTickCount timebase the program itself reads.  These
tests pin that: identical behaviour with no audio device, with a backend that
records nothing, and across repeated runs.
"""
import struct

import pytest

from win16.api import mmsystem
from win16.api.mmsystem import (
    CALLBACK_WINDOW, MM_WOM_DONE, MMSYSERR_INVALHANDLE, MMSYSERR_NOERROR,
    SIZEOF_WAVEHDR, TIME_MS, WAVERR_STILLPLAYING, WAVERR_UNPREPARED,
    WAVE_MAPPER, WHDR_DONE, WHDR_INQUEUE, WHDR_PREPARED,
    _WH_BUFFERLENGTH, _WH_FLAGS, _WH_LPDATA, _buffer_duration_ms,
)
from win16.audio import wrap_pcm_as_wav

# A scratch guest address space: one segment of bytes, far pointers into it.
SEG = 0x1000
FMT_OFF = 0x0000        # PCMWAVEFORMAT
HDR_OFF = 0x0100        # WAVEHDR
DATA_OFF = 0x0200       # PCM samples
HWO_OFF = 0x0080        # the HWAVEOUT out-parameter
MMT_OFF = 0x00C0        # MMTIME
HWND = 0x0111


class FakeMem:
    def __init__(self):
        self.b = bytearray(0x10000)

    def rb(self, seg, off):
        return self.b[off & 0xFFFF]

    def wb(self, seg, off, v):
        self.b[off & 0xFFFF] = v & 0xFF

    def rw(self, seg, off):
        return int.from_bytes(self.b[off & 0xFFFF:(off & 0xFFFF) + 2], "little")

    def ww(self, seg, off, v):
        self.b[off & 0xFFFF:(off & 0xFFFF) + 2] = (v & 0xFFFF).to_bytes(2, "little")

    def block(self, seg, off, n):
        return bytes(self.b[off & 0xFFFF:(off & 0xFFFF) + n])


class FakeSystem:
    """Just enough Win16System to drive the device: a settable virtual clock
    plus the real scheduled-message machinery."""

    def __init__(self):
        self.now = 1000
        self.clock_ms = 1000
        self.msg_queue = []
        self.scheduled_messages = []

    def tick_count(self):
        return self.now & 0xFFFFFFFF

    def post_message(self, hwnd, msg, wparam, lparam):
        self.msg_queue.append((hwnd, msg, wparam, lparam, self.clock_ms, 0))

    # the real implementations, bound to this fake
    def _release_due_messages(self):
        from win16.api.system import Win16System
        return Win16System._release_due_messages(self)

    def cancel_scheduled_messages(self, hwnd, msg, wparam=None):
        from win16.api.system import Win16System
        return Win16System.cancel_scheduled_messages(self, hwnd, msg, wparam)

    def schedule_message(self, due_ms, hwnd, msg, wparam, lparam):
        from win16.api.system import Win16System
        return Win16System.schedule_message(self, due_ms, hwnd, msg, wparam, lparam)


class FakeRegistry:
    def __init__(self):
        self.services = {}
        self.named_procs = {}

    def register_proc(self, module, name, args="", ret="word"):
        def deco(fn):
            self.named_procs[name] = fn
            return fn
        return deco

    def register(self, module, ordinal, name=None, args="", ret="word"):
        def deco(fn):
            return fn
        return deco

    provided_dlls = set()


class Ctx:
    def __init__(self, registry, mem, args):
        self.registry, self.mem, self.args = registry, mem, args

    def read_string(self, ptr):
        return b""


@pytest.fixture
def dev():
    """An installed MMSYSTEM surface over a fake machine."""
    reg = FakeRegistry()
    mmsystem.install(reg)
    mem = FakeMem()
    sysobj = FakeSystem()
    reg.services["system"] = sysobj

    def call(name, *args):
        return reg.named_procs[name](Ctx(reg, mem, list(args)))

    return dict(reg=reg, mem=mem, sys=sysobj, call=call)


def _far(off):
    return (SEG << 16) | off


def _ask_position(d, hwo, want=TIME_MS):
    """Ask the device where it is, the way a program does: declare which
    MMTIME unit you want in wType, then read the value back."""
    d["mem"].ww(SEG, MMT_OFF, want)
    rc = d["call"]("waveOutGetPosition", hwo, _far(MMT_OFF), 8)
    return rc, int.from_bytes(d["mem"].b[MMT_OFF + 2:MMT_OFF + 6], "little")


def _put_format(mem, *, tag=1, channels=1, rate=4096, avg=None, align=1, bits=8):
    """Write a PCMWAVEFORMAT the way a program would."""
    if avg is None:
        avg = rate * align
    mem.b[FMT_OFF:FMT_OFF + 16] = struct.pack(
        "<HHIIHH", tag, channels, rate, avg, align, bits)


def _put_header(mem, nbytes, *, flags=0):
    mem.b[HDR_OFF:HDR_OFF + SIZEOF_WAVEHDR] = bytes(SIZEOF_WAVEHDR)
    mem.b[HDR_OFF + _WH_LPDATA:HDR_OFF + _WH_LPDATA + 4] = \
        _far(DATA_OFF).to_bytes(4, "little")
    mem.b[HDR_OFF + _WH_BUFFERLENGTH:HDR_OFF + _WH_BUFFERLENGTH + 4] = \
        nbytes.to_bytes(4, "little")
    mem.b[HDR_OFF + _WH_FLAGS:HDR_OFF + _WH_FLAGS + 4] = flags.to_bytes(4, "little")


def _hdr_flags(mem):
    return int.from_bytes(mem.b[HDR_OFF + _WH_FLAGS:HDR_OFF + _WH_FLAGS + 4], "little")


def _open(d, **kw):
    _put_format(d["mem"], **kw)
    rc = d["call"]("waveOutOpen", _far(HWO_OFF), WAVE_MAPPER, _far(FMT_OFF),
                   HWND, 0, CALLBACK_WINDOW)
    assert rc == MMSYSERR_NOERROR
    return d["mem"].rw(SEG, HWO_OFF)


def _write(d, hwo, nbytes, fill=0x80):
    d["mem"].b[DATA_OFF:DATA_OFF + nbytes] = bytes([fill]) * nbytes
    _put_header(d["mem"], nbytes)
    assert d["call"]("waveOutPrepareHeader", hwo, _far(HDR_OFF),
                     SIZEOF_WAVEHDR) == MMSYSERR_NOERROR
    return d["call"]("waveOutWrite", hwo, _far(HDR_OFF), SIZEOF_WAVEHDR)


# -- the format the PROGRAM declares is what the device uses ------------------
def test_open_reads_the_declared_format_from_guest_memory(dev):
    hwo = _open(dev, rate=4096, bits=8, channels=1, align=1)
    fmt = dev["reg"].services["wave_state"]["devices"][hwo]["format"]
    assert (fmt["rate"], fmt["bits"], fmt["channels"], fmt["avg_bytes"]) == \
        (4096, 8, 1, 4096)


def test_open_reports_the_device_and_returns_a_handle(dev):
    assert dev["call"]("waveOutGetNumDevs") == 1
    hwo = _open(dev)
    assert hwo != 0


def test_open_rejects_a_non_pcm_format_loudly(dev):
    _put_format(dev["mem"], tag=2)          # ADPCM: we implement no codec
    with pytest.raises(NotImplementedError, match="only PCM"):
        dev["call"]("waveOutOpen", _far(HWO_OFF), WAVE_MAPPER, _far(FMT_OFF),
                    HWND, 0, CALLBACK_WINDOW)


def test_open_rejects_an_unimplemented_callback_type_loudly(dev):
    _put_format(dev["mem"])
    with pytest.raises(NotImplementedError, match="callback type"):
        dev["call"]("waveOutOpen", _far(HWO_OFF), WAVE_MAPPER, _far(FMT_OFF),
                    HWND, 0, 0x00030000)    # CALLBACK_FUNCTION
    with pytest.raises(NotImplementedError, match="open flags"):
        dev["call"]("waveOutOpen", _far(HWO_OFF), WAVE_MAPPER, _far(FMT_OFF),
                    HWND, 0, CALLBACK_WINDOW | 0x8)     # WAVE_ALLOWSYNC


def test_a_bad_wavehdr_size_is_a_loud_gap_not_a_guess(dev):
    hwo = _open(dev)
    _put_header(dev["mem"], 16)
    with pytest.raises(NotImplementedError, match="sizeof\\(WAVEHDR\\)"):
        dev["call"]("waveOutPrepareHeader", hwo, _far(HDR_OFF), 20)


def test_calls_on_an_unknown_device_report_a_bad_handle(dev):
    assert dev["call"]("waveOutClose", 0x999) == MMSYSERR_INVALHANDLE
    assert dev["call"]("waveOutReset", 0x999) == MMSYSERR_INVALHANDLE


def test_write_requires_a_prepared_header(dev):
    hwo = _open(dev)
    _put_header(dev["mem"], 64)             # never prepared
    assert dev["call"]("waveOutWrite", hwo, _far(HDR_OFF),
                       SIZEOF_WAVEHDR) == WAVERR_UNPREPARED


# -- duration is arithmetic on the declared byte rate -------------------------
def test_buffer_duration_uses_the_declared_byte_rate():
    fmt = {"avg_bytes": 4096}
    assert _buffer_duration_ms(fmt, 4096) == 1000       # exactly one second
    assert _buffer_duration_ms(fmt, 472) == 116         # 115.2ms -> rounds UP
    assert _buffer_duration_ms(fmt, 0) == 0


def test_buffer_duration_refuses_a_zero_byte_rate(dev):
    with pytest.raises(NotImplementedError, match="zero byte rate"):
        _buffer_duration_ms({"avg_bytes": 0}, 100)


# -- THE COMPLETION MODEL -----------------------------------------------------
def test_completion_is_scheduled_on_the_virtual_clock_not_a_host_callback(dev):
    hwo = _open(dev, rate=4096)
    dev["sys"].now = 5000
    assert _write(dev, hwo, 4096) == MMSYSERR_NOERROR   # exactly 1000 ms
    # Nothing is delivered yet, and NOTHING about the host decides when.
    assert dev["sys"].msg_queue == []
    assert len(dev["sys"].scheduled_messages) == 1
    due, hwnd, msg, wparam, lparam = dev["sys"].scheduled_messages[0]
    assert (due, hwnd, msg, wparam, lparam) == \
        (6000, HWND, MM_WOM_DONE, hwo, _far(HDR_OFF))


def test_mm_wom_done_arrives_exactly_when_the_buffer_finishes(dev):
    hwo = _open(dev, rate=4096)
    dev["sys"].now = 5000
    _write(dev, hwo, 4096)                              # 1000 ms of audio

    dev["sys"].now = 5999                               # one ms short
    dev["sys"]._release_due_messages()
    assert dev["sys"].msg_queue == []

    dev["sys"].now = 6000                               # due
    dev["sys"]._release_due_messages()
    assert len(dev["sys"].msg_queue) == 1
    hwnd, msg, wparam, lparam, _t, _p = dev["sys"].msg_queue[0]
    assert (hwnd, msg, wparam, lparam) == (HWND, MM_WOM_DONE, hwo, _far(HDR_OFF))


def test_the_header_is_marked_done_at_the_same_instant(dev):
    hwo = _open(dev, rate=4096)
    dev["sys"].now = 5000
    _write(dev, hwo, 4096)
    assert _hdr_flags(dev["mem"]) & WHDR_INQUEUE
    assert not _hdr_flags(dev["mem"]) & WHDR_DONE

    dev["sys"].now = 5999
    _ask_position(dev, hwo)                            # settles
    assert _hdr_flags(dev["mem"]) & WHDR_INQUEUE       # still playing

    dev["sys"].now = 6000
    _ask_position(dev, hwo)
    flags = _hdr_flags(dev["mem"])
    assert flags & WHDR_DONE and not flags & WHDR_INQUEUE


def test_buffers_queue_back_to_back(dev):
    hwo = _open(dev, rate=4096)
    dev["sys"].now = 1000
    _write(dev, hwo, 4096)                              # 1000..2000
    _write(dev, hwo, 4096)                              # starts when #1 drains
    dues = sorted(s[0] for s in dev["sys"].scheduled_messages)
    assert dues == [2000, 3000]


def test_a_write_into_an_idle_device_starts_now_not_in_the_past(dev):
    hwo = _open(dev, rate=4096)
    dev["sys"].now = 1000
    _write(dev, hwo, 4096)                              # done at 2000
    dev["sys"].now = 9000                               # long since drained
    _write(dev, hwo, 4096)
    assert max(s[0] for s in dev["sys"].scheduled_messages) == 10000


def test_completion_is_reproducible_across_runs(dev):
    """The same clock + the same writes give the same arrival — the property
    replay depends on."""
    def run():
        reg = FakeRegistry()
        mmsystem.install(reg)
        mem, sysobj = FakeMem(), FakeSystem()
        reg.services["system"] = sysobj
        d = dict(reg=reg, mem=mem, sys=sysobj,
                 call=lambda n, *a: reg.named_procs[n](Ctx(reg, mem, list(a))))
        hwo = _open(d, rate=4096)
        for now, n in ((1000, 472), (1050, 1830), (4000, 364)):
            sysobj.now = now
            _write(d, hwo, n)
        return sorted(s[0] for s in sysobj.scheduled_messages)

    assert run() == run() == run()


def test_a_host_backend_cannot_influence_the_guest_timeline(dev):
    """Host audio is a pure sink: an installed backend — even a broken one —
    changes nothing the program can observe."""
    def timeline(backend):
        reg = FakeRegistry()
        mmsystem.install(reg)
        mem, sysobj = FakeMem(), FakeSystem()
        reg.services["system"] = sysobj
        if backend is not None:
            reg.services["sound_backend"] = backend
        d = dict(reg=reg, mem=mem, sys=sysobj,
                 call=lambda n, *a: reg.named_procs[n](Ctx(reg, mem, list(a))))
        hwo = _open(d, rate=4096)
        sysobj.now = 2500
        _write(d, hwo, 2048)
        return (hwo, sorted(sysobj.scheduled_messages), _hdr_flags(mem))

    class Slow:
        """A backend that "takes time" and mangles what it is given."""
        def play_pcm(self, pcm, *, rate, bits, channels):
            list(range(10000))

    class Silent:
        def play_pcm(self, pcm, *, rate, bits, channels):
            pass

    assert timeline(None) == timeline(Slow()) == timeline(Silent())


def test_the_pcm_handed_to_the_backend_is_the_guest_buffer(dev):
    got = []

    class Cap:
        def play_pcm(self, pcm, *, rate, bits, channels):
            got.append((bytes(pcm), rate, bits, channels))

    dev["reg"].services["sound_backend"] = Cap()
    hwo = _open(dev, rate=4096, bits=8, channels=1)
    _write(dev, hwo, 64, fill=0x33)
    assert got == [(bytes([0x33]) * 64, 4096, 8, 1)]


def test_the_wave_path_is_logged_in_the_digitized_audio_record(dev):
    """sound_log is the authoritative record for this device — and the MCI
    command log is a different device's record, so it stays empty."""
    hwo = _open(dev, rate=4096)
    _write(dev, hwo, 472)
    dev["call"]("waveOutReset", hwo)
    dev["call"]("waveOutClose", hwo)
    log = dev["reg"].services["sound_log"]
    assert [e[1] for e in log] == ["wave_open", "wave_write", "wave_reset",
                                   "wave_close"]
    assert log[0][2] == (hwo, 4096, 8, 1)               # rate/bits/channels
    assert log[1][2] == (hwo, 472, 4096, 8, 1)          # + the byte count
    assert "mci_log" not in dev["reg"].services


# -- position -----------------------------------------------------------------
def test_position_reports_what_has_played_never_more_than_was_queued(dev):
    hwo = _open(dev, rate=4096)
    dev["sys"].now = 1000
    _write(dev, hwo, 4096)                              # 1000 ms queued

    for now, expect in ((1000, 0), (1400, 400), (2000, 1000), (9999, 1000)):
        dev["sys"].now = now
        rc, ms = _ask_position(dev, hwo)
        assert rc == MMSYSERR_NOERROR
        assert ms == expect, f"at {now}: {ms} != {expect}"


def test_position_rejects_an_unimplemented_mmtime_type_loudly(dev):
    hwo = _open(dev)
    dev["mem"].ww(SEG, MMT_OFF, 0x0008)                 # TIME_SMPTE
    with pytest.raises(NotImplementedError, match="MMTIME type"):
        dev["call"]("waveOutGetPosition", hwo, _far(MMT_OFF), 8)


# -- reset / unprepare / close ------------------------------------------------
def test_reset_abandons_the_queue_and_cancels_its_completion(dev):
    hwo = _open(dev, rate=4096)
    dev["sys"].now = 1000
    _write(dev, hwo, 4096)
    dev["sys"].now = 1100                               # 100ms in, still playing
    assert dev["call"]("waveOutReset", hwo) == MMSYSERR_NOERROR
    assert dev["sys"].scheduled_messages == []          # no late MM_WOM_DONE
    flags = _hdr_flags(dev["mem"])
    assert flags & WHDR_DONE and not flags & WHDR_INQUEUE
    # ...and the abandoned buffer never arrives, however far the clock runs.
    dev["sys"].now = 99_000
    dev["sys"]._release_due_messages()
    assert dev["sys"].msg_queue == []


def test_reset_of_one_device_leaves_another_on_the_same_window_alone(dev):
    a = _open(dev, rate=4096)
    b = _open(dev, rate=4096)
    dev["sys"].now = 1000
    _write(dev, a, 4096)
    _write(dev, b, 4096)
    dev["call"]("waveOutReset", a)
    assert [s[3] for s in dev["sys"].scheduled_messages] == [b]


def test_unprepare_refuses_a_buffer_still_in_the_queue(dev):
    hwo = _open(dev, rate=4096)
    dev["sys"].now = 1000
    _write(dev, hwo, 4096)
    dev["sys"].now = 1500                               # mid-play
    assert dev["call"]("waveOutUnprepareHeader", hwo, _far(HDR_OFF),
                       SIZEOF_WAVEHDR) == WAVERR_STILLPLAYING
    dev["sys"].now = 2000                               # finished
    assert dev["call"]("waveOutUnprepareHeader", hwo, _far(HDR_OFF),
                       SIZEOF_WAVEHDR) == MMSYSERR_NOERROR
    assert not _hdr_flags(dev["mem"]) & WHDR_PREPARED


def test_close_refuses_while_a_buffer_is_still_queued(dev):
    hwo = _open(dev, rate=4096)
    dev["sys"].now = 1000
    _write(dev, hwo, 4096)
    dev["sys"].now = 1500
    assert dev["call"]("waveOutClose", hwo) == WAVERR_STILLPLAYING
    dev["call"]("waveOutReset", hwo)
    assert dev["call"]("waveOutClose", hwo) == MMSYSERR_NOERROR
    assert dev["call"]("waveOutClose", hwo) == MMSYSERR_INVALHANDLE


def test_reset_restarts_the_position(dev):
    hwo = _open(dev, rate=4096)
    dev["sys"].now = 1000
    _write(dev, hwo, 4096)
    dev["sys"].now = 1400
    dev["call"]("waveOutReset", hwo)
    assert _ask_position(dev, hwo)[1] == 0


# -- the sndPlaySound fallback ------------------------------------------------
# A program that cannot open the wave device falls back to handing MMSYSTEM a
# complete RIFF/WAVE image in memory.  It resolves sndPlaySound the same way it
# resolved the waveOut procs — GetProcAddress by NAME — so a by-ordinal-only
# registration hands it NULL and the fallback plays nothing at all.
def test_sndplaysound_is_resolvable_by_name(dev):
    assert "sndPlaySound" in dev["reg"].named_procs


def test_sndplaysound_snd_memory_parses_the_riff_image_and_plays_it(dev):
    played = []

    class Cap:
        def play_wav(self, data, *, loop=False):
            played.append((bytes(data), loop))

        def stop_wav(self):
            played.append(("stop", None))

    dev["reg"].services["sound_backend"] = Cap()
    pcm = bytes(range(64))
    img = wrap_pcm_as_wav(pcm, rate=4096, bits=8, channels=1)
    dev["mem"].b[DATA_OFF:DATA_OFF + len(img)] = img

    SND_MEMORY_NOSTOP = 0x0004 | 0x0010
    assert dev["call"]("sndPlaySound", _far(DATA_OFF), SND_MEMORY_NOSTOP) == 1
    assert played == [(img, False)]                 # the whole image, verbatim
    log = dev["reg"].services["sound_log"]
    assert log[-1][1] == "wav" and log[-1][2][1] == SND_MEMORY_NOSTOP


def test_sndplaysound_snd_memory_copies_exactly_the_image(dev):
    """How much is copied out of guest memory comes from the image itself —
    never a guess, and never past the blob."""
    img = wrap_pcm_as_wav(bytes(100), rate=4096, bits=8, channels=1)
    dev["mem"].b[DATA_OFF:DATA_OFF + len(img)] = img
    dev["mem"].b[DATA_OFF + len(img):DATA_OFF + len(img) + 32] = b"\xEE" * 32

    got = []
    dev["reg"].services["sound_backend"] = SimpleNamespaceBackend(got)
    dev["call"]("sndPlaySound", _far(DATA_OFF), 0x0004)
    assert got[0] == img and b"\xEE" not in got[0]


def test_a_riff_that_under_declares_its_size_is_read_whole(dev):
    """A program can write a WRONG parent size: SimAnt's SFX builder counts
    only the data chunk and its header, leaving out the "WAVE" tag and the
    "fmt " chunk (28 bytes short).  Windows' MMIO reader finds the chunks by
    walking, so the whole image plays — trusting the field would silently clip
    the tail off every sound."""
    img = bytearray(wrap_pcm_as_wav(bytes(range(200)), rate=4096, bits=8,
                                    channels=1))
    true_len = len(img)
    img[4:8] = (len(img) - 8 - 28).to_bytes(4, "little")     # the game's bug
    dev["mem"].b[DATA_OFF:DATA_OFF + len(img)] = img
    dev["mem"].b[DATA_OFF + len(img):DATA_OFF + len(img) + 64] = b"\x00" * 64

    got = []
    dev["reg"].services["sound_backend"] = SimpleNamespaceBackend(got)
    dev["call"]("sndPlaySound", _far(DATA_OFF), 0x0004)
    assert len(got[0]) == true_len              # the whole image, not 28 short
    assert got[0] == bytes(img)


def test_riff_image_length_walks_the_chunks():
    from win16.api.mmsystem import riff_image_length

    img = bytearray(wrap_pcm_as_wav(bytes(64), rate=4096, bits=8, channels=1))
    # trailing garbage must not be mistaken for another chunk
    blob = bytes(img) + b"\xFF" * 64

    def read_at(pos, n):
        return blob[pos:pos + n].ljust(n, b"\x00")

    assert riff_image_length(read_at) == len(img)

    img[4:8] = (len(img) - 8 - 28).to_bytes(4, "little")
    blob = bytes(img) + b"\x00" * 64
    assert riff_image_length(read_at) == len(img)        # walk beats the field


def test_riff_image_length_rejects_a_non_riff():
    from win16.api.mmsystem import riff_image_length
    blob = b"NOPE" + bytes(64)
    assert riff_image_length(lambda p, n: blob[p:p + n]) is None


def test_sndplaysound_null_stops_the_current_sound(dev):
    stopped = []

    class Cap:
        def play_wav(self, data, *, loop=False):
            stopped.append("play")

        def stop_wav(self):
            stopped.append("stop")

    dev["reg"].services["sound_backend"] = Cap()
    assert dev["call"]("sndPlaySound", 0, 0) == 1
    assert stopped == ["stop"]


class SimpleNamespaceBackend:
    def __init__(self, sink):
        self.sink = sink

    def play_wav(self, data, *, loop=False):
        self.sink.append(bytes(data))

    def stop_wav(self):
        pass


# -- the RIFF wrapper (the host-playback bridge) ------------------------------
def test_wrap_pcm_as_wav_round_trips_through_the_wave_module():
    import io
    import wave
    pcm = bytes(range(256))
    img = wrap_pcm_as_wav(pcm, rate=4096, bits=8, channels=1)
    w = wave.open(io.BytesIO(img))
    assert (w.getframerate(), w.getsampwidth(), w.getnchannels()) == (4096, 1, 1)
    assert w.readframes(w.getnframes()) == pcm


def test_wrap_pcm_as_wav_pads_an_odd_length_chunk():
    img = wrap_pcm_as_wav(b"\x01\x02\x03", rate=4096, bits=8, channels=1)
    assert len(img) % 2 == 0
    assert int.from_bytes(img[4:8], "little") == len(img) - 8


def test_wrap_pcm_as_wav_refuses_a_width_wav_cannot_carry():
    for bits in (0, 4, 12, 24):
        with pytest.raises(ValueError, match="not a WAV sample width"):
            wrap_pcm_as_wav(b"\x00", rate=4096, bits=bits, channels=1)


# -- the host-decoder bridge --------------------------------------------------
def test_riff_with_consistent_size_lets_a_strict_reader_see_every_sample():
    """A strict reader clamps the data chunk to the parent size, so an image
    that under-declares it loses its tail.  Correcting the FIELD (never a
    sample) is what makes the host play what Windows played."""
    import io
    import wave
    from win16.audio import riff_with_consistent_size

    pcm = bytes(range(200))
    img = bytearray(wrap_pcm_as_wav(pcm, rate=4096, bits=8, channels=1))
    img[4:8] = (len(img) - 8 - 28).to_bytes(4, "little")

    clipped = wave.open(io.BytesIO(bytes(img)))
    assert len(clipped.readframes(clipped.getnframes())) == len(pcm) - 28

    fixed = wave.open(io.BytesIO(riff_with_consistent_size(bytes(img))))
    assert fixed.readframes(fixed.getnframes()) == pcm      # every sample


def test_riff_with_consistent_size_leaves_a_correct_image_alone():
    from win16.audio import riff_with_consistent_size
    img = wrap_pcm_as_wav(bytes(64), rate=4096, bits=8, channels=1)
    assert riff_with_consistent_size(img) is img
    assert riff_with_consistent_size(b"NOPE") == b"NOPE"    # not a RIFF
