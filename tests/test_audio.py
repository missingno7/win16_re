"""SOUND.DRV note math + square-wave synthesis (no audio device, no assets)."""
import numpy as np

from win16.api.sound import NOTE_BASE_HZ, note_to_freq, note_to_ms
from win16.audio import SquareWaveBackend


def test_note_to_freq():
    assert note_to_freq(0) == 0.0                       # rest
    assert abs(note_to_freq(1) - NOTE_BASE_HZ) < 1e-6   # note 1 = base (C3)
    # each note is a semitone; 12 semitones up doubles the frequency
    assert abs(note_to_freq(13) - 2 * note_to_freq(1)) < 1e-6
    # a perfect fifth (7 semitones) is ~1.5x
    assert abs(note_to_freq(8) / note_to_freq(1) - 2 ** (7 / 12)) < 1e-9


def test_note_to_ms():
    assert note_to_ms(0, 120) == 0.0
    assert note_to_ms(4, 120) == 500.0                  # quarter at 120bpm
    assert note_to_ms(8, 120) == 250.0                  # eighth = half a quarter
    assert note_to_ms(4, 240) == 250.0                  # faster tempo, shorter


def _render(notes, volume=0.3, rate=22050):
    b = SquareWaveBackend.__new__(SquareWaveBackend)
    b.rate, b.volume, b._np = rate, volume, np
    return b._render(notes)


def test_render_lengths_and_silence():
    buf = _render([(200.0, 100), (0.0, 50), (400.0, 100)])
    assert buf.dtype == np.int16
    assert buf.size == int(22050 * 0.25)                # 100+50+100 ms
    rest = buf[int(22050 * 0.10):int(22050 * 0.15)]
    assert int(np.abs(rest).max()) == 0                 # the rest is silent
    assert int(np.abs(buf).max()) > 0                   # the tones are not


def test_render_is_square_wave():
    buf = _render([(441.0, 200)], volume=0.5)
    # A square wave has only two levels (±amp) away from the click envelope;
    # sample the steady middle and check it is near-bipolar.
    mid = buf[2000:4000].astype(np.int64)
    hi = mid[mid > 0]
    lo = mid[mid < 0]
    assert hi.size and lo.size
    assert np.std(hi) < 0.02 * hi.mean()                # flat top
    assert abs(hi.mean() + lo.mean()) < 0.02 * hi.mean()  # symmetric
