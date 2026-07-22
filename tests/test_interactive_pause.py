"""The snapshot pause must reach the CPU thread even while the game BUSY-POLLS
(PeekMessage) and never calls GetMessage — the state SimAnt is in on its menus
and in-game, where F9 used to time out.  A worker that only ever calls
check_pause() (never _next) must still park on request."""
import threading
import time
import types

from win16.interactive import InteractiveDriver


def _driver():
    sysobj = types.SimpleNamespace(clock_ms=0, message_source=None,
                                   msg_queue=[], windows=[], quit_posted=None)
    return InteractiveDriver(sysobj)


def test_drain_notes_polled_state_at_arrival():
    """A polling game (GetAsyncKeyState in a tight loop, no GetMessage) must see
    host input: the driver feeds polled state as input DRAINS, not when a message
    is consumed — otherwise SimAnt's caste-slider drag never sees the button
    release and freezes."""
    noted = []
    sysobj = types.SimpleNamespace(clock_ms=0, message_source=None, msg_queue=[],
                                   windows=[], quit_posted=None,
                                   _note_input=lambda m: noted.append(m))
    drv = InteractiveDriver(sysobj)
    drv.post_input(0x10, 0x0202, 0, 0)          # WM_LBUTTONUP
    drv._drain_input()
    assert len(noted) == 1 and noted[0][:4] == (0x10, 0x0202, 0, 0)
    assert sysobj.msg_queue and sysobj.msg_queue[0][:4] == (0x10, 0x0202, 0, 0)


def test_pause_parks_at_instruction_boundary_without_getmessage():
    drv = _driver()
    spun = {"n": 0}

    def worker():
        # Simulate the CPU worker's busy-poll: only check_pause between chunks,
        # never touching _next (GetMessage).
        while drv.running:
            drv.check_pause()
            spun["n"] += 1
            time.sleep(0.0005)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    try:
        assert drv.pause_at_boundary(timeout=3.0), "worker never parked"
        # While parked, the worker is blocked in check_pause -> spin count frozen.
        frozen = spun["n"]
        time.sleep(0.05)
        assert spun["n"] == frozen, "worker kept running while 'paused'"
        drv.resume()
        time.sleep(0.02)
        assert spun["n"] > frozen, "worker did not resume"
    finally:
        drv.stop()
        t.join(timeout=1.0)


def test_ready_gate_defers_the_park_until_a_resumable_boundary():
    """A recorder gates the park on ``ready`` — the CPU must NOT park at a
    boundary the gate rejects (inside a non-resumable callback), and must park
    at the first boundary the gate accepts (the top-level / resumable one).
    This is the fix for a base captured mid-UpdateWindow that failed deep into
    replay with OrphanReturnError."""
    drv = _driver()
    resumable = {"ok": False}
    spun = {"n": 0}

    def worker():
        while drv.running:
            drv.check_pause()
            spun["n"] += 1
            time.sleep(0.0005)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    try:
        # Request a pause that only accepts a resumable boundary; the gate says
        # NO for now (simulating: parked inside UpdateWindow's WM_PAINT).
        parked = drv.pause_at_boundary(timeout=0.4,
                                       ready=lambda: resumable["ok"])
        assert not parked, "parked at a non-resumable boundary"
        moved = spun["n"]
        time.sleep(0.03)
        assert spun["n"] > moved, "worker wrongly blocked while gate said no"
        # The paint finishes: the boundary is now resumable.
        resumable["ok"] = True
        assert drv.paused.wait(2.0), "did not park once the gate accepted"
        frozen = spun["n"]
        time.sleep(0.03)
        assert spun["n"] == frozen, "kept running after the resumable park"
        drv.resume()
    finally:
        drv.stop()
        t.join(timeout=1.0)
