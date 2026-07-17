"""win16.apicoverage — the IR-vs-registry API coverage join.

Synthetic, game-free (this repo's rule): a hand-written mini recovery-IR
document + a real ``ApiRegistry`` on a mock machine.  Pins the join contracts
consuming game ports rely on: global ``(CS, IP)`` site dedupe across
overlapping dispatch-fact records, honest ``unnamed`` identity (never guessed
from ordinal folklore), handler/equate/tripwire status, live dispatch
counting through the thunk hooks (static slots AND GetProcAddress-minted
procs, incl. NULL-mint misses), per-service interrupt counting, and the
classification axes of the report.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from win16.api.core import ApiRegistry, Win16ApiGap
from win16.apicoverage import (
    build_coverage, format_table, implementation_status, instrument_machine,
    parse_api_tag, resolve_name, static_usage)

THUNK_SEG = 0x0060

# The exemplar for the UNNAMED path: imported, slot allocated, no handler and
# no name anywhere.  It must be an ordinal the real Wine table can never
# resolve, or the fixture rots the moment a game proves a real ordinal and it
# gains a name — which is exactly what happened to the previous choice, GDI.44
# (now SelectClipRgn).  GDI exports nothing near 4000.
UNNAMED_ORD = 4000
UNNAMED_TARGET = f"GDI.{UNNAMED_ORD}"


# --------------------------------------------------------------------------
# the synthetic machine (mock CPU + flat byte memory — enough for dispatch)
# --------------------------------------------------------------------------

class _Mem:
    def __init__(self) -> None:
        self.b = bytearray(0x20000)

    def _lin(self, seg: int, off: int) -> int:
        return (seg * 16 + (off & 0xFFFF)) % len(self.b)

    def rb(self, seg, off):
        return self.b[self._lin(seg, off)]

    def wb(self, seg, off, val):
        self.b[self._lin(seg, off)] = val & 0xFF

    def rw(self, seg, off):
        return self.rb(seg, off) | (self.rb(seg, off + 1) << 8)

    def ww(self, seg, off, val):
        self.wb(seg, off, val)
        self.wb(seg, off + 1, val >> 8)


@dataclass
class _State:
    ax: int = 0
    dx: int = 0
    cs: int = 0
    ip: int = 0
    ss: int = 0x1000
    sp: int = 0x0100


@dataclass
class _Cpu:
    mem: _Mem = field(default_factory=_Mem)
    s: _State = field(default_factory=_State)
    replacement_hooks: dict = field(default_factory=dict)
    hook_names: dict = field(default_factory=dict)
    interrupt_handler: object = None


@dataclass
class _Machine:
    cpu: _Cpu
    api: ApiRegistry


def _registry() -> ApiRegistry:
    api = ApiRegistry()

    @api.register_raw("USER", 109)              # named by the ordinal table
    def PeekMessage(ctx):
        pass

    @api.register_raw("KERNEL", 22)             # NOT in the ordinal table —
    def GlobalFlags(ctx):                       # named by handler __name__
        pass

    api.register_equate("KERNEL", 114, 8)       # __AHINCR

    @api.register_proc("MMSYSTEM", "midiOutOpen", ret="word")
    def midiOutOpen(ctx):
        return 0

    return api


def _machine() -> _Machine:
    api = _registry()
    # The loader-facing slot allocation: the unnamed target gets a slot but has no
    # handler — the tripwire case.
    for module, ordinal in (("USER", 109), ("KERNEL", 22), ("GDI", UNNAMED_ORD)):
        kind, _ = api.resolve_import(module, ordinal)
        assert kind == "thunk"
    kind, value = api.resolve_import("KERNEL", 114)
    assert (kind, value) == ("equate", 8)
    cpu = _Cpu()
    api.install(cpu, THUNK_SEG)
    return _Machine(cpu, api)


# --------------------------------------------------------------------------
# the synthetic IR document
# --------------------------------------------------------------------------

def _fn(entry, symbol, insts, *, liftable=True, origin=None):
    ip = entry.split(":")[1]
    rec = {"entry": entry, "symbol": symbol, "ne_seg": 1, "liftable": liftable,
           "blocks": [{"leader": ip, "instructions": insts}]}
    if origin:
        rec["entry_origin"] = origin
    return rec


_DOC = {
    "provenance": {"exe": "SYNTH.EXE sha1=0"},
    "functions": {
        "0100:0010": _fn("0100:0010", "_Alpha", [
            {"ip": "0010", "kind": "call_far",
             "platform_effect": "api:USER.109:PeekMessage"},
            {"ip": "0015", "kind": "call_far",
             "platform_effect": "api:KERNEL.22"},
            {"ip": "001A", "kind": "int", "platform_effect": "int21_dos"},
        ]),
        # A dispatch-fact case entry overlapping _Alpha's bytes: the SAME
        # call site at 0100 IP 0015 — must dedupe, attributed to _Alpha.
        "0100:0015": _fn("0100:0015", "case_0015", [
            {"ip": "0015", "kind": "call_far",
             "platform_effect": "api:KERNEL.22"},
        ], origin="dispatch-fact"),
        "0200:0020": _fn("0200:0020", "_Beta", [
            {"ip": "0020", "kind": "call_far",
             "platform_effect": "api:USER.109:PeekMessage"},
            {"ip": "0025", "kind": "call_far",
             "platform_effect": f"api:GDI.{UNNAMED_ORD}"},
            {"ip": "002A", "kind": "call_far",
             "platform_effect": "api:slot_0044"},
        ]),
        # Refused scans carry no reliable edges — must be skipped.
        "0200:0030": _fn("0200:0030", "_Dead", [
            {"ip": "0030", "kind": "call_far",
             "platform_effect": "api:USER.109:PeekMessage"},
        ], liftable=False),
    },
}


# --------------------------------------------------------------------------
# static join
# --------------------------------------------------------------------------

def test_parse_api_tag():
    assert parse_api_tag("api:USER.109:PeekMessage") == ("USER", 109, "PeekMessage")
    assert parse_api_tag("api:KERNEL.90") == ("KERNEL", 90, None)
    assert parse_api_tag("api:slot_0044") is None


def test_static_usage_dedupes_overlapping_records_and_skips_unliftable():
    api_sites, int_sites, unresolved = static_usage(_DOC)
    assert set(api_sites) == {("USER", 109), ("KERNEL", 22), ("GDI", UNNAMED_ORD)}
    # Two distinct PeekMessage sites (the non-liftable _Dead one is skipped).
    assert api_sites[("USER", 109)]["sites"] == ["0100:0010", "0200:0020"]
    assert api_sites[("USER", 109)]["callers"] == {"_Alpha", "_Beta"}
    # ONE KERNEL.22 site: the case_0015 record overlaps it; attribution goes
    # to the record without a generated entry_origin.
    assert api_sites[("KERNEL", 22)]["sites"] == ["0100:0015"]
    assert api_sites[("KERNEL", 22)]["callers"] == {"_Alpha"}
    assert int_sites == {"int21_dos": 1}
    assert unresolved == {"api:slot_0044": 1}


def test_identity_resolution_is_honest():
    api = _registry()
    assert resolve_name(api, "USER", 109) == ("PeekMessage", "ordinal-table")
    assert resolve_name(api, "KERNEL", 22) == ("GlobalFlags", "handler-name")
    # KERNEL.114 has no handler but the ordinal table knows the equate.
    assert resolve_name(api, "KERNEL", 114) == ("__AHINCR", "ordinal-table")
    # No table entry, no handler, no IR name -> unnamed, never guessed.
    assert resolve_name(api, "GDI", UNNAMED_ORD) == (None, "unnamed")
    # The IR tag's name part is the last resort before unnamed.
    assert resolve_name(api, "GDI", UNNAMED_ORD, ir_name="TagName") == ("TagName", "ir-tag")


def test_implementation_status():
    api = _registry()
    assert implementation_status(api, "USER", 109) == "handler-raw"
    assert implementation_status(api, "KERNEL", 114) == "equate"
    assert implementation_status(api, "GDI", UNNAMED_ORD) == "tripwire"


# --------------------------------------------------------------------------
# runtime instrumentation
# --------------------------------------------------------------------------

def test_instrumented_dispatch_counts_slots_procs_and_ints():
    machine = _machine()
    cpu, api = machine.cpu, machine.api

    ints_seen = []
    cpu.interrupt_handler = lambda c, num: ints_seen.append(num)

    counts = instrument_machine(machine, description="synthetic run")
    # Instrumentation is idempotent (a second call must not double-count).
    counts2 = instrument_machine(machine)
    assert counts2.api == {}

    peek = (THUNK_SEG, api.slots[("USER", 109)])
    for _ in range(3):
        cpu.replacement_hooks[peek](cpu)
    assert counts.api == {("USER", 109): 3}

    # A tripwire dispatch still fails loud through the wrapper.
    trip = (THUNK_SEG, api.slots[("GDI", UNNAMED_ORD)])
    with pytest.raises(Win16ApiGap):
        cpu.replacement_hooks[trip](cpu)

    # GetProcAddress minting: implemented -> wrapped thunk; unknown -> NULL,
    # recorded as a mint miss.
    far = api.mint_proc_thunk("MMSYSTEM", "midiOutOpen")
    assert far and (far >> 16) == THUNK_SEG
    assert api.mint_proc_thunk("MMSYSTEM", "midiOutOpen") == far  # idempotent
    cpu.replacement_hooks[(THUNK_SEG, far & 0xFFFF)](cpu)
    assert counts.procs == {("MMSYSTEM", "midiOutOpen"): 1}
    assert api.mint_proc_thunk("MMSYSTEM", "waveOutOpen") == 0
    assert counts.mint_misses == {("MMSYSTEM", "waveOutOpen"): 1}

    # Interrupts count per service (INT 21h keys on AH), original still runs.
    cpu.s.ax = 0x3D00
    cpu.interrupt_handler(cpu, 0x21)
    cpu.interrupt_handler(cpu, 0x21)
    cpu.s.ax = 0x4C00
    cpu.interrupt_handler(cpu, 0x21)
    cpu.interrupt_handler(cpu, 0x2F)
    assert counts.ints == {"int21:3D": 2, "int21:4C": 1, "int2F": 1}
    assert ints_seen == [0x21, 0x21, 0x21, 0x2F]


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------

def test_build_coverage_static_only():
    machine = _machine()
    report = build_coverage(_DOC, machine.api)
    t = report["targets"]
    assert set(t) == {"USER.109", "KERNEL.22", "KERNEL.114", UNNAMED_TARGET}
    assert t["USER.109"]["classification"] == "implemented"
    assert t["USER.109"]["static_sites"] == 2
    assert t["USER.109"]["runtime_calls"] is None
    assert t["KERNEL.114"] == {
        "module": "KERNEL", "ordinal": 114, "name": "__AHINCR",
        "name_source": "ordinal-table", "unnamed": False,
        "implemented": "equate", "imported": True, "static_sites": 0,
        "callers": [], "runtime_calls": None, "classification": "equate"}
    assert t[UNNAMED_TARGET]["classification"] == "unimplemented-tripwire"
    assert t[UNNAMED_TARGET]["unnamed"] is True
    assert report["summary"]["exercised"] is None
    assert report["summary"]["unnamed"] == 1
    assert report["ints"]["static_sites"] == {"int21_dos": 1}
    assert report["ints"]["runtime"] is None
    assert report["unresolved_api_tags"] == {"api:slot_0044": 1}


def test_build_coverage_with_runtime_classifies_and_lists_dynamic_surface():
    machine = _machine()
    counts = instrument_machine(machine, description="synthetic run")
    cpu, api = machine.cpu, machine.api
    cpu.replacement_hooks[(THUNK_SEG, api.slots[("USER", 109)])](cpu)
    far = api.mint_proc_thunk("MMSYSTEM", "midiOutOpen")
    cpu.replacement_hooks[(THUNK_SEG, far & 0xFFFF)](cpu)
    api.mint_proc_thunk("MMSYSTEM", "waveOutOpen")

    report = build_coverage(_DOC, api, runtime=counts)
    t = report["targets"]
    assert t["USER.109"]["classification"] == "implemented+exercised"
    assert t["USER.109"]["runtime_calls"] == 1
    assert t["KERNEL.22"]["classification"] == "implemented+never-exercised"
    s = report["summary"]
    assert (s["exercised"], s["never_exercised"]) == (1, 1)
    d = report["dynamic_procs"]
    assert d["MMSYSTEM.midiOutOpen"] == {
        "implemented": True, "minted": True,
        "runtime_calls": 1, "mint_misses": 0}
    assert d["MMSYSTEM.waveOutOpen"] == {
        "implemented": False, "minted": False,
        "runtime_calls": 0, "mint_misses": 1}
    assert report["provenance"]["runtime"] == "synthetic run"


def test_format_table_lists_risk_and_dynamic_sections():
    machine = _machine()
    counts = instrument_machine(machine)
    cpu, api = machine.cpu, machine.api
    cpu.replacement_hooks[(THUNK_SEG, api.slots[("USER", 109)])](cpu)
    counts.description = "demo=synthetic"
    text = format_table(build_coverage(_DOC, api, runtime=counts))
    assert "PeekMessage" in text
    assert "implemented+never-exercised" in text
    assert "GlobalFlags" in text            # the risk list names KERNEL.22
    assert "(unnamed)" in text              # the unnamed target, reported honestly
    assert "int21_dos" in text
    assert "MMSYSTEM.midiOutOpen" in text
