"""Demo v2 record/replay round-trip (win16/demo.py) — the peek timeline.

A game that consumes its input through PeekMessage (SimAnt in-game) must
record and replay through the same "p" records the GetMessage pump gets from
"m" records: same order, same filters, same clock stamps.  These pin the
format contract without a machine — the sysobj is just a clock holder.
"""
from types import SimpleNamespace

import pytest

from win16.demo import DemoDivergence, DemoEnded, DemoPlayer, DemoRecorder

MSG_A = (330, 0x0115, 1, 0, 1000, 0)          # WM_VSCROLL via GetMessage
MSG_B = (280, 0x0113, 7, 0, 1016, 0)          # WM_TIMER via PeekMessage
MSG_C = (330, 0x0201, 0, 0x00100010, 1032, 0)  # WM_LBUTTONDOWN via PeekMessage
FILT_TIMER = (280, 0x0113, 0x0113)
FILT_ANY = (0, 0, 0)


def _record(tmp_path, **hdr):
    path = tmp_path / "demo.jsonl"
    rec = DemoRecorder(path, "GAME.EXE", **hdr)
    rec.message(MSG_A)
    rec.peek(MSG_B, FILT_TIMER)
    rec.peek(MSG_C, FILT_ANY)
    rec.message(None)
    rec.close()
    return path


def test_roundtrip_m_and_p_records(tmp_path):
    player = DemoPlayer(_record(tmp_path))
    sysobj = SimpleNamespace(clock_ms=0)

    assert player.next_message(sysobj) == MSG_A
    assert sysobj.clock_ms == 1000

    # A peek with the wrong filter misses without consuming.
    assert player.next_peek(sysobj, 0, 0, 0, True) is None
    assert sysobj.clock_ms == 1000
    # A NOREMOVE glance with the right filter sees it but doesn't consume.
    assert player.next_peek(sysobj, *FILT_TIMER, False) == MSG_B
    assert player.next_peek(sysobj, *FILT_TIMER, False) == MSG_B
    assert sysobj.clock_ms == 1000
    # PM_REMOVE consumes and advances the clock.
    assert player.next_peek(sysobj, *FILT_TIMER, True) == MSG_B
    assert sysobj.clock_ms == 1016

    assert player.next_peek(sysobj, *FILT_ANY, True) == MSG_C
    assert sysobj.clock_ms == 1032

    assert player.next_message(sysobj) is None          # the recorded quit
    assert player.exhausted


def test_exhausted_stream_ends_the_replay_on_either_path(tmp_path):
    # A peek-driven game never calls GetMessage, so a peek past the last
    # record must end the replay just like GetMessage does — deterministically
    # at the first ask-for-more.
    player = DemoPlayer(_record(tmp_path))
    sysobj = SimpleNamespace(clock_ms=0)
    while not player.exhausted:
        if player.records[player.pos]["t"] == "p":
            player.next_peek(sysobj, *tuple(player.records[player.pos]["f"]), True)
        else:
            player.next_message(sysobj)
    with pytest.raises(DemoEnded):
        player.next_peek(sysobj, 0, 0, 0, True)
    with pytest.raises(DemoEnded):
        player.next_message(sysobj)


def test_getmessage_diverges_on_pending_peek_record(tmp_path):
    player = DemoPlayer(_record(tmp_path))
    sysobj = SimpleNamespace(clock_ms=0)
    player.next_message(sysobj)
    with pytest.raises(DemoDivergence):
        player.next_message(sysobj)     # next record is "p", not "m"


def test_snapshot_anchor_header(tmp_path):
    path = _record(tmp_path, snapshot="snap_114308", instruction=17050442)
    player = DemoPlayer(path)
    assert player.snapshot == "snap_114308"
    assert player.instruction == 17050442
    unanchored = DemoPlayer(_record(tmp_path))
    assert unanchored.snapshot is None
    assert unanchored.instruction == 0


def test_v1_demo_still_replays(tmp_path):
    path = tmp_path / "v1.jsonl"
    path.write_text(
        '{"kind": "win16-demo", "version": 1, "exe": "OLD.EXE"}\n'
        '{"t": "m", "v": [1, 15, 0, 0, 5, 0]}\n'
        '{"t": "quit"}\n', encoding="ascii")
    player = DemoPlayer(path)
    sysobj = SimpleNamespace(clock_ms=0)
    assert player.snapshot is None
    assert player.next_peek(sysobj, 0, 0, 0, True) is None   # "m" next: peek misses
    assert player.next_message(sysobj) == (1, 15, 0, 0, 5, 0)
    assert player.next_message(sysobj) is None
