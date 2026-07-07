"""Determinism gates: demo record→replay and snapshot roundtrip, bit-exact."""
import pytest

from ppython import runtime
from win16.demo import DemoPlayer, DemoRecorder
from win16.vmsnap import digest, load_snapshot, save_snapshot

pytestmark = pytest.mark.skipif(not runtime.assets_present(),
                                reason="game assets not present")


def test_demo_replay_is_bit_exact(tmp_path):
    """Record a headless session (boot + New Game + gameplay incl. the
    game-over high-score dialog), then replay it: the game-observable state
    digest must match exactly at the same instruction count."""
    demo = tmp_path / "session.jsonl"

    m = runtime.create_machine()
    m.cpu.trace_enabled = False
    rec = DemoRecorder(demo, "PYTHON.EXE")
    m.api.services["demo_recorder"] = rec
    m.cpu.run(1_500_000)
    s = m.api.services["system"]
    s.post_message(s.windows[0].handle, 0x0111, 1050, 0)    # New Game
    m.cpu.run(2_500_000)
    rec.close()
    recorded = digest(m)

    m2 = runtime.create_machine()
    m2.cpu.trace_enabled = False
    player = DemoPlayer(demo)
    m2.api.services["system"].message_source = player.next_message
    m2.api.services["demo_player"] = player
    m2.cpu.run(4_000_000)                                   # same total budget

    assert m2.cpu.instruction_count == m.cpu.instruction_count
    assert digest(m2) == recorded, "replay diverged from the recorded run"


def test_snapshot_roundtrip_is_bit_exact(tmp_path):
    m = runtime.create_machine()
    m.cpu.trace_enabled = False
    m.cpu.run(1_500_000)                                    # boot -> idle
    save_snapshot(m, tmp_path / "snap", note="test")
    m.cpu.run(400_000)
    expected = digest(m)

    m2 = load_snapshot(tmp_path / "snap", runtime.create_machine)
    m2.cpu.trace_enabled = False
    m2.cpu.run(400_000)
    assert digest(m2) == expected, "resumed snapshot diverged from the original run"


def test_demo_divergence_fails_loud(tmp_path):
    """Feeding a demo to a machine that behaves differently must raise
    DemoDivergence/DemoEnded — never silently continue."""
    from win16.demo import DemoDivergence, DemoEnded
    demo = tmp_path / "short.jsonl"
    m = runtime.create_machine()
    m.cpu.trace_enabled = False
    rec = DemoRecorder(demo, "PYTHON.EXE")
    m.api.services["demo_recorder"] = rec
    m.cpu.run(1_600_000)                    # a short idle-only session
    rec.close()

    m2 = runtime.create_machine()
    m2.cpu.trace_enabled = False
    player = DemoPlayer(demo)
    m2.api.services["system"].message_source = player.next_message
    m2.api.services["demo_player"] = player
    with pytest.raises((DemoEnded, DemoDivergence)):
        m2.cpu.run(5_000_000)               # run far past the recording
