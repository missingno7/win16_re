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

    def play_wav(self, data: bytes, *, loop: bool = False) -> None:
        """Play a RIFF/WAV image (MMSYSTEM sndPlaySound path).  pygame parses
        the WAV and the mixer resamples to the open output format."""
        if not self.ok or not data:
            return
        import io
        try:
            snd = self._pg.mixer.Sound(file=io.BytesIO(data))
        except Exception as exc:  # noqa: BLE001 — a bad WAV is not a game bug
            print(f"[audio] WAV decode failed: {type(exc).__name__}: {exc}",
                  flush=True)
            return
        self._wav_playing = snd         # keep alive while it plays
        snd.play(loops=-1 if loop else 0)

    def stop_wav(self) -> None:
        snd = getattr(self, "_wav_playing", None)
        if self.ok and snd is not None:
            snd.stop()

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
