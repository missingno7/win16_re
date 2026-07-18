"""Demo v4 record/replay round-trip (win16/demo.py) — the instruction-keyed
input timeline.

v4 keys input to the INSTRUCTION COUNT, not the fetch API: the recorder logs raw
input arrivals ("i") + periodic clock samples ("c") + quit, and the DemoDriver
injects each arrival into the queue when the machine reaches its instruction and
reproduces GetTickCount from the (instr -> tick) samples.  These pin the format
contract with a tiny fake machine (a mutable instruction counter + a queue).
"""
from collections import deque
from types import SimpleNamespace

import pytest

from win16.demo import DemoDriver, DemoEnded, DemoDivergence, DemoRecorder

# messages: (hwnd, msg, wparam, lparam, tick, pt)
KEY_A = (330, 0x0100, 0x41, 0, 10, 0)              # WM_KEYDOWN 'A' @ tick 10
CLICK = (330, 0x0201, 1, 0x00100010, 30, 0)        # WM_LBUTTONDOWN @ tick 30


def _fake_sys():
    """A minimal Win16System stand-in: a mutable instruction counter, a message
    queue, and the two hooks the driver drives (_note_input, next_message)."""
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


def _record(tmp_path, **hdr):
    path = tmp_path / "demo.jsonl"
    rec = DemoRecorder(path, "GAME.EXE", **hdr)
    rec.arrival(KEY_A, instr=100)
    rec.clock_sample(200, 20, min_gap=0)
    rec.arrival(CLICK, instr=300)
    rec.quit(instr=400)
    rec.close()
    return path


def test_clock_timeline_is_reproduced(tmp_path):
    d = DemoDriver(_record(tmp_path))
    # samples: (100,10) from KEY_A, (200,20) from "c", (300,30) from CLICK
    assert d.tick_at(50) == 10          # before first sample -> clamp
    assert d.tick_at(100) == 10
    assert d.tick_at(150) == 15         # linear between (100,10) and (200,20)
    assert d.tick_at(250) == 25         # linear between (200,20) and (300,30)
    assert d.tick_at(300) == 30
    assert d.tick_at(1300) == 31        # tail: instruction floor past last sample


def test_arrivals_inject_at_their_instruction(tmp_path):
    d = DemoDriver(_record(tmp_path))
    d.sys = _fake_sys()

    d.sys.machine.cpu.instruction_count = 99
    d.inject_due()
    assert not d.sys.msg_queue and d.sys.noted == []      # first arrival is @100

    d.sys.machine.cpu.instruction_count = 100
    d.inject_due()
    assert list(d.sys.msg_queue) == [KEY_A]
    assert d.sys.noted == [KEY_A]                          # polled state fed at arrival

    d.sys.machine.cpu.instruction_count = 305             # past the click's @300
    d.inject_due()
    assert list(d.sys.msg_queue) == [KEY_A, CLICK]


def test_pump_peek_misses_until_the_arrival_instruction(tmp_path):
    # A busy-poll: PeekMessage injects nothing until the awaited arrival's
    # instruction is reached, then the game's own queue scan finds it.
    d = DemoDriver(_record(tmp_path))
    d.sys = _fake_sys()
    d.sys.machine.cpu.instruction_count = 50
    d.pump_peek()
    assert not d.sys.msg_queue                             # nothing due yet
    d.sys.machine.cpu.instruction_count = 100
    d.pump_peek()
    assert list(d.sys.msg_queue) == [KEY_A]                # now injected


def test_pump_get_delivers_queued_then_blocks_to_next_arrival(tmp_path):
    d = DemoDriver(_record(tmp_path))
    d.sys = _fake_sys()
    # At the key's instruction, GetMessage returns it (injected + popped).
    d.sys.machine.cpu.instruction_count = 100
    assert d.pump_get() == KEY_A
    # Idle between arrivals: a blocking GetMessage delivers the next arrival
    # (the recorded wall-clock wait — the CPU was parked, instr frozen).
    d.sys.machine.cpu.instruction_count = 150
    assert d.pump_get() == CLICK
    # Next blocking GetMessage hits the recorded quit.
    assert d.pump_get() is None
    assert d.sys.quit_posted is True


def test_exhausted_stream_raises_demo_ended(tmp_path):
    d = DemoDriver(_record(tmp_path))
    d.sys = _fake_sys()
    # Instructions advance gradually, as they do in a real run.
    d.sys.machine.cpu.instruction_count = 100
    assert d.pump_get() == KEY_A
    d.sys.machine.cpu.instruction_count = 300
    assert d.pump_get() == CLICK
    d.sys.machine.cpu.instruction_count = 400
    assert d.pump_get() is None                            # quit ends the timeline
    d.sys.quit_posted = None                               # a peek-driven game keeps polling
    with pytest.raises(DemoEnded):
        d.pump_peek()


def test_polled_input_refresh_injects_due_arrivals(tmp_path):
    # THE CONFIG-INVARIANCE RULE: a poll (GetAsyncKeyState/GetKeyState/
    # GetCursorPos -> refresh_polled_input) must inject arrivals due at ITS
    # instruction count — the poll instant is identical on the interpreted
    # oracle and on a virtual-time-preserving lifted graph, while yield_check
    # (the only other in-callback touchpoint) fires per interpreter STEP and
    # so shifts with the installed hook set.  Found live as the first
    # oracle-vs-VMless-graph divergence on a real game (a VK_LBUTTON poll
    # racing a recorded WM_LBUTTONDOWN between two yields).
    from win16.api.system import Win16System
    d = DemoDriver(_record(tmp_path))
    d.sys = _fake_sys()
    d.sys.demo_driver = d
    d.sys.input_drainer = None

    d.sys.machine.cpu.instruction_count = 299     # click arrives at 300
    Win16System.refresh_polled_input(d.sys)
    assert CLICK not in d.sys.msg_queue

    d.sys.machine.cpu.instruction_count = 300
    Win16System.refresh_polled_input(d.sys)       # the poll injects it NOW
    assert CLICK in d.sys.msg_queue
    assert CLICK in d.sys.noted                   # polled state fed at arrival


def test_snapshot_anchor_header(tmp_path):
    path = _record(tmp_path, snapshot="snap_114308", instruction=17050442)
    d = DemoDriver(path)
    assert d.snapshot == "snap_114308"
    assert d.instruction == 17050442
    assert DemoDriver(_record(tmp_path)).snapshot is None


def test_dialog_events_replay_in_order(tmp_path):
    path = tmp_path / "dlg.jsonl"
    rec = DemoRecorder(path, "GAME.EXE")
    rec.dialog_event("myd_scores", ("command", 1, 0), instr=10)
    rec.dialog_event("myd_scores", ("command", 2, 0), instr=20)
    rec.close()
    d = DemoDriver(path)
    assert d.next_dialog_event("myd_scores") == ("command", 1, 0)
    assert d.next_dialog_event("myd_scores") == ("command", 2, 0)
    with pytest.raises(DemoEnded):
        d.next_dialog_event("myd_scores")


def test_dialog_divergence_on_wrong_name(tmp_path):
    path = tmp_path / "dlg.jsonl"
    rec = DemoRecorder(path, "GAME.EXE")
    rec.dialog_event("myd_scores", ("command", 1, 0), instr=10)
    rec.close()
    d = DemoDriver(path)
    with pytest.raises(DemoDivergence):
        d.next_dialog_event("some_other_dialog")


def test_pre_v4_demo_is_rejected(tmp_path):
    path = tmp_path / "v3.jsonl"
    path.write_text(
        '{"kind": "win16-demo", "version": 3, "exe": "OLD.EXE"}\n'
        '{"t": "m", "v": [1, 15, 0, 0, 5, 0]}\n', encoding="ascii")
    with pytest.raises(ValueError, match="pre-v4"):
        DemoDriver(path)


# --- message-box results: a user DECISION that steers control flow ---------

def test_messagebox_results_replay_in_order(tmp_path):
    """The recorded button, not the default one.  A "save the current file?"
    answered No goes on to Open; answered Yes it saves first — so replaying the
    default silently takes the other branch and diverges somewhere later."""
    path = tmp_path / "box.jsonl"
    rec = DemoRecorder(path, "GAME.EXE")
    rec.messagebox_result("SimAnt Load", 7, instr=10)      # IDNO
    rec.messagebox_result("SimAnt Quit", 6, instr=20)      # IDYES
    rec.close()
    d = DemoDriver(path)
    assert d.next_messagebox_result("SimAnt Load") == 7
    assert d.next_messagebox_result("SimAnt Quit") == 6


def test_a_demo_with_no_recorded_box_answers_none(tmp_path):
    """The compatibility contract every demo recorded before boxes were
    captured depends on — including the pinned byte-exact gate demo.  ``None``
    means "decide it as if there were no demo", so those replay unchanged
    rather than failing on a record that was never written."""
    d = DemoDriver(_record(tmp_path))
    assert d.next_messagebox_result("SimAnt Load") is None


def test_messagebox_divergence_on_wrong_caption(tmp_path):
    path = tmp_path / "box.jsonl"
    rec = DemoRecorder(path, "GAME.EXE")
    rec.messagebox_result("SimAnt Load", 7, instr=10)
    rec.close()
    d = DemoDriver(path)
    with pytest.raises(DemoDivergence):
        d.next_messagebox_result("Some Other Box")


def test_exhausted_box_stream_falls_back_rather_than_raising(tmp_path):
    """Running out of recorded answers is NOT divergence: a demo may legitimately
    stop before a box the game shows later (an unexpected error box, say), and
    the default is the honest answer there."""
    path = tmp_path / "box.jsonl"
    rec = DemoRecorder(path, "GAME.EXE")
    rec.messagebox_result("SimAnt Load", 7, instr=10)
    rec.close()
    d = DemoDriver(path)
    assert d.next_messagebox_result("SimAnt Load") == 7
    assert d.next_messagebox_result("SimAnt Load") is None
