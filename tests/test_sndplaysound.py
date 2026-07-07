"""sndPlaySound routing — filename, SND_MEMORY (WAV-in-RAM), and stop.

Microman's SFX (fire/hit) are RIFF/WAV images the game builds in a global
buffer and plays with SND_MEMORY; only the looping title music is a disk WAV.
This pins that every mode reaches the backend with the right bytes — no audio
device needed (a fake backend records the calls).
"""
import struct

from win16.api.core import ApiRegistry, CallContext
from win16.api.mmsystem import SND_LOOP, SND_MEMORY, install
from win16.app import create_machine

from scripts.games import game_exe, game_winflags


def _tiny_wav() -> bytes:
    body = b"\x80" * 8                      # 8 sample bytes, 8-bit mono
    fmt = struct.pack("<HHIIHH", 1, 1, 11025, 11025, 1, 8)
    data = (b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", len(body)) + body)
    return b"RIFF" + struct.pack("<I", len(data)) + data


class _FakeBackend:
    def __init__(self):
        self.calls = []

    def play_wav(self, data, *, loop=False):
        self.calls.append(("play", bytes(data), loop))

    def stop_wav(self):
        self.calls.append(("stop",))


def _machine_with_backend():
    m = create_machine(game_exe("microman"), winflags=game_winflags("microman"))
    backend = _FakeBackend()
    m.api.services["sound_backend"] = backend
    return m, backend


def _call(m, ptr, flags):
    ctx = CallContext(m.cpu, m.api, "MMSYSTEM", 2, "sndPlaySound",
                      args=(ptr, flags))
    m.api.entries[("MMSYSTEM", 2)].handler(ctx)


def test_snd_memory_plays_the_ram_wav_image():
    m, backend = _machine_with_backend()
    wav = _tiny_wav()
    # Park the WAV in a scratch paragraph and hand its far pointer + trailing
    # garbage (only RIFF size bytes should be copied, not the whole segment).
    seg = m.free_para
    m.free_para += 64
    for i, b in enumerate(wav + b"\xAB\xCD\xEF"):
        m.mem.wb(seg, i, b)
    _call(m, (seg << 16) | 0, SND_MEMORY | 0x1)

    assert len(backend.calls) == 1
    kind, data, loop = backend.calls[0]
    assert kind == "play" and loop is False
    assert data == wav                       # exact RIFF length, no trailing junk


def test_filename_and_loop_and_stop_route():
    m, backend = _machine_with_backend()
    # A disk WAV by name (the title music path): SND_LOOP -> loop=True.
    seg = m.free_para
    m.free_para += 16
    for i, b in enumerate(b"MICROMAN.WAV\x00"):
        m.mem.wb(seg, i, b)
    _call(m, (seg << 16) | 0, SND_LOOP)
    assert backend.calls[-1][0] == "play"
    assert backend.calls[-1][2] is True      # looped
    assert backend.calls[-1][1][:4] == b"RIFF"

    _call(m, 0, 0)                           # NULL ptr -> stop
    assert backend.calls[-1] == ("stop",)
