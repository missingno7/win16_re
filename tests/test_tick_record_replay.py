"""win16.tick_demo record/replay pair — game-free mechanics.

The end-to-end proof (record from a real v4 replay, canonize, cross-config
verify) needs a game EXE and lives in the game-port project; asserted here:
the container round-trips, buckets/boundaries land where recorded, the driver
delivers boundaries on ask (advancing only on PM_REMOVE), injects per-tick
input with polled-state noting, serves the recorded clock monotonically, and
check-mode raises at the first divergent tick.
"""
from __future__ import annotations

import collections
from types import SimpleNamespace

import pytest

from win16.demo import DemoDivergence, DemoEnded
from win16.tick_demo import (RAMP_CALLS, STALL_CALLS_PER_MS, TickDemoDriver,
                             TickDemoRecorder, WM_TIMER, is_input_message)

CLICK0 = (5, 0x0201, 1, 0x00100010, 100, 0)      # WM_LBUTTONDOWN, bucket 0
KEY1 = (5, 0x0100, 0x41, 1, 220, 0)              # WM_KEYDOWN 'A', bucket 1
TIMER = (7, WM_TIMER, 1, 0, 0, 0)


def _record(tmp_path):
    p = tmp_path / "t.tickdemo"
    rec = TickDemoRecorder(p, "GAME.EXE", ms0=90)
    rec.clock(95); rec.clock(120)                # bucket 0's GetTickCount reads
    rec.input(CLICK0)                            # consumed pre-tick
    rec.boundary((7, WM_TIMER, 1, 0, 200, 0))    # boundary 0 (ends bucket 0)
    rec.clock(205)                               # bucket 1's read
    rec.input(KEY1)                              # consumed in tick 1
    rec.boundary((7, WM_TIMER, 1, 0, 217, 0))    # boundary 1
    rec.quit()
    rec.close()
    return p


def test_clock_sideband_replays_recorded_reads_exactly(tmp_path):
    d = TickDemoDriver(_record(tmp_path), mode="off")
    sysobj, _ = _fake_sys()
    d.install(sysobj)
    assert d.clocks[0] == [95, 120] and d.clocks[1] == [205]
    assert d.tick_count() == 95                  # exact recorded reads, in order
    assert d.tick_count() == 120
    assert d.tick_count() >= 120                 # past the recorded reads -> ramp/escape, monotonic
    _drain_then_boundary(d)                       # boundary 0 -> bucket 1
    assert d.tick_count() == 205                 # bucket 1's recorded read


def _fake_sys():
    noted = []
    return SimpleNamespace(
        msg_queue=collections.deque(), quit_posted=None,
        timer_procs={(7, 1): 0x12345678}, machine=SimpleNamespace(),
        _note_input=noted.append, interactive=True, tick_driver=None), noted


def test_round_trip_buckets_and_boundaries(tmp_path):
    d = TickDemoDriver(_record(tmp_path), mode="off")
    assert d.exe == "GAME.EXE" and d.ms0 == 90
    assert d.n_ticks == 2
    assert d.buckets[0] == [CLICK0] and d.buckets[1] == [KEY1]
    assert d.boundaries[0]["key"] == [7, 1] and d.boundaries[0]["ms"] == 200
    assert d.quit_k == 2


def test_driver_delivers_on_demand_in_consumption_order(tmp_path):
    d = TickDemoDriver(_record(tmp_path), mode="off")
    sysobj, noted = _fake_sys()
    d.install(sysobj)
    assert sysobj.tick_driver is d and sysobj.interactive is False
    assert not sysobj.msg_queue                  # nothing pre-injected

    # the boundary is NOT deliverable while the bucket has undelivered input
    assert d.timer_ask(0, True) is None and d.bucket == 0

    # a filtered ask that doesn't match the next message misses; matching pops
    assert d.next_input(0, WM_TIMER, WM_TIMER, True) is None
    assert d.next_input(0, 0, 0, False) == CLICK0 and noted == []   # peek: no pop
    assert d.next_input(0, 0, 0, True) == CLICK0 and noted == [CLICK0]

    # bucket drained -> PM_NOREMOVE shows the boundary without advancing
    peek = d.timer_ask(0, False)
    assert peek[:3] == (7, WM_TIMER, 1) and d.bucket == 0
    # a filtered ask for another window's timer still misses
    assert d.timer_ask(99, True) is None and d.bucket == 0

    m0 = d.timer_ask(7, True)                    # boundary 0: tick 1 begins
    assert m0[3] == 0x12345678                   # TimerProc looked up live
    assert m0[4] == 200                          # the recorded boundary clock
    assert d.bucket == 1

    assert d.next_input(0, 0, 0, True) == KEY1   # tick 1's input, on demand
    d.timer_ask(0, True)                         # boundary 1: final bucket
    assert d.next_input(0, 0, 0, True) is None   # empty final bucket...
    assert sysobj.quit_posted is True            # ...lands the recorded quit
    with pytest.raises(DemoEnded):
        d.timer_ask(0, True)


def _drain_then_boundary(d):
    while d.next_input(0, 0, 0, True) is not None:
        pass
    return d.timer_ask(0, True)


def test_clock_ramps_to_next_boundary_then_holds_monotonic(tmp_path):
    d = TickDemoDriver(_record(tmp_path), mode="off")
    sysobj, _ = _fake_sys()
    d.install(sysobj)
    # bucket 0: ramp from ms0=90 toward boundary 0's ms=200 over RAMP_CALLS.
    first = d.tick_count()
    assert 90 <= first < 200
    for _ in range(RAMP_CALLS):
        v = d.tick_count()
    assert v >= 200                              # reached the boundary...
    assert d.tick_count() >= 200                 # ...and keeps escaping past it
    _drain_then_boundary(d)                       # boundary 0 -> tick 1
    # bucket 1: ramp from boundary0.ms=200 toward boundary1.ms=217, monotonic.
    for _ in range(RAMP_CALLS):
        v = d.tick_count()
    assert v >= 217
    # a recording whose next base steps backward stays clamped monotonic
    d.boundaries[1]["ms"] = 150
    assert d.tick_count() >= 217


def test_check_mode_raises_at_first_divergent_tick(tmp_path):
    p = _record(tmp_path)
    # canonicalization pass: record digests, save
    rec_run = TickDemoDriver(p, digest_fn=lambda m: "aa", mode="record")
    sysobj, _ = _fake_sys()
    rec_run.install(sysobj)
    _drain_then_boundary(rec_run)
    _drain_then_boundary(rec_run)
    p2 = p.with_suffix(".canon")
    rec_run.save(p2)

    ok = TickDemoDriver(p2, digest_fn=lambda m: "aa", mode="check")
    s2, _ = _fake_sys(); ok.install(s2)
    _drain_then_boundary(ok); _drain_then_boundary(ok)
    assert ok.ticks_checked == 2

    bad = TickDemoDriver(p2, digest_fn=lambda m: "bb", mode="check")
    s3, _ = _fake_sys(); bad.install(s3)
    with pytest.raises(DemoDivergence, match="tick 0"):
        _drain_then_boundary(bad)


def test_check_mode_requires_canonicalized_digests(tmp_path):
    with pytest.raises(ValueError, match="canonicalization"):
        TickDemoDriver(_record(tmp_path), digest_fn=lambda m: "x", mode="check")


def test_input_message_classes():
    assert is_input_message(0x0201) and is_input_message(0x0100)
    assert not is_input_message(WM_TIMER) and not is_input_message(0x000F)
