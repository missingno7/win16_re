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


# -- MidiBackend MCI-play semantics (win16.audio.MidiBackend) ------------------
from win16.audio import MidiBackend


class _FakeMusic:
    def __init__(self):
        self.loads, self.plays, self.busy, self.stops = [], 0, False, 0

    def load(self, p, namehint=None):
        # the backend hands SDL a BytesIO of the Mapper-filtered SMF; record
        # the logical song it carries so the assertions stay readable
        self.loads.append(p.getvalue().decode() if hasattr(p, "getvalue") else p)

    def play(self, *a, **k):
        self.plays += 1
        self.busy = True

    def get_busy(self):
        return self.busy

    def stop(self):
        self.stops += 1
        self.busy = False


def _midi():
    b = MidiBackend.__new__(MidiBackend)
    music = _FakeMusic()
    b.ok, b._devices, b._playing = True, {}, None
    b._filtered = {}
    # these tests pin the MCI play/stop semantics, not the SMF rewrite (which
    # has its own suite in test_midi_mapper.py) — stub the filter to identity
    b._mapper_filtered = lambda path: path.encode()
    b._pg = type("Pg", (), {"mixer": type("Mx", (), {"music": music})()})()
    return b, music


def test_midi_play_idempotent_while_playing():
    """MCI_PLAY re-issued while the song is still sounding must NOT restart it
    (the stutter fix) — real MCI continues, it does not reload from the top."""
    b, music = _midi()
    b.open(1, "GAMETHME.MID")
    b.play(1)
    assert music.plays == 1 and music.loads == ["GAMETHME.MID"]
    b.play(1)
    b.play(1)
    assert music.plays == 1                         # no restart storm
    assert music.loads == ["GAMETHME.MID"]


def test_midi_play_restarts_after_song_ended():
    """Once the song has finished, re-issuing MCI_PLAY (the game looping its
    music) legitimately restarts it."""
    b, music = _midi()
    b.open(1, "GAMETHME.MID")
    b.play(1)
    music.busy = False                              # song ended
    b.play(1)
    assert music.plays == 2


def test_midi_new_device_plays_over_busy():
    """A different device id (a new song) plays even while the previous is
    busy — the guard is per-device, not a global mute."""
    b, music = _midi()
    b.open(1, "A.MID")
    b.open(2, "B.MID")
    b.play(1)
    b.play(2)
    assert music.plays == 2 and music.loads[-1] == "B.MID"
