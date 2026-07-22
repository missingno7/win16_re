"""Win16 ReplayArtifact record/replay round-trip (win16/replay.py).

The 3.0 successor of the v4 demo contract tests: the recorder writes the
instruction-keyed input timeline into a ``dos_re.replay.ReplayRecording``,
and the input driver applies it back — injection at instruction counts,
GetTickCount interpolation, in-order dialog / message-box consumption —
pinned with the same tiny fake machine (a mutable instruction counter + a
queue).  What is NOT re-tested here is the artifact format itself: that is
dos_re's contract, covered upstream.
"""
from collections import deque
from types import SimpleNamespace

import pytest

from dos_re.replay import (ContinuationState, ReplayArtifact,
                           ReplayExecutionIdentity, ReplayRecording)
from win16.replay import (ArtifactRecorder, GUEST_INSTRUCTION_COORDINATE,
                          ReplayDivergence, ReplayExhausted,
                          Win16ReplayRecorder, input_driver_for)

# messages: (hwnd, msg, wparam, lparam, tick, pt)
KEY_A = (330, 0x0100, 0x41, 0, 10, 0)              # WM_KEYDOWN 'A' @ tick 10
CLICK = (330, 0x0201, 1, 0x00100010, 30, 0)        # WM_LBUTTONDOWN @ tick 30


def _profile() -> ReplayExecutionIdentity:
    return ReplayExecutionIdentity(
        profile_id="win16-test", role="oracle", implementation="interp",
        image="GAME.EXE sha256=0", runtime="win16-re-test",
        devices="none", continuation_schema="test-continuation-v1",
        projection_schema="test-projection-v1")


def _base_state() -> ContinuationState:
    return ContinuationState(schema_id="test-continuation-v1", metadata={},
                             regions={}, event_cursor=0).normalized()


def _record(tmp_path, name="artifact", *, taps=None) -> ReplayArtifact:
    recording = ReplayRecording(tmp_path / name, timeline_id=f"t-{name}",
                                profile=_profile(), base_state=_base_state())
    rec = Win16ReplayRecorder(recording, start_instruction=0)
    if taps is None:
        rec.arrival(KEY_A, instr=100)
        rec.clock_sample(200, 20, min_gap=0)
        rec.arrival(CLICK, instr=300)
        rec.quit(instr=400)
        end_instr = 400
    else:
        end_instr = taps(rec)
    end = rec.final_mark(end_instr)
    return recording.finish(end)


def _fake_sys():
    """A minimal Win16System stand-in: a mutable instruction counter, a
    message queue, and the two hooks the driver drives."""
    cpu = SimpleNamespace(instruction_count=0)
    machine = SimpleNamespace(cpu=cpu)
    noted = []

    def next_message():
        if sysobj.quit_posted is not None:
            return None
        if sysobj.msg_queue:
            return sysobj.msg_queue.popleft()
        raise RuntimeError("idle")                 # what the real pump raises

    sysobj = SimpleNamespace(
        machine=machine, msg_queue=deque(), quit_posted=None, clock_ms=0,
        interactive=True, demo_driver=None, yield_check=None,
        _note_input=lambda m: noted.append(m), next_message=next_message)
    sysobj.noted = noted
    return sysobj


def _driver(tmp_path, name="artifact", **kw):
    d = input_driver_for(_record(tmp_path, name, **kw))
    d.sys = _fake_sys()
    return d


def test_clock_timeline_is_reproduced(tmp_path):
    d = _driver(tmp_path)
    # samples: (100,10) from KEY_A, (200,20) from clock, (300,30) from CLICK
    assert d.tick_at(50) == 10          # before first sample -> clamp
    assert d.tick_at(100) == 10
    assert d.tick_at(150) == 15         # linear between (100,10) and (200,20)
    assert d.tick_at(250) == 25         # linear between (200,20) and (300,30)
    assert d.tick_at(300) == 30
    assert d.tick_at(1300) == 31        # tail: instruction floor past last


def test_arrivals_inject_at_their_instruction(tmp_path):
    d = _driver(tmp_path)
    d.sys.machine.cpu.instruction_count = 99
    d.inject_due()
    assert not d.sys.msg_queue and d.sys.noted == []      # first arrival @100

    d.sys.machine.cpu.instruction_count = 100
    d.inject_due()
    assert list(d.sys.msg_queue) == [KEY_A]
    assert d.sys.noted == [KEY_A]                # polled state fed at arrival

    d.sys.machine.cpu.instruction_count = 305    # past the click's @300
    d.inject_due()
    assert list(d.sys.msg_queue) == [KEY_A, CLICK]
    assert d.current_ordinal == 2                # timeline position advanced


def test_pump_peek_misses_until_the_arrival_instruction(tmp_path):
    d = _driver(tmp_path)
    d.sys.machine.cpu.instruction_count = 50
    d.pump_peek()
    assert not d.sys.msg_queue                             # nothing due yet
    d.sys.machine.cpu.instruction_count = 100
    d.pump_peek()
    assert list(d.sys.msg_queue) == [KEY_A]                # now injected


def test_pump_get_delivers_queued_then_blocks_to_next_arrival(tmp_path):
    d = _driver(tmp_path)
    d.sys.machine.cpu.instruction_count = 100
    assert d.pump_get() == KEY_A
    # Idle between arrivals: a blocking GetMessage delivers the next arrival
    # (the recorded wall-clock wait — the CPU was parked, instr frozen).
    d.sys.machine.cpu.instruction_count = 150
    assert d.pump_get() == CLICK
    # Next blocking GetMessage hits the recorded quit.
    assert d.pump_get() is None
    assert d.sys.quit_posted is True


def test_exhausted_stream_raises(tmp_path):
    d = _driver(tmp_path)
    d.sys.machine.cpu.instruction_count = 100
    assert d.pump_get() == KEY_A
    d.sys.machine.cpu.instruction_count = 300
    assert d.pump_get() == CLICK
    d.sys.machine.cpu.instruction_count = 400
    assert d.pump_get() is None                  # quit ends the timeline
    d.sys.quit_posted = None                     # a peek-driven game keeps polling
    with pytest.raises(ReplayExhausted):
        d.pump_peek()


def test_dialog_events_replay_in_order(tmp_path):
    def taps(rec):
        rec.dialog_event("myd_scores", ("command", 1, 0), instr=10)
        rec.dialog_event("myd_scores", ("command", 2, 0), instr=20)
        return 20
    d = _driver(tmp_path, "dlg", taps=taps)
    assert d.next_dialog_event("myd_scores") == ("command", 1, 0)
    assert d.next_dialog_event("myd_scores") == ("command", 2, 0)
    with pytest.raises(ReplayExhausted):
        d.next_dialog_event("myd_scores")


def test_dialog_divergence_on_wrong_name(tmp_path):
    def taps(rec):
        rec.dialog_event("myd_scores", ("command", 1, 0), instr=10)
        return 10
    d = _driver(tmp_path, "dlg", taps=taps)
    with pytest.raises(ReplayDivergence):
        d.next_dialog_event("some_other_dialog")


# --- message-box results: a user DECISION that steers control flow ---------

def test_messagebox_results_replay_in_order(tmp_path):
    def taps(rec):
        rec.messagebox_result("SimAnt Load", 7, instr=10)      # IDNO
        rec.messagebox_result("SimAnt Quit", 6, instr=20)      # IDYES
        return 20
    d = _driver(tmp_path, "box", taps=taps)
    assert d.next_messagebox_result("SimAnt Load") == 7
    assert d.next_messagebox_result("SimAnt Quit") == 6


def test_a_timeline_with_no_recorded_box_answers_none(tmp_path):
    """The compatibility contract recordings made before boxes were captured
    depend on: ``None`` means "decide it as if there were no replay"."""
    d = _driver(tmp_path)
    assert d.next_messagebox_result("SimAnt Load") is None


def test_messagebox_divergence_on_wrong_caption(tmp_path):
    def taps(rec):
        rec.messagebox_result("SimAnt Load", 7, instr=10)
        return 10
    d = _driver(tmp_path, "box", taps=taps)
    with pytest.raises(ReplayDivergence):
        d.next_messagebox_result("Some Other Box")


def test_exhausted_box_stream_falls_back_rather_than_raising(tmp_path):
    """Running out of recorded answers is NOT divergence: a recording may
    legitimately stop before a box the game shows later, and the default is
    the honest answer there."""
    def taps(rec):
        rec.messagebox_result("SimAnt Load", 7, instr=10)
        return 10
    d = _driver(tmp_path, "box", taps=taps)
    assert d.next_messagebox_result("SimAnt Load") == 7
    assert d.next_messagebox_result("SimAnt Load") is None


# --- artifact-level facts this layer relies on ------------------------------

def test_every_ordinal_carries_an_instruction_coordinate(tmp_path):
    art = _record(tmp_path, "coords")
    coords = {c.point.ordinal: c for c in art.timeline_coordinates}
    assert sorted(coords) == list(range(art.end_point.ordinal + 1))
    assert all(c.schema_id == GUEST_INSTRUCTION_COORDINATE
               for c in coords.values())
    assert coords[1].value == 100 and coords[3].value == 300


def test_artifact_recorder_round_trips_through_the_input_driver(tmp_path):
    """The interactive recorder's tap surface -> a ReplayArtifact -> the input
    driver replays the same arrivals.  This is the record<->replay contract the
    v4 DemoRecorder/DemoDriver pair used to hold, now on ReplayArtifact."""
    rec = ArtifactRecorder(
        tmp_path / "session", timeline_id="win16:session",
        profile=_profile(), base_state=_base_state(), start_instruction=0)
    rec.arrival(KEY_A, instr=100)
    rec.clock_sample(200, 20, min_gap=0)
    rec.arrival(CLICK, instr=300)
    rec.quit(instr=400)
    artifact = rec.close()
    assert rec.records == 4

    d = input_driver_for(artifact)
    d.sys = _fake_sys()
    d.sys.machine.cpu.instruction_count = 100
    assert d.pump_get() == KEY_A
    d.sys.machine.cpu.instruction_count = 305
    assert d.pump_get() == CLICK
    assert d.pump_get() is None and d.sys.quit_posted is True
    assert d.tick_at(150) == 15                     # clock reproduced


def test_clock_sample_rate_limit(tmp_path):
    def taps(rec):
        rec.arrival(KEY_A, instr=100)
        rec.clock_sample(110, 11, min_gap=20000)   # too close: dropped
        rec.clock_sample(30000, 25, min_gap=20000)
        return 30000
    art = _record(tmp_path, "rate", taps=taps)
    clocks = [e for e in art.events if e.channel == "win16.clock"]
    assert len(clocks) == 1 and clocks[0].payload["ms"] == 25
