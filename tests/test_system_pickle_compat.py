"""A snapshot outlives the class that wrote it.

Saved machines, crash dumps and generated boot images are pickles of
Win16System (win16/vmsnap.py).  Pickle restores __dict__ verbatim, so a field
added to the class later is simply ABSENT from every snapshot written before
it — and the first line of code to touch that field dies with AttributeError,
on evidence the project cannot regenerate cheaply.  Win16System.__setstate__
fills the defaults for exactly those fields.
"""
import dataclasses
import pickle

from win16.api.system import Win16System


def _bare_system():
    """A Win16System built WITHOUT __init__ (as unpickling does), carrying the
    field set of some earlier version of the class."""
    sysobj = Win16System.__new__(Win16System)
    state = {f.name: (f.default_factory() if f.default_factory is not dataclasses.MISSING
                      else (None if f.default is dataclasses.MISSING else f.default))
             for f in dataclasses.fields(Win16System)}
    return sysobj, state


def test_setstate_fills_a_field_the_pickle_predates():
    sysobj, state = _bare_system()
    del state["scheduled_messages"]         # written before the field existed
    sysobj.__setstate__(state)
    assert sysobj.scheduled_messages == []


def test_setstate_never_overrides_a_saved_value():
    sysobj, state = _bare_system()
    state["scheduled_messages"] = [(5, 1, 2, 3, 4)]
    state["clock_ms"] = 4321
    sysobj.__setstate__(state)
    assert sysobj.scheduled_messages == [(5, 1, 2, 3, 4)]
    assert sysobj.clock_ms == 4321


def test_each_restored_default_is_its_own_object():
    """A shared mutable default across two restored systems would alias their
    queues together."""
    a, sa = _bare_system()
    b, sb = _bare_system()
    del sa["scheduled_messages"], sb["scheduled_messages"]
    a.__setstate__(sa)
    b.__setstate__(sb)
    a.scheduled_messages.append((1, 2, 3, 4, 5))
    assert b.scheduled_messages == []


def test_a_pickled_system_round_trips():
    sysobj = Win16System.__new__(Win16System)
    _s, state = _bare_system()
    state["clock_ms"] = 99
    state["scheduled_messages"] = [(1, 2, 3, 4, 5)]
    sysobj.__setstate__(state)
    back = pickle.loads(pickle.dumps(sysobj))
    assert back.clock_ms == 99
    assert back.scheduled_messages == [(1, 2, 3, 4, 5)]
