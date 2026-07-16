"""MIDI Mapper level filtering (win16.audio.midi_keep_channels).

Windows 3.x MIDI files carry the same music twice per the Microsoft MIDI
Mapper authoring guideline — extended level on channels 1-10, base level on
channels 13-16.  The MCI sequencer's Mapper played exactly one level; a
General MIDI synth fed the raw file plays both (doubled melody + the
base-level percussion channel 16 rendered as a melodic noise patch, heard
as a constant re-triggering hiss).  These tests pin the SMF rewrite that
reproduces the Mapper: drop one level, keep every surviving event at its
absolute time, preserve meta/sysex verbatim.  Pure function — no audio
device, no assets.
"""
import pytest

from win16.audio import (BASE_LEVEL_CHANNELS, EXTENDED_LEVEL_CHANNELS,
                         MidiBackend, midi_keep_channels)


# --- tiny SMF builder/parser (test-local, independent of the filter) --------

def vlq(value: int) -> bytes:
    out = bytearray([value & 0x7F])
    value >>= 7
    while value:
        out.insert(0, 0x80 | (value & 0x7F))
        value >>= 7
    return bytes(out)


def smf(tracks: list[bytes], division: int = 240) -> bytes:
    head = b"MThd" + (6).to_bytes(4, "big") + (1).to_bytes(2, "big") \
        + len(tracks).to_bytes(2, "big") + division.to_bytes(2, "big")
    return head + b"".join(
        b"MTrk" + len(t).to_bytes(4, "big") + t for t in tracks)


EOT = bytes([0x00, 0xFF, 0x2F, 0x00])
TEMPO = bytes([0x00, 0xFF, 0x51, 0x03, 0x07, 0xA1, 0x20])   # 500000 us/qn


def parse_events(data: bytes) -> list[list[tuple[int, bytes]]]:
    """-> per track: [(absolute_time, event_bytes)], running status resolved
    (channel events always reported with their explicit status byte)."""
    assert data[:4] == b"MThd"
    hlen = int.from_bytes(data[4:8], "big")
    ntrks = int.from_bytes(data[10:12], "big")
    pos = 8 + hlen
    tracks = []
    for _ in range(ntrks):
        assert data[pos:pos + 4] == b"MTrk"
        length = int.from_bytes(data[pos + 4:pos + 8], "big")
        p, end = pos + 8, pos + 8 + length
        pos = end
        events, t, running = [], 0, None

        def rvlq(p):
            v = 0
            while True:
                b = data[p]
                p += 1
                v = (v << 7) | (b & 0x7F)
                if not b & 0x80:
                    return v, p

        while p < end:
            dt, p = rvlq(p)
            t += dt
            b = data[p]
            if b == 0xFF:
                ln, q = rvlq(p + 2)
                events.append((t, data[p:q + ln]))
                p = q + ln
                running = None
            elif b in (0xF0, 0xF7):
                ln, q = rvlq(p + 1)
                events.append((t, data[p:q + ln]))
                p = q + ln
                running = None
            else:
                if b & 0x80:
                    running = b
                    p += 1
                status = running
                n = 1 if status & 0xF0 in (0xC0, 0xD0) else 2
                events.append((t, bytes([status]) + data[p:p + n]))
                p += n
        tracks.append(events)
    return tracks


def note_on(ch: int, note: int, vel: int = 96) -> bytes:
    return bytes([0x90 | ch, note, vel])


def note_off(ch: int, note: int) -> bytes:
    return bytes([0x80 | ch, note, 0])


# --- the dual-level fixture: same phrase on ch 2 (extended) and 12 (base) ---

def dual_level_track() -> bytes:
    return (TEMPO
            + b"\x00" + bytes([0xC0 | 2, 34])            # ch3 program 34
            + b"\x00" + bytes([0xC0 | 12, 34])           # ch13 mirror
            + b"\x00" + note_on(2, 60)
            + b"\x00" + note_on(12, 60)                  # base-level unison
            + vlq(240) + note_off(2, 60)
            + b"\x00" + note_off(12, 60)
            + vlq(240) + note_on(9, 42)                  # extended percussion
            + b"\x00" + note_on(15, 42)                  # base percussion (ch16)
            + vlq(120) + note_off(9, 42)
            + b"\x00" + note_off(15, 42)
            + EOT)


def channel_of(ev: bytes) -> int | None:
    return ev[0] & 0x0F if ev[0] < 0xF0 else None


def test_extended_level_drops_base_channels():
    filtered = midi_keep_channels(smf([dual_level_track()]),
                                  EXTENDED_LEVEL_CHANNELS)
    (events,) = parse_events(filtered)
    channels = {channel_of(ev) for _, ev in events} - {None}
    assert channels == {2, 9}                 # ch13/ch16 mirrors gone
    # every surviving event keeps its absolute time
    (orig,) = parse_events(smf([dual_level_track()]))
    kept = [(t, ev) for t, ev in orig
            if channel_of(ev) is None or channel_of(ev) in EXTENDED_LEVEL_CHANNELS]
    assert events == kept


def test_base_level_is_the_complement():
    filtered = midi_keep_channels(smf([dual_level_track()]),
                                  BASE_LEVEL_CHANNELS)
    (events,) = parse_events(filtered)
    channels = {channel_of(ev) for _, ev in events} - {None}
    assert channels == {12, 15}


def test_meta_events_survive_verbatim():
    filtered = midi_keep_channels(smf([dual_level_track()]),
                                  EXTENDED_LEVEL_CHANNELS)
    (events,) = parse_events(filtered)
    metas = [ev for _, ev in events if ev[0] == 0xFF]
    assert bytes([0xFF, 0x51, 0x03, 0x07, 0xA1, 0x20]) in metas   # tempo
    assert bytes([0xFF, 0x2F, 0x00]) in metas                     # end of track


def test_dropped_deltas_fold_into_the_next_kept_event():
    # delta 100 to a DROPPED ch12 note, then delta 50 to a kept ch0 note:
    # the kept note must land at absolute time 150.
    track = (vlq(100) + note_on(12, 64)
             + vlq(50) + note_on(0, 64)
             + EOT)
    (events,) = parse_events(midi_keep_channels(smf([track]),
                                                EXTENDED_LEVEL_CHANNELS))
    assert (150, note_on(0, 64)) in events
    assert all(channel_of(ev) != 12 for _, ev in events)


def test_running_status_resolves_across_dropped_events():
    # ch12 note-on with explicit status, then two RUNNING-status data pairs
    # (same dropped channel), then an explicit kept ch1 note: the filter must
    # consume the running-status pairs without desyncing.
    track = (b"\x00" + note_on(12, 60)
             + vlq(10) + bytes([62, 96])          # running status, ch12
             + vlq(10) + bytes([64, 96])          # running status, ch12
             + vlq(10) + note_on(1, 72)
             + EOT)
    (events,) = parse_events(midi_keep_channels(smf([track]),
                                                EXTENDED_LEVEL_CHANNELS))
    assert (30, note_on(1, 72)) in events
    assert all(channel_of(ev) != 12 for _, ev in events)


def test_single_param_messages_on_dropped_channels():
    # program change (Cn) and channel pressure (Dn) take ONE data byte —
    # a two-byte skip would shear the stream.
    track = (b"\x00" + bytes([0xC0 | 13, 126])    # base-level "Applause"
             + b"\x00" + bytes([0xD0 | 13, 50])
             + vlq(5) + note_on(3, 60)
             + EOT)
    (events,) = parse_events(midi_keep_channels(smf([track]),
                                                EXTENDED_LEVEL_CHANNELS))
    assert (5, note_on(3, 60)) in events
    assert all(channel_of(ev) != 13 for _, ev in events)


def test_multi_track_files_filter_every_track():
    ext = b"\x00" + note_on(4, 60) + EOT
    base = b"\x00" + note_on(14, 60) + EOT
    filtered = midi_keep_channels(smf([ext, base]), EXTENDED_LEVEL_CHANNELS)
    t0, t1 = parse_events(filtered)
    assert (0, note_on(4, 60)) in t0
    assert all(channel_of(ev) is None for _, ev in t1)   # only the EOT remains


def test_not_an_smf_raises():
    with pytest.raises(ValueError):
        midi_keep_channels(b"RIFF....", EXTENDED_LEVEL_CHANNELS)
    with pytest.raises(ValueError):
        midi_keep_channels(smf([dual_level_track()])[:8] + b"XXXX",
                           EXTENDED_LEVEL_CHANNELS)


def test_backend_level_selection():
    assert EXTENDED_LEVEL_CHANNELS == frozenset(range(0, 10))
    assert BASE_LEVEL_CHANNELS == frozenset(range(12, 16))
    with pytest.raises(KeyError):                # unknown level fails loud
        MidiBackend(level="karaoke")
