"""win16.vmsnap capture contract: pickle_system detaches then RESTORES.

A capture (snapshot or replay-continuation base) pickles the OS object graph
with the live host wiring detached — machine, message_source, input_drainer,
yield_check — because those hold unpicklable state (threading locks, the 4 MB
machine ref).  The detach is TEMPORARY: it MUST be restored on return, so a
sequential caller's live object is unchanged.

The corollary the caller must honour (not testable here — it is GUI-thread
discipline): the CPU thread reads sysobj.machine on the hot path, so a capture
MUST park the CPU first.  An unparked capture races the detach window and
crashes ('NoneType has no api').  This bit play.py's F11 recorder after the
3.0 migration dropped the pause; the fix is caller-side (park then capture),
and the framework's crash was the correct fail-loud outcome — better than a
silently torn snapshot.
"""
from __future__ import annotations

from types import SimpleNamespace

from win16.vmsnap import pickle_system


def _fake_system():
    sysobj = SimpleNamespace(
        machine="MACHINE", message_source="SRC", input_drainer="DRAIN",
        yield_check="YIELD", windows=[], timers={}, clock_ms=7,
        huge_heap=None)
    return sysobj


def test_pickle_system_restores_the_detached_wiring():
    sysobj = _fake_system()
    blob = pickle_system(sysobj)
    assert isinstance(blob, bytes) and blob
    # every host-wiring attr is back after the call (the finally restored them)
    assert sysobj.machine == "MACHINE"
    assert sysobj.message_source == "SRC"
    assert sysobj.input_drainer == "DRAIN"
    assert sysobj.yield_check == "YIELD"


def test_the_pickled_graph_has_the_wiring_detached():
    import pickle
    sysobj = _fake_system()
    restored = pickle.loads(pickle_system(sysobj))
    # the SERIALIZED copy carries None for the host wiring (re-attached on
    # resume), while the live object above keeps its values.
    assert restored.machine is None
    assert restored.message_source is None
    assert restored.clock_ms == 7          # game state survives verbatim


def test_restore_happens_even_if_pickling_raises():
    sysobj = _fake_system()
    sysobj.windows = _Unpicklable()        # force pickle.dumps to raise
    import pytest
    with pytest.raises(Exception):
        pickle_system(sysobj)
    assert sysobj.machine == "MACHINE"     # restored despite the failure


class _Unpicklable:
    def __reduce__(self):
        raise TypeError("cannot pickle this")
