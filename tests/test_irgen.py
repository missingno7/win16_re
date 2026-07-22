"""win16.irgen — the Win16 front-end over dos_re's generic irgen core.

Synthetic, game-free (this repo's rule): a mock loaded machine with two
mapped NE segments, an import-thunk far call, a cross-segment game far call
and a raw INT 21h.  Pins the Win16 conventions the consuming game ports rely
on: paragraph-base ``CS:IP`` record keys, the ``ne_seg``/symbol identity
embedding, ``api:*`` effect tags from ``cpu.hook_names``, the DOS tagger
fallback for raw INT paths, NE-pair fact translation, and byte-identical
serialization.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from win16.irgen import build_ir, dump_document
from win16.loader import THUNK_SEG


@dataclass
class _Mem:
    data: bytearray
    sel_base: dict = field(default_factory=dict)


@dataclass
class _Cpu:
    hook_names: dict = field(default_factory=dict)


@dataclass
class _Machine:
    mem: _Mem
    cpu: _Cpu
    seg_bases: list


#: NE seg 1 at paragraph 0x0100, NE seg 2 at paragraph 0x0200.
_SEG_BASES = [0, 0x0100, 0x0200]

_ALPHA = bytes.fromhex(
    "9A08006000"      # 0010 call far 0060:0008   -> import thunk (API)
    "9A20000002"      # 0015 call far 0200:0020   -> game far call into seg 2
    "CD21"            # 001A int 21h              -> raw DOS path (C runtime)
    "C3")             # 001C ret

_BETA = bytes.fromhex("C3")   # 0020 ret


def _machine() -> _Machine:
    data = bytearray(0x4000)
    data[0x1000 + 0x10:0x1000 + 0x10 + len(_ALPHA)] = _ALPHA
    data[0x2000 + 0x20:0x2000 + 0x20 + len(_BETA)] = _BETA
    cpu = _Cpu(hook_names={(THUNK_SEG, 0x0008): "api:USER.MessageBeep"})
    return _Machine(_Mem(data), cpu, list(_SEG_BASES))


_NAMES = {(1, 0x0010): {"symbol": "_Alpha", "module": "MAIN_MODULE"},
          (2, 0x0020): {"symbol": "_Beta", "module": "SIM_MODULE"}}


def _build(**kw):
    kw.setdefault("names", _NAMES)
    kw.setdefault("exe", "SYNTH.EXE sha1=0")
    kw.setdefault("symbols", "SYNTH.SYM sha1=0")
    return build_ir(_machine(), [(1, 0x0010), (2, 0x0020)], **kw)


def test_records_are_keyed_by_paragraph_base_with_ne_identity():
    doc = _build()
    assert set(doc["functions"]) == {"0100:0010", "0200:0020"}
    alpha = doc["functions"]["0100:0010"]
    assert alpha["entry"] == "0100:0010"
    assert alpha["ne_seg"] == 1
    assert alpha["symbol"] == "_Alpha"
    assert alpha["module"] == "MAIN_MODULE"
    assert doc["functions"]["0200:0020"]["ne_seg"] == 2
    assert doc["provenance"]["symbols"] == "SYNTH.SYM sha1=0"
    assert doc["provenance"]["entries"] == 2


def test_api_far_calls_tag_and_raw_int_falls_through_to_dos_tagger():
    alpha = _build()["functions"]["0100:0010"]
    insts = [i for b in alpha["blocks"] for i in b["instructions"]]
    assert [(i["ip"], i.get("platform_effect")) for i in insts] == [
        ("0010", "api:USER.MessageBeep"),   # thunk far call, named from hooks
        ("0015", None),                     # game far call — no platform tag
        ("001A", "int21_dos"),              # DOS tagger fallback
        ("001C", None),
    ]
    # Both far edges are in the call list, paragraph-based.
    assert alpha["calls_far"] == [["0060", "0008"], ["0200", "0020"]]


def test_unnamed_thunk_slot_gets_the_slot_fallback_tag():
    m = _machine()
    m.cpu.hook_names.clear()
    doc = build_ir(m, [(1, 0x0010)], exe="SYNTH.EXE sha1=0")
    insts = [i for b in doc["functions"]["0100:0010"]["blocks"]
             for i in b["instructions"]]
    assert insts[0]["platform_effect"] == "api:slot_0008"


def test_facts_are_ne_pairs_translated_to_paragraph_space():
    doc = _build(environment_wait_entries=[(2, 0x0020)])
    assert doc["functions"]["0200:0020"]["platform_effect"] == "env_wait"
    assert doc["facts_applied"]["environment_wait_entries"] == ["0200:0020"]


def test_duplicate_entries_scan_once_and_dump_is_deterministic():
    doc = build_ir(_machine(), [(1, 0x0010), (1, 0x0010), (2, 0x0020)],
                   names=_NAMES, exe="SYNTH.EXE sha1=0")
    assert doc["provenance"]["entries"] == 2
    again = build_ir(_machine(), [(1, 0x0010), (2, 0x0020)],
                     names=_NAMES, exe="SYNTH.EXE sha1=0")
    assert dump_document(doc) == dump_document(again)
