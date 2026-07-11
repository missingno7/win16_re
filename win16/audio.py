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
            snd = self._pg.mixer.Sound(file=io.BytesIO(data))
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


class MidiBackend:
    """Plays the game's `.mid` songs through the host MIDI synth (pygame /
    SDL_mixer's dedicated music stream).  Driven by the MMSYSTEM MCI layer's
    open/play/stop/close (win16/api/mmsystem.py) — presentation only; the
    deterministic record is `services["mci_log"]`, so audio never affects state.

    Reuses whatever mixer a SquareWaveBackend already opened; the music stream
    is independent of the SFX channels, so both play at once."""

    def __init__(self) -> None:
        self.ok = False
        self._pg = None
        self._devices: dict[int, str] = {}      # MCI device id -> host .mid path
        self._playing = None
        try:
            import pygame
            self._pg = pygame
            if pygame.mixer.get_init() is None:
                pygame.mixer.init()
            self.ok = True
            print("[audio] MIDI music via SDL_mixer", flush=True)
        except Exception as exc:  # noqa: BLE001 — no synth is not a game bug
            print(f"[audio] MIDI music disabled: {type(exc).__name__}: {exc}",
                  flush=True)

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
            import os
            music.load(path)
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
