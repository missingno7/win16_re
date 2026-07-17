"""Host audio output for the Win16 SOUND.DRV voice API — square-wave synthesis.

The game's sound is a stream of SOUND.DRV notes (SetVoiceNote); on a real
Windows 3.x machine with no sound card these drove the PC speaker as square
waves.  We reproduce that with pygame's mixer.  This is the presentation layer
only — mixer-flexible per the dos_re audio doctrine; the authoritative model is
the note event log (`services["sound_log"]`).  If no audio device is available
the backend degrades to a clearly-logged no-op, and the events are still
captured — never a silent fake.
"""
from __future__ import annotations

import io
import os
import struct


def riff_with_consistent_size(data: bytes) -> bytes:
    """A RIFF image whose parent size field covers its chunks.

    Strict readers (Python's `wave`, SDL_mixer, ...) clamp every subchunk to
    the parent RIFF size, so an image that under-declares it loses the tail of
    its data — SimAnt's SFX builder writes `RIFF size = data chunk + header`,
    omitting the "WAVE" tag and the "fmt " chunk, and a strict reader then
    drops the last 28 bytes of every sound.  Windows' own MMIO reader finds
    the chunks by walking and plays the whole thing, so correcting the field
    is what reproduces the original playback, not a liberty taken with the
    data: the SAMPLES are untouched.

    Returns the image unchanged when the field is already consistent (or when
    this is not a RIFF)."""
    if len(data) < 12 or data[:4] != b"RIFF":
        return data
    declared = int.from_bytes(data[4:8], "little")
    actual = len(data) - 8
    if declared == actual:
        return data
    return data[:4] + struct.pack("<I", actual) + data[8:]


def wrap_pcm_as_wav(pcm: bytes, *, rate: int, bits: int, channels: int) -> bytes:
    """Wrap raw PCM samples in a RIFF/WAVE container.

    The bridge from a wave-device buffer (raw samples + the format the program
    declared) to anything that decodes files.  WAV's own conventions decide how
    the samples are read: 8-bit is UNSIGNED (0x80 = silence), 9-bit and wider
    are signed little-endian — which is exactly the convention a Windows wave
    device applies to the same bytes, so no sample conversion happens here."""
    if channels < 1:
        raise ValueError(f"a wave format needs at least one channel, got {channels}")
    if bits not in (8, 16):
        raise ValueError(f"{bits}-bit PCM is not a WAV sample width (8 or 16)")
    if rate < 1:
        raise ValueError(f"invalid sample rate {rate}")
    block_align = channels * bits // 8
    byte_rate = rate * block_align
    fmt_chunk = struct.pack("<HHIIHH", 1, channels, rate, byte_rate,
                            block_align, bits)
    body = (b"WAVE"
            + b"fmt " + struct.pack("<I", len(fmt_chunk)) + fmt_chunk
            + b"data" + struct.pack("<I", len(pcm)) + pcm)
    if len(pcm) & 1:                    # RIFF chunks are word-aligned
        body += b"\x00"
    return b"RIFF" + struct.pack("<I", len(body)) + body


class SquareWaveBackend:
    def __init__(self, rate: int = 22050, volume: float = 0.22) -> None:
        self.rate = rate
        self.volume = volume
        self.ok = False
        self._pg = None
        self._np = None
        self._playing = None            # keep the Sound alive while it plays
        self.channels = 1
        try:
            import numpy
            import pygame
            self._np, self._pg = numpy, pygame
            pygame.mixer.quit()
            pygame.mixer.init(frequency=rate, size=-16, channels=1, buffer=1024)
            # SDL may hand back a stereo mixer regardless of the request; honour
            # whatever it actually opened when shaping sample buffers.
            init = pygame.mixer.get_init()
            if init:
                self.rate, _fmt, self.channels = init
            # Room for the looping music plus several overlapping SFX voices.
            pygame.mixer.set_num_channels(16)
            self.ok = True
            print(f"[audio] square-wave output @ {self.rate}Hz, "
                  f"{self.channels}ch", flush=True)
        except Exception as exc:  # noqa: BLE001 — a missing device is not a game bug
            print(f"[audio] output disabled (no device): "
                  f"{type(exc).__name__}: {exc}", flush=True)

    def _render(self, notes):
        """notes: list of (freq_hz, duration_ms).  freq<=0 == rest."""
        np = self._np
        chunks = []
        attack = max(int(0.004 * self.rate), 1)
        for freq, dur_ms in notes:
            n = max(int(self.rate * dur_ms / 1000.0), 1)
            if freq <= 0:
                chunks.append(np.zeros(n, dtype=np.int16))
                continue
            t = np.arange(n)
            period = self.rate / freq
            square = ((t % period) < (period / 2.0)).astype(np.float64) * 2.0 - 1.0
            env = np.ones(n)
            a = min(attack, n // 2)
            if a > 0:
                env[:a] = np.linspace(0.0, 1.0, a)
                env[-a:] = np.linspace(1.0, 0.0, a)
            amp = 32767 * self.volume
            chunks.append((square * env * amp).astype(np.int16))
        if not chunks:
            return np.zeros(0, dtype=np.int16)
        return np.concatenate(chunks)

    def play_sequence(self, notes) -> None:
        if not self.ok or not notes:
            return
        buf = self._render(notes)
        if buf.size == 0:
            return
        np = self._np
        if self.channels == 2:          # duplicate mono into a 2-D stereo array
            buf = np.column_stack((buf, buf))
        buf = np.ascontiguousarray(buf)
        self._playing = self._pg.sndarray.make_sound(buf)
        self._playing.play()

    def _decode(self, data: bytes):
        """Decode a RIFF/WAV image to a pygame Sound, cached by content so a
        rapidly-fired SFX (microman shoots on every Ins press) is decoded
        once, not per shot."""
        import hashlib
        import io
        key = hashlib.sha1(data).digest()
        cache = getattr(self, "_wav_cache", None)
        if cache is None:
            cache = self._wav_cache = {}
        snd = cache.get(key)
        if snd is None:
            snd = self._pg.mixer.Sound(
                file=io.BytesIO(riff_with_consistent_size(data)))
            cache[key] = snd
        return snd

    def play_wav(self, data: bytes, *, loop: bool = False) -> None:
        """Play a RIFF/WAV image (MMSYSTEM sndPlaySound path).  Looping sounds
        are treated as background MUSIC (a new one replaces the old); one-shots
        are SFX that mix on any free channel.  pygame resamples to the open
        output format."""
        if not self.ok or not data:
            return
        try:
            snd = self._decode(data)
        except Exception as exc:  # noqa: BLE001 — a bad WAV is not a game bug
            print(f"[audio] WAV decode failed: {type(exc).__name__}: {exc}",
                  flush=True)
            return
        if loop:
            music = getattr(self, "_music", None)
            if music is not None:
                music.stop()
            self._music = snd
            snd.play(loops=-1)
        else:
            # Keep the last few SFX referenced so pygame doesn't stop a sound
            # whose only Python reference was dropped while it still plays.
            live = getattr(self, "_sfx_live", None)
            if live is None:
                live = self._sfx_live = []
            live.append(snd)
            del live[:-8]
            snd.play()

    def play_pcm(self, pcm: bytes, *, rate: int, bits: int,
                 channels: int) -> None:
        """Play one raw PCM buffer (the MMSYSTEM waveOutWrite path).

        Pure SINK: the wave device's model — when the buffer finishes, what
        waveOutGetPosition reports — lives on the virtual clock in
        win16/api/mmsystem.py and never consults this.  Dropping every call
        here changes nothing a program can observe.

        The buffer arrives in the format the program DECLARED to waveOutOpen
        (SimAnt: 4096 Hz mono 8-bit — a rate no host mixer opens natively), so
        it is wrapped in a RIFF/WAVE image and handed to the existing decode
        path, which resamples to the output device."""
        if not self.ok or not pcm:
            return
        try:
            wav = wrap_pcm_as_wav(pcm, rate=rate, bits=bits, channels=channels)
        except ValueError as exc:
            print(f"[audio] PCM buffer not playable: {exc}", flush=True)
            return
        self.play_wav(wav)

    def stop_pcm(self) -> None:
        """waveOutReset — cut off the wave buffers still sounding."""
        live = getattr(self, "_sfx_live", None)
        if self.ok and live:
            for snd in live:
                snd.stop()
            live.clear()

    def stop_wav(self) -> None:
        """sndPlaySound(NULL) — stop the background music (SFX are one-shots
        that finish on their own)."""
        music = getattr(self, "_music", None)
        if self.ok and music is not None:
            music.stop()
            self._music = None

    def stop(self) -> None:
        if self.ok:
            self._pg.mixer.stop()

    def close(self) -> None:
        if self.ok:
            try:
                self._pg.mixer.quit()
            except Exception:  # noqa: BLE001
                pass
            self.ok = False


# --- Windows 3.x MIDI Mapper level filtering ---------------------------------
# MIDI files authored for Windows 3.x follow the Microsoft MIDI Mapper
# authoring guideline: the same music appears TWICE in one file — an
# "extended level" arrangement on channels 1-10 (percussion on channel 10)
# and a "base level" duplicate on channels 13-16 (percussion on channel 16).
# The MCI sequencer routed playback through the MIDI Mapper, whose setup
# passed only ONE of the two levels to the synthesizer.  A modern General
# MIDI synth (our SDL_mixer stream) has no Mapper and plays both: every
# melody doubles, and the base-level percussion channel 16 renders as a
# *melodic* GM instrument — the SimAnt soundtrack sets program 126
# ("Applause") there, heard as a constant re-triggering hiss riding over the
# music.  Playing the extended level only (channels 1-10, which line up with
# General MIDI, percussion on 10) reproduces what the original setup played.

EXTENDED_LEVEL_CHANNELS = frozenset(range(0, 10))   # 0-based: channels 1-10
BASE_LEVEL_CHANNELS = frozenset(range(12, 16))       # 0-based: channels 13-16
MAPPER_LEVELS = {"extended": EXTENDED_LEVEL_CHANNELS,
                 "base": BASE_LEVEL_CHANNELS}


def _read_vlq(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    while True:
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, pos


def _write_vlq(value: int) -> bytes:
    out = bytearray([value & 0x7F])
    value >>= 7
    while value:
        out.insert(0, 0x80 | (value & 0x7F))
        value >>= 7
    return bytes(out)


def midi_keep_channels(data: bytes, keep: frozenset[int]) -> bytes:
    """Rewrite a Standard MIDI File keeping only channel-voice events on the
    0-based channels in `keep`.  Deltas of dropped events fold into the next
    kept event, so every surviving event keeps its absolute time; meta and
    sysex events are preserved verbatim.  The output never relies on running
    status.  Raises ValueError on anything that is not a well-formed SMF."""
    if data[:4] != b"MThd":
        raise ValueError("not a Standard MIDI File (no MThd)")
    header_len = int.from_bytes(data[4:8], "big")
    ntrks = int.from_bytes(data[10:12], "big")
    out = bytearray(data[:8 + header_len])
    pos = 8 + header_len
    for _ in range(ntrks):
        if data[pos:pos + 4] != b"MTrk":
            raise ValueError(f"bad track chunk at {pos:#x}")
        length = int.from_bytes(data[pos + 4:pos + 8], "big")
        p, end = pos + 8, pos + 8 + length
        pos = end
        body = bytearray()
        running: int | None = None
        pending = 0                       # accumulated delta of dropped events
        while p < end:
            delta, p = _read_vlq(data, p)
            pending += delta
            byte = data[p]
            if byte == 0xFF:              # meta event: keep verbatim
                ln, q = _read_vlq(data, p + 2)
                event = data[p:q + ln]
                p = q + ln
                running = None
            elif byte in (0xF0, 0xF7):    # sysex: keep verbatim
                ln, q = _read_vlq(data, p + 1)
                event = data[p:q + ln]
                p = q + ln
                running = None
            else:                         # channel voice message
                if byte & 0x80:
                    status = running = byte
                    p += 1
                elif running is None:
                    raise ValueError(f"dangling running status at {p:#x}")
                else:
                    status = running
                nparams = 1 if status & 0xF0 in (0xC0, 0xD0) else 2
                params = data[p:p + nparams]
                p += nparams
                if status & 0x0F not in keep:
                    continue              # dropped: delta stays pending
                event = bytes([status]) + params
            body += _write_vlq(pending)
            body += event
            pending = 0
        out += b"MTrk" + len(body).to_bytes(4, "big") + bytes(body)
    return bytes(out)


class MidiBackend:
    """Plays the game's `.mid` songs through the host MIDI synth (pygame /
    SDL_mixer's dedicated music stream).  Driven by the MMSYSTEM MCI layer's
    open/play/stop/close (win16/api/mmsystem.py) — presentation only; the
    deterministic record is `services["mci_log"]`, so audio never affects state.

    Reuses whatever mixer a SquareWaveBackend already opened; the music stream
    is independent of the SFX channels, so both play at once.

    `level` selects which MIDI Mapper device level to emulate ("extended",
    the default — the best-quality authoring intent, channels 1-10 aligned
    with General MIDI — or "base", channels 13-16)."""

    def __init__(self, level: str = "extended") -> None:
        self.ok = False
        self._pg = None
        self._devices: dict[int, str] = {}      # MCI device id -> host .mid path
        self._filtered: dict[str, bytes] = {}   # host path -> Mapper-filtered SMF
        self._keep = MAPPER_LEVELS[level]       # KeyError on an unknown level
        self._playing = None
        try:
            import pygame
            self._pg = pygame
            if pygame.mixer.get_init() is None:
                pygame.mixer.init()
            self.ok = True
            print(f"[audio] MIDI music via SDL_mixer "
                  f"(MIDI Mapper {level}-level filter)", flush=True)
        except Exception as exc:  # noqa: BLE001 — no synth is not a game bug
            print(f"[audio] MIDI music disabled: {type(exc).__name__}: {exc}",
                  flush=True)

    def _mapper_filtered(self, path: str) -> bytes:
        """The Mapper-level view of the song at `path`, filtered once and
        cached.  A file the SMF filter cannot parse plays unfiltered — loudly:
        the anomaly is printed with the reason, never swallowed."""
        payload = self._filtered.get(path)
        if payload is None:
            with open(path, "rb") as fh:
                raw = fh.read()
            try:
                payload = midi_keep_channels(raw, self._keep)
            except (ValueError, IndexError) as exc:
                print(f"[audio] MIDI Mapper filter skipped for "
                      f"{os.path.basename(path)} ({exc}) — playing the file "
                      f"as-is", flush=True)
                payload = raw
            self._filtered[path] = payload
        return payload

    def open(self, dev_id: int, host_path: str | None) -> None:
        self._devices[dev_id] = host_path

    def play(self, dev_id: int) -> None:
        path = self._devices.get(dev_id)
        if not self.ok or not path:
            return
        music = self._pg.mixer.music
        # Real MCI semantics: MCI_PLAY on a device that is already playing
        # CONTINUES the current playback — it does not reload and restart from
        # the top.  Only (re)start when this device's song is not currently
        # sounding (a fresh play, or the game looping it after it ended).
        # Without this guard a game that re-issues MCI_PLAY while polling status
        # (SimAnt's music service loop) restarts the song every poll, which is
        # heard as the same clip stuttering "over and over".
        if self._playing == dev_id and music.get_busy():
            return
        try:
            # The MIDI Mapper level filter runs on the SMF image; SDL_mixer
            # gets the filtered bytes (BytesIO + namehint, no temp files).
            music.load(io.BytesIO(self._mapper_filtered(path)), namehint="mid")
            music.play()
            self._playing = dev_id
            self._plays = getattr(self, "_plays", 0) + 1
            # Per-(re)start log: if a song shows up here repeatedly in quick
            # succession, that is the repeat to chase (name + running count).
            print(f"[audio] MIDI play #{self._plays}: {os.path.basename(path)}",
                  flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[audio] MIDI play failed: {exc}", flush=True)

    def stop(self, dev_id: int) -> None:
        if self.ok and self._playing == dev_id:
            self._pg.mixer.music.stop()
            self._playing = None

    def close(self, dev_id: int) -> None:
        self.stop(dev_id)
        self._devices.pop(dev_id, None)

    def is_playing(self, dev_id: int) -> bool:
        return bool(self.ok and self._playing == dev_id
                    and self._pg.mixer.music.get_busy())

    def shutdown(self) -> None:
        if self.ok:
            try:
                self._pg.mixer.music.stop()
            except Exception:  # noqa: BLE001
                pass
