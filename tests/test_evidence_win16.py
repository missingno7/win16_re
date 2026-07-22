"""Win16 evidence probe: entry visits + dispatch-target binding (win16/evidence.py).

Pins the observation rules: an executed dispatch site binds its target from
the NEXT observation — an interpreted instruction (guest target) or a hook
dispatch (API thunk, bound by name) — and function entries count true
invocations with a first-entry replay point.  Identity keying is pinned
against the exact key grammar the static Recovery-IR Atlas import uses.
"""
from dos_re.identity import (BoundaryIdentity, ExecutionPointIdentity,
                             FunctionIdentity, ImageIdentity, ProgramIdentity)
from dos_re.replay import ReplayExecutionIdentity
from win16.evidence import (EntryOnlyVisits, Win16EvidenceProbe,
                            dispatch_sites, entry_set, finish, modrm_reg)

IMAGE = ImageIdentity(ProgramIdentity("game:1.0"), "GAME.EXE", "sha256",
                      "0" * 64)


def _profile() -> ReplayExecutionIdentity:
    return ReplayExecutionIdentity(
        profile_id="p", role="oracle", implementation="i",
        image="GAME.EXE", runtime="r", devices="d",
        continuation_schema="c", projection_schema="s")


def _probe(ordinal_box):
    return Win16EvidenceProbe(
        frozenset({(0x100, 0x10), (0x200, 0x20)}),
        frozenset({(0x100, 0x55)}),
        lambda: ordinal_box[0])


def test_entries_count_true_invocations_with_first_point():
    box = [3]
    p = _probe(box)
    p.record_interpreted_instruction((0x100, 0x10))
    box[0] = 7
    p.record_interpreted_instruction((0x100, 0x10))
    p.record_interpreted_instruction((0x300, 0x99))     # not an entry
    assert p.counts == {(0x100, 0x10): 2}
    assert p.first_ord == {(0x100, 0x10): 3}


def test_site_binds_guest_target_on_next_interpreted_instruction():
    box = [5]
    p = _probe(box)
    p.record_interpreted_instruction((0x100, 0x55))     # the dispatch site
    p.record_interpreted_instruction((0x200, 0x20))     # its resolved target
    assert p.dyn == {(0x100, 0x55): {(0x200, 0x20): [1, 5, 5]}}
    assert p.pending is None


def test_site_binds_api_thunk_by_hook_name():
    box = [2]
    p = _probe(box)
    p.record_interpreted_instruction((0x100, 0x55))
    p.record_hook_unverified((0x60, 0x8), "api:USER.1:MessageBeep")
    assert p.dyn == {(0x100, 0x55): {"api:USER.1:MessageBeep": [1, 2, 2]}}


def test_repeat_bindings_accumulate_count_and_last_point():
    box = [1]
    p = _probe(box)
    for ordinal in (1, 4):
        box[0] = ordinal
        p.record_interpreted_instruction((0x100, 0x55))
        p.record_interpreted_instruction((0x200, 0x20))
    assert p.dyn[(0x100, 0x55)][(0x200, 0x20)] == [2, 1, 4]


def test_finish_emits_static_import_compatible_identities():
    box = [0]
    p = _probe(box)
    p.record_interpreted_instruction((0x100, 0x10))     # a visit
    p.record_interpreted_instruction((0x100, 0x55))     # site ->
    p.record_interpreted_instruction((0x200, 0x20))     #   guest function
    p.record_interpreted_instruction((0x100, 0x55))     # site ->
    p.record_hook_unverified((0x60, 0x8), "api:USER.1:MessageBeep")

    p.record_callback("DispatchMessage", 0x200, 0x20)   # a WndProc dispatch
    evidence, visits, roots = finish(
        p, image=IMAGE, address_space="win16-para", timeline_id="t",
        profile=_profile(), site_kinds={(0x100, 0x55): "call_ind"},
        provenance={"observer": "test"})
    # The callback target is a known function entry -> it is a coverage ROOT,
    # and the dispatch lands as a "callback" transfer from the API boundary.
    assert roots == (FunctionIdentity(IMAGE, "win16-para", "0200:0020").key,)
    cb = [t for t in evidence.transfers if t.kind == "callback"]
    assert len(cb) == 1 and cb[0].source == BoundaryIdentity(
        ProgramIdentity("game:1.0"), "platform-effect",
        "api:DispatchMessage").key

    # BOTH functions were entered: 0100:0010 directly, and 0200:0020 as the
    # dispatch target (its execution is an entry observation like any other).
    assert [v.function_id for v in visits.records()] == [
        FunctionIdentity(IMAGE, "win16-para", "0100:0010").key,
        FunctionIdentity(IMAGE, "win16-para", "0200:0020").key]
    assert all(v.incomplete for v in visits.records())

    ids = {(t.source_id, t.target_id, t.kind) for t in evidence.transfers}
    site = ExecutionPointIdentity(IMAGE, "win16-para", "0100:0055").key
    assert ids == {
        (site, FunctionIdentity(IMAGE, "win16-para", "0200:0020").key,
         "call_ind"),
        (site, BoundaryIdentity(ProgramIdentity("game:1.0"),
                                "platform-effect",
                                "api:USER.1:MessageBeep").key, "call_ind"),
    }


def test_ir_walkers_extract_entries_and_far_indirect_sites():
    ir = {"functions": {"0100:0010": {"blocks": [{"instructions": [
        {"kind": "call_ind", "ip": "0055", "bytes": "ff1e3412"},   # /3 far
        {"kind": "call_ind", "ip": "0060", "bytes": "ff163412"},   # /2 near
        {"kind": "call_ind", "ip": "0070", "bytes": "ff063412"},   # /0 inc — not a site
        {"kind": "jmp_ind", "ip": "0080", "bytes": "ff263412"},    # /4 near jmp
    ]}]}}}
    assert entry_set(ir) == frozenset({(0x100, 0x10)})
    assert dispatch_sites(ir) == {(0x100, 0x55): "call_ind",
                                  (0x100, 0x60): "call_ind",
                                  (0x100, 0x80): "jmp_ind"}
    assert modrm_reg("ff1e3412") == 3 and modrm_reg("2eff263412") == 4
