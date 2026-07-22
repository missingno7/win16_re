"""Win16ReplayDriver protocol + projection contract (win16/replay_driver.py).

The full capture/restore/replay path needs a real Win16 machine (game-side),
so the byte-exact oracle-vs-candidate verify lives in the consuming project.
Here we pin the machine-light halves: the declared projection contract, and
that project() extracts the game-observable fields and MASKS the recovered-
code ranges (the EXE-independence comparison seam) — with a minimal
duck-typed machine.
"""
from dataclasses import dataclass
from types import SimpleNamespace

from dos_re.replay import ReplayExecutionIdentity
from dos_re.verification_contract import VerificationRepresentation
from win16.replay_driver import (PROJECTION_CONTRACT, PROJECTION_SCHEMA,
                                  Win16ReplayDriver)


@dataclass
class _S:
    cs: int = 0x1234
    ip: int = 0x0056
    ax: int = 1
    flags: int = 0x0202


def _fake_machine(mem: bytes, *, windows=(), timers=None, clock_ms=0):
    cpu = SimpleNamespace(s=_S(), instruction_count=4242,
                          mem=SimpleNamespace(data=bytearray(mem)))
    sysobj = SimpleNamespace(
        windows=list(windows), timers=dict(timers or {}), clock_ms=clock_ms)
    machine = SimpleNamespace(
        cpu=cpu, mem=cpu.mem,
        api=SimpleNamespace(services={"system": sysobj}))
    return machine


def _win(name, pixels):
    return SimpleNamespace(wndclass=SimpleNamespace(name=name),
                           surface=SimpleNamespace(pixels=bytearray(pixels)))


def _driver(machine, *, ordinal=7, mask=()):
    d = Win16ReplayDriver(
        profile=ReplayExecutionIdentity(
            profile_id="p", role="candidate", implementation="i",
            image="IMG", runtime="win16-re", devices="d",
            continuation_schema="win16-re-continuation-v1",
            projection_schema=PROJECTION_SCHEMA),
        machine_factory=lambda: machine, mask_ranges=mask)
    d.machine = machine
    d._timeline = "t"
    d._input = SimpleNamespace(current_ordinal=ordinal, sys=object())
    d._finished = False
    return d


def test_projection_contract_shape():
    c = PROJECTION_CONTRACT
    assert c.schema_id == PROJECTION_SCHEMA
    assert c.representation is VerificationRepresentation.SEMANTIC_STATE
    # instruction_count IS a comparison field: generated bodies preserve
    # virtual time instruction-exactly, so it is signal, not carrier noise.
    assert "instruction_count" in c.required_fields
    assert "memory" in c.required_regions


def test_projection_extracts_observable_fields():
    m = _fake_machine(b"\x00" * 64,
                      windows=[_win("AntRoot", b"\x01\x02"),
                               _win("Ribbon", b"\x03")],
                      timers={5: 55, 1: 11}, clock_ms=62365)
    proj = _driver(m, ordinal=9).project()
    assert proj.schema_id == PROJECTION_SCHEMA
    assert proj.event_cursor == 9
    f = proj.fields
    assert f["instruction_count"] == 4242 and f["clock_ms"] == 62365
    assert f["windows"] == ["AntRoot", "Ribbon"]
    assert f["timers"] == ["1:11", "5:55"]          # sorted, deterministic
    assert len(f["surfaces"]) == 2                  # one hash per window
    assert f["cpu"]["cs"] == 0x1234


def test_projection_masks_recovered_code_ranges():
    mem = bytes(range(64))
    unmasked = _driver(_fake_machine(mem)).project().regions["memory"]
    assert unmasked == mem
    # A poisoned range hashes as zero so a boot-image run compares against an
    # EXE-full oracle; on the poisoned side the mask is a no-op by construction.
    masked = _driver(_fake_machine(mem), mask=[(16, 8)]).project()
    region = masked.regions["memory"]
    assert region[16:24] == b"\x00" * 8
    assert region[:16] == mem[:16] and region[24:] == mem[24:]


def test_current_point_needs_a_position():
    import pytest
    from dos_re.replay import ReplayError
    d = Win16ReplayDriver(
        profile=ReplayExecutionIdentity(
            profile_id="p", role="candidate", implementation="i", image="IMG",
            runtime="r", devices="d", continuation_schema="c",
            projection_schema=PROJECTION_SCHEMA),
        machine_factory=lambda: None)
    with pytest.raises(ReplayError, match="no position"):
        _ = d.current_point
