"""The Win16 LIFTING-CONFORMANCE fence.

dos_re owns the generic code-shape mechanisms and learns no operating system.
That is right — but it left Windows with no fence of its own: the only Win16
evidence in existence lived in a game project two repositories downstream, so a
dos_re frame-model change could break every Win16 binary in the world and
nothing in this repo would notice.

This suite is that notice.  Every fixture is **synthetic and hand-assembled**
here — small byte arrays written from the Win16 calling convention, not
extracted from any binary.  There is no game, no asset, no EXE.  Run
``pytest tests/test_win16_lifting_conformance.py`` on a bare checkout and it
proves that the shapes a 16-bit Windows compiler actually emits still lift, and
still compute what the CPU computes.

What is fenced:

* **the far-entry prologue** — ``inc bp ; push bp ; mov bp,sp`` ... ``mov sp,bp
  ; pop bp ; dec bp ; retf N``.  The ``inc bp`` is not decoration: it tags the
  saved frame pointer so Windows' stack walker can tell a FAR frame from a near
  one, and it means bp is saved one instruction BEFORE it becomes the frame
  base.  A frame model that only credits a ``push bp`` made when bp already
  holds the base refuses every exported function in every Win16 program.
* **``__loadds``** in all three preambles a Win16 image can carry — the
  self-loading ``push ds ; pop ax ; nop``, the ``mov ax,ds ; nop`` form, and
  the loader-PATCHED ``mov ax,DGROUP`` that overwrites those same three bytes
  at load time — each followed by ``inc bp ; push bp ; mov bp,sp ; push ds ;
  mov ds,ax``, with ``sub bp,2`` biasing the frame pointer onto the saved DS so
  the single ``mov sp,bp`` teardown lands on it.
* **DS != SS**, which is the whole reason ``__loadds`` exists.  A far-model
  Win16 app runs with a stack segment that is not its data segment, and the
  fixtures prove a lifted body keeps them apart: DIFFERENT bytes are seeded at
  the SAME offset in DS and in SS, the body reads through one and writes
  through the other, and a body that conflated them lands on the wrong bytes.
* **an exported far entry vs an internal near one** — same body, different
  convention; the recovered return contract must follow.
* **a static far call into the import-thunk segment**, composed as a
  ``plat.farcall`` platform effect with the pascal callee-cleanup produced by
  :mod:`win16.lift` off the real API registry — the fact producer and the
  emitter that consumes it, checked against each other.

Every behavioural claim is a DIFFERENTIAL: the composed CPUless body is exec'd
and its whole register file plus its stack memory are diffed against stepping
the identical bytes through ``dos_re.cpu.CPU8086``.  A fixture that merely
promotes proves nothing; it has to agree with the CPU.
"""
from __future__ import annotations

import inspect
import sys
import types

import pytest

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.cfg import scan_function
from dos_re.lift.emit_cpuless import (PlatformFarCall, Refusal,
                                      check_promotable, emit_adapter,
                                      emit_recovered)
from dos_re.memory import Memory

from win16.api.surface import WINFLAGS_NO_FPU, build_registry
from win16.lift import (API_DISPATCH_COST, THUNK_SEG, plat_farcall_contracts,
                        plat_farcalls_document)

CS = 0x2000                     # where the fixture body is assembled
SS = 0x3000                     # the stack segment ...
DS = 0x5000                     # ... deliberately NOT the data segment
SP0 = 0x0100
DGROUP = 0x6000                 # what a loader-patched `mov ax,DGROUP` loads

#: registers compared across the differential (sp is checked separately —
#: whether it is a runtime output is part of the recovered contract)
CMP_REGS = ("ax", "bx", "cx", "dx", "bp", "si", "di")


# ---------------------------------------------------------------- fixtures --
#
# Hand-assembled Win16 shapes.  Each is written as (mnemonic, bytes) pairs so
# the assembly stays readable and the byte string cannot drift from it.

def _asm(*pairs: tuple[str, str]) -> bytes:
    return bytes.fromhex("".join(hexs for _, hexs in pairs))


#: the shared frame body: two locals, an add, a read-back.  Addressed at the
#: bias the caller's prologue establishes.
def _frame_body(disp_a: int, disp_b: int) -> tuple[tuple[str, str], ...]:
    a, b = disp_a & 0xFF, disp_b & 0xFF
    return (
        ("sub sp, 4", "83ec04"),
        (f"mov [bp{disp_a:+d}], ax", f"8946{a:02x}"),
        ("add ax, cx", "03c1"),
        (f"mov [bp{disp_b:+d}], ax", f"8946{b:02x}"),
        (f"mov dx, [bp{disp_a:+d}]", f"8b56{a:02x}"),
    )


#: An EXPORTED far entry, no __loadds.  The `inc bp` tags the saved frame
#: pointer as FAR for Windows' stack walker; `dec bp` after the pop removes it.
FAR_ENTRY = _asm(
    ("inc bp", "45"),                       # tag the frame pointer
    ("push bp", "55"),                      # saved BEFORE it becomes the base
    ("mov bp, sp", "8bec"),
    *_frame_body(-4, -2),
    ("mov sp, bp", "8be5"),
    ("pop bp", "5d"),
    ("dec bp", "4d"),                       # untag
    ("retf 4", "ca0400"),                   # pascal callee cleanup
)

#: The same body as an INTERNAL near function: no tag, no cleanup, near ret.
NEAR_ENTRY = _asm(
    ("push bp", "55"),
    ("mov bp, sp", "8bec"),
    *_frame_body(-4, -2),
    ("mov sp, bp", "8be5"),
    ("pop bp", "5d"),
    ("ret", "c3"),
)

#: The three preambles that can occupy the first three bytes of a __loadds far
#: entry.  All three leave the DS the body will install in AX.
LOADDS_PREAMBLES = {
    # the self-loading form as linked: DS is republished through AX
    "push_ds_pop_ax": (("push ds", "1e"), ("pop ax", "58"), ("nop", "90")),
    # the equivalent single-instruction form some compilers emit
    "mov_ax_ds": (("mov ax, ds", "8cd8"), ("nop", "90")),
    # what the Windows loader PATCHES over those same three bytes for a fixed
    # DGROUP: AX gets a segment that is genuinely not the incoming DS
    "patched_dgroup": (("mov ax, DGROUP", f"b8{DGROUP & 0xFF:02x}"
                        f"{DGROUP >> 8:02x}"),),
}


def _loadds_entry(preamble: tuple[tuple[str, str], ...]) -> bytes:
    """A __loadds far entry: preamble, tagged far frame, DS saved and
    reloaded, the frame pointer BIASED onto the saved DS so one `mov sp,bp`
    tears the frame down to it.

    The body reads [0x1000] through DS and writes [bp-2] through SS — the two
    segments must stay apart.
    """
    return _asm(
        *preamble,
        ("inc bp", "45"),
        ("push bp", "55"),
        ("mov bp, sp", "8bec"),
        ("push ds", "1e"),                  # the caller's DS, restored on exit
        ("mov ds, ax", "8ed8"),             # __loadds: install our own
        ("sub sp, 4", "83ec04"),
        ("sub bp, 2", "83ed02"),            # bias bp onto the saved-DS slot
        ("mov cx, [0x1000]", "8b0e0010"),   # a DS-relative read
        ("mov [bp-2], cx", "894efe"),       # an SS-relative write
        ("add cx, ax", "03c8"),
        ("mov dx, [bp-2]", "8b56fe"),       # read the SS slot back
        ("mov sp, bp", "8be5"),             # lands on the saved DS (bias -2)
        ("pop ds", "1f"),                   # restore the caller's DS
        ("pop bp", "5d"),
        ("dec bp", "4d"),
        ("retf 4", "ca0400"),
    )


# ------------------------------------------------------------- differential --

def _scan(code: bytes):
    return scan_function(lambda o: code[o] if o < len(code) else 0x90, 0)


def _exit_offset(code: bytes) -> int:
    """Offset of the terminating ret/retf — where the body's own effect ends
    (the return itself is the adapter's job, not the body's)."""
    scan = _scan(code)
    exits = [off for off, i in scan.insts.items()
             if i.kind in ("ret", "retf", "iret")]
    assert len(exits) == 1, f"fixture must have exactly one exit, got {exits}"
    return exits[0]


def _seed(mem: Memory, seed: dict[int, int]) -> None:
    for addr, val in seed.items():
        mem.data[addr] = val


def _interp(code: bytes, inputs: dict, mem: Memory) -> CPUState:
    """Step CPU8086 through the body, stopping AT the terminating ret."""
    st = CPUState(cs=CS, ip=0, ss=SS,
                  **{k: v for k, v in inputs.items() if k != "sp"})
    st.sp = inputs["sp"]
    cpu = CPU8086(mem, st)
    for k, b in enumerate(code):
        mem.data[(CS << 4) + k] = b
    stop = _exit_offset(code)
    while cpu.s.ip < stop:
        cpu.step()
    return cpu.s


def _run_body(code: bytes, inputs: dict, mem: Memory) -> dict:
    scan = _scan(code)
    spec = check_promotable(scan)
    src = emit_recovered(scan, spec.abi, f"{CS:04X}:0000",
                         recovered_import_base="x", needs_plat=spec.needs_plat,
                         df_livein=spec.df_livein, sp_output=spec.sp_output,
                         flags_livein=spec.flags_livein)
    ns: dict = {"_PARITY": [0] * 256}
    exec(compile(src, "<rec>", "exec"), ns)
    fn = next(v for k, v in ns.items() if k.startswith("func_"))
    # the recovered body takes exactly its ABI-scanned live-ins; handing it a
    # register it proved dead would be testing a signature we did not recover.
    accepted = set(inspect.signature(fn).parameters)
    out, _compat = fn(mem=mem, ss=SS,
                      **{k: v for k, v in inputs.items() if k in accepted})
    return out


def _differential(code: bytes, inputs: dict, seed: dict[int, int] | None = None
                  ) -> dict:
    """Run the lifted body and the interpreter over identical state; diff the
    register file and the whole stack segment.  Returns the body's outputs."""
    m_body, m_interp = Memory(), Memory()
    for m in (m_body, m_interp):
        _seed(m, seed or {})
        for k, b in enumerate(code):        # identical images before the run
            m.data[(CS << 4) + k] = b
    out = _run_body(code, dict(inputs), m_body)
    s = _interp(code, dict(inputs), m_interp)
    for r in CMP_REGS:
        got = out[r] if r in out else inputs.get(r, 0)
        assert got & 0xFFFF == getattr(s, r) & 0xFFFF, (
            f"{r}: lifted={got & 0xFFFF:04X} interp={getattr(s, r):04X}")
    # the WHOLE address space, not a window: a body that wrote through the
    # wrong segment lands somewhere, and it must land in the same place the
    # CPU put it.  (The interpreter also wrote the code bytes at CS; the
    # lifted run gets them seeded below so the images start identical.)
    assert bytes(m_body.data) == bytes(m_interp.data), "memory diverged"
    return out


# -------------------------------------------------- the far-entry prologue --

def test_far_entry_prologue_promotes_with_the_pascal_return_contract() -> None:
    """`inc bp; push bp; mov bp,sp` ... `pop bp; dec bp; retf 4` is the shape
    of EVERY exported Win16 function.  It must promote, and the recovered
    contract must carry the far return and the 4-byte callee cleanup."""
    spec = check_promotable(_scan(FAR_ENTRY))
    assert spec.ret_kind == "far"
    assert spec.ret_pop == 4
    assert spec.sp_output is False        # the frame is balanced


def test_far_entry_prologue_matches_the_interpreter() -> None:
    out = _differential(
        FAR_ENTRY,
        {"ax": 0x1234, "cx": 0x1111, "bx": 0, "dx": 0, "si": 0, "di": 0,
         "bp": 0x7777, "sp": SP0, "ds": DS, "es": 0})
    assert out["ax"] & 0xFFFF == (0x1234 + 0x1111) & 0xFFFF
    assert out["dx"] & 0xFFFF == 0x1234           # the local round-tripped


def test_the_far_tag_is_carried_through_the_whole_frame() -> None:
    """bp enters biased +1 across the push and leaves un-biased — so a caller's
    bp survives the call.  If a frame model dropped the tag, bp would come back
    off by one and this fails."""
    entry_bp = 0x7777
    out = _differential(
        FAR_ENTRY,
        {"ax": 1, "cx": 2, "bx": 0, "dx": 0, "si": 0, "di": 0,
         "bp": entry_bp, "sp": SP0, "ds": DS, "es": 0})
    assert out.get("bp", entry_bp) & 0xFFFF == entry_bp


def test_exported_far_entry_and_internal_near_entry_differ_only_in_convention(
) -> None:
    """The same frame body under the two Win16 conventions.  Both promote; the
    return contract follows the convention, and the computation does not."""
    far_spec = check_promotable(_scan(FAR_ENTRY))
    near_spec = check_promotable(_scan(NEAR_ENTRY))
    assert (far_spec.ret_kind, far_spec.ret_pop) == ("far", 4)
    assert (near_spec.ret_kind, near_spec.ret_pop) == ("near", 0)

    inputs = {"ax": 0x0F0F, "cx": 0x0101, "bx": 0, "dx": 0, "si": 0, "di": 0,
              "bp": 0x7777, "sp": SP0, "ds": DS, "es": 0}
    far_out = _differential(FAR_ENTRY, inputs)
    near_out = _differential(NEAR_ENTRY, inputs)
    for r in ("ax", "dx"):
        assert far_out[r] == near_out[r]


# ------------------------------------------------------------- __loadds ----

@pytest.mark.parametrize("preamble_name", sorted(LOADDS_PREAMBLES))
def test_loadds_far_entry_promotes(preamble_name: str) -> None:
    """All three preambles sit in front of the same tagged, DS-saving,
    bias-torn-down far frame, and all three must lift."""
    code = _loadds_entry(LOADDS_PREAMBLES[preamble_name])
    spec = check_promotable(_scan(code))
    assert spec.ret_kind == "far" and spec.ret_pop == 4
    assert spec.sp_output is False


@pytest.mark.parametrize("preamble_name", sorted(LOADDS_PREAMBLES))
def test_loadds_far_entry_keeps_ds_and_ss_apart(preamble_name: str) -> None:
    """The DS != SS contract, which is the entire point of __loadds.

    DIFFERENT bytes are seeded at offset 0x1000 in DS, in SS, and in the
    loader-patched DGROUP.  The body reads [0x1000] through DS and writes
    [bp-2] through SS.  A lifted body that conflated the two segments reads the
    wrong word and the differential catches it — as does the explicit
    assertion on the value that came back.
    """
    code = _loadds_entry(LOADDS_PREAMBLES[preamble_name])
    seed = {
        (DS << 4) + 0x1000: 0xCD, (DS << 4) + 0x1001: 0xAB,        # 0xABCD
        (SS << 4) + 0x1000: 0x11, (SS << 4) + 0x1001: 0x22,        # 0x2211
        (DGROUP << 4) + 0x1000: 0x34, (DGROUP << 4) + 0x1001: 0x12,  # 0x1234
    }
    out = _differential(
        code,
        {"ax": 0, "bx": 0, "cx": 0, "dx": 0, "si": 0, "di": 0,
         "bp": 0x7777, "sp": SP0, "ds": DS, "es": 0},
        seed=seed)

    # which segment the read landed in depends on the preamble, and that is
    # exactly the fact being pinned: the patched form installs DGROUP, the
    # other two republish the incoming DS.
    installed = DGROUP if preamble_name == "patched_dgroup" else DS
    expected_word = {DS: 0xABCD, DGROUP: 0x1234}[installed]
    # cx was read from [0x1000] through the installed DS, then ax was added
    assert out["cx"] & 0xFFFF == (expected_word + installed) & 0xFFFF
    assert out["cx"] & 0xFFFF != (0x2211 + installed) & 0xFFFF, \
        "the DS-relative read landed in SS"
    # and the SS write round-tripped through the frame slot
    assert out["dx"] & 0xFFFF == expected_word


@pytest.mark.parametrize("preamble_name", sorted(LOADDS_PREAMBLES))
def test_loadds_restores_the_callers_ds(preamble_name: str) -> None:
    """`push ds` ... `pop ds` around the body: the caller's DS must come back
    out unchanged, whatever the preamble installed."""
    code = _loadds_entry(LOADDS_PREAMBLES[preamble_name])
    seed = {(DS << 4) + 0x1000: 0x01, (DGROUP << 4) + 0x1000: 0x02}
    inputs = {"ax": 0, "bx": 0, "cx": 0, "dx": 0, "si": 0, "di": 0,
              "bp": 0x7777, "sp": SP0, "ds": DS, "es": 0}
    out = _run_body(code, dict(inputs), _seeded(seed))
    assert out.get("ds", DS) & 0xFFFF == DS


def _seeded(seed: dict[int, int]) -> Memory:
    mem = Memory()
    _seed(mem, seed)
    return mem


def test_a_biased_frame_pointer_that_is_not_constant_still_refuses() -> None:
    """The guard on the relaxation the far-entry shapes depend on.  A bp loaded
    from DATA is not frame-derived by any bias — a Win16-shaped prologue around
    a genuine clobber must NOT be laundered into a promotion."""
    code = _asm(
        ("push ds", "1e"), ("pop ax", "58"), ("nop", "90"),
        ("inc bp", "45"), ("push bp", "55"), ("mov bp, sp", "8bec"),
        ("mov bp, [0x1000]", "8b2e0010"),      # <- not a constant bias
        ("mov sp, bp", "8be5"), ("pop bp", "5d"), ("dec bp", "4d"),
        ("retf 4", "ca0400"))
    with pytest.raises(Refusal, match="frame-pointer-clobbered"):
        check_promotable(_scan(code))


# ------------------------------------------ the import-thunk boundary call --

def _thunk_registry_and_slots():
    """A synthetic import-thunk table over the REAL API registry: three slots,
    one ordinary pascal API, one raw API, one unimplemented ordinal.  The
    ordinals are Windows facts, not any program's."""
    reg = build_registry(winflags=WINFLAGS_NO_FPU)
    slots = {
        "USER.1": 0x0000,        # MessageBox  — pascal, arg_sizes [2,4,4,2]
        "KERNEL.91": 0x0004,     # InitTask    — a RAW handler (no arg_sizes)
        "KERNEL.9999": 0x0008,   # not implemented at all
    }
    return reg, slots


def test_plat_farcall_contracts_derives_the_pascal_cleanup() -> None:
    """The fact producer: argbytes is the sum of the API's declared pascal
    argument sizes — the same number win16's own dispatch pops in ret_far."""
    reg, slots = _thunk_registry_and_slots()
    contracts, _skipped = plat_farcall_contracts(THUNK_SEG, slots, reg)

    key = f"{THUNK_SEG:04X}:0000"
    assert set(contracts) == {key}
    entry = reg.entries[("USER", 1)]
    assert contracts[key]["argbytes"] == sum(entry.arg_sizes) == 12
    assert contracts[key]["cost"] == API_DISPATCH_COST
    assert contracts[key]["name"] == "USER.1"


def test_plat_farcall_contracts_refuses_to_guess() -> None:
    """A raw API and a gap get NO contract, each with its own reason — so a far
    call to either refuses `platform-farcall-contract-unknown` downstream
    instead of being handed an invented argument count."""
    reg, slots = _thunk_registry_and_slots()
    _contracts, skipped = plat_farcall_contracts(THUNK_SEG, slots, reg)
    assert {(s.key, s.reason) for s in skipped} == {
        ("KERNEL.91", "raw-api"),
        ("KERNEL.9999", "unimplemented"),
    }
    assert {s.off for s in skipped} == {0x0004, 0x0008}


def test_plat_farcall_contracts_accepts_both_slot_table_spellings() -> None:
    """The serialized manifest form and the in-process ApiRegistry.slots form
    name the same table and must produce the same contracts."""
    reg, slots = _thunk_registry_and_slots()
    tupled = {tuple(k.rsplit(".", 1)[:1]) + (int(k.rsplit(".", 1)[1]),): v
              for k, v in slots.items()}
    assert (plat_farcall_contracts(THUNK_SEG, slots, reg)[0] ==
            plat_farcall_contracts(THUNK_SEG, tupled, reg)[0])


def test_plat_farcalls_document_is_the_shape_the_promoter_reads() -> None:
    """dos_re's `--plat-farcalls @FILE` reader takes {"contracts": {...}} with
    "SEG:OFF" hex keys and skips leading-underscore metadata."""
    import json

    reg, slots = _thunk_registry_and_slots()
    doc, _skipped = plat_farcalls_document(THUNK_SEG, slots, reg)
    assert doc["thunk_seg"] == f"{THUNK_SEG:04X}"
    assert doc["_notice"].startswith("GENERATED")
    round_tripped = json.loads(json.dumps(doc))
    assert round_tripped == doc                # JSON-serializable as produced
    for key, spec in round_tripped["contracts"].items():
        seg, off = key.split(":")
        assert int(seg, 16) == THUNK_SEG and 0 <= int(off, 16) <= 0xFFFF
        assert isinstance(spec["argbytes"], int)


#: push two pascal words, call far into the thunk slot, near-return.
_BOUNDARY_CALL = _asm(
    ("mov ax, 7", "b80700"),
    ("push ax", "50"),
    ("mov ax, 3", "b80300"),
    ("push ax", "50"),
    ("call far THUNK_SEG:0", f"9a0000{THUNK_SEG & 0xFF:02x}"
                             f"{THUNK_SEG >> 8:02x}"),
    ("ret", "c3"),
)

#: A real two-word pascal API sitting at the slot the fixture calls.  The
#: cleanup the emitter is handed is NOT written here — it is DERIVED, by the
#: same producer a consumer runs over a real import table, so this fixture
#: fails if the producer ever starts reporting the wrong number of bytes.
_BOUNDARY_API = ("USER", 63)            # GetScrollPos(HWND, int) — 2 + 2
_BOUNDARY_CONTRACT, _ = plat_farcall_contracts(
    THUNK_SEG, {_BOUNDARY_API: 0x0000},
    build_registry(winflags=WINFLAGS_NO_FPU))
_ARGBYTES = _BOUNDARY_CONTRACT[f"{THUNK_SEG:04X}:0000"]["argbytes"]
assert _ARGBYTES == 4, "the fixture pushes exactly two pascal words"


def _pascal_api_hook():
    """A synthetic pascal Win16 API at the thunk slot: sums two word args into
    AX, writes the result to DS:0002, clobbers BX, and far-returns popping the
    frame plus its own arguments — the callee cleanup the contract describes."""
    def api(cpu):
        s = cpu.s
        ss, sp = s.ss & 0xFFFF, s.sp & 0xFFFF
        total = (cpu.mem.rw(ss, (sp + 4) & 0xFFFF) +
                 cpu.mem.rw(ss, (sp + 6) & 0xFFFF)) & 0xFFFF
        cpu.mem.ww(s.ds & 0xFFFF, 0x0002, total)
        s.bx = 0xBEEF
        ret_off = cpu.mem.rw(ss, sp)
        ret_cs = cpu.mem.rw(ss, (sp + 2) & 0xFFFF)
        s.sp = (sp + 4 + _ARGBYTES) & 0xFFFF
        s.cs, s.ip = ret_cs & 0xFFFF, ret_off & 0xFFFF
        s.ax = total
    return api


_BC_REGS = dict(ax=0, bx=0x1111, cx=0x2222, dx=0x4444, si=0x3333, di=0x5555,
                bp=0x6666, ds=DS, es=0x7777)
_RET_SENTINEL = 0xDEAD


def _bc_machine(hook=None):
    mem = Memory()
    for k, b in enumerate(_BOUNDARY_CALL):
        mem.data[(CS << 4) + k] = b
    mem.ww(SS, SP0, _RET_SENTINEL)
    st = CPUState(cs=CS, ip=0, ss=SS, **_BC_REGS)
    st.sp = SP0
    cpu = CPU8086(mem, st)
    cpu.replacement_hooks[(THUNK_SEG, 0)] = _pascal_api_hook()
    if hook is not None:
        cpu.replacement_hooks[(CS, 0)] = hook
    for _ in range(64):
        cpu.step()
        if (cpu.s.cs & 0xFFFF, cpu.s.ip & 0xFFFF) == (CS, _RET_SENTINEL):
            return cpu, mem
    raise AssertionError("fixture did not return within the step budget")


def _compile_boundary_hook(argbytes: int):
    """Emit the recovered body + CPU-ABI adapter for the boundary call, wired
    with the contract the win16 fact producer supplies."""
    scan = _scan(_BOUNDARY_CALL)
    plat = {(THUNK_SEG, 0): PlatformFarCall(
        argbytes=argbytes, cost=API_DISPATCH_COST,
        name=f"{_BOUNDARY_API[0]}.{_BOUNDARY_API[1]}")}
    spec = check_promotable(scan, plat_far_segs=frozenset({THUNK_SEG}),
                            plat_farcalls=plat)
    base = "t_w16_conformance_pkg"
    rec = emit_recovered(scan, spec.abi, f"{CS:04X}:0000",
                         recovered_import_base=base,
                         needs_plat=spec.needs_plat, df_livein=spec.df_livein,
                         sp_output=spec.sp_output,
                         flags_livein=spec.flags_livein, plat_farcalls=plat)
    ad = emit_adapter(scan, spec.abi, f"{CS:04X}:0000",
                      signature=_BOUNDARY_CALL, recovered_import_base=base,
                      needs_plat=spec.needs_plat, ret_kind=spec.ret_kind,
                      df_livein=spec.df_livein, sp_output=spec.sp_output,
                      ret_pop=spec.ret_pop, flags_livein=spec.flags_livein)
    pkg = types.ModuleType(base)
    pkg.__path__ = []
    sys.modules[base] = pkg
    recmod = types.ModuleType(f"{base}.func_{CS:04x}_0000")
    exec(compile(rec, "<recovered>", "exec"), recmod.__dict__)
    sys.modules[f"{base}.func_{CS:04x}_0000"] = recmod
    admod = types.ModuleType(base + ".adapter")
    exec(compile(ad, "<adapter>", "exec"), admod.__dict__)
    return getattr(admod, f"lifted_{CS:04x}_0000"), rec


def test_boundary_farcall_composes_and_matches_the_interpreter() -> None:
    """A static `call far` into the import-thunk segment lifts to a
    plat.farcall — with the pascal cleanup taken from a contract the win16
    producer derives, not from a guess — and computes byte-for-byte what the
    interpreter computes, including the virtual clock."""
    # _ARGBYTES came out of win16.lift's producer, not out of this file.
    hook, rec_src = _compile_boundary_hook(_ARGBYTES)

    icpu, imem = _bc_machine()
    ccpu, cmem = _bc_machine(hook)

    for r in ("ax", "bx", "cx", "dx", "si", "di", "bp", "sp", "ds", "es"):
        assert getattr(ccpu.s, r) & 0xFFFF == getattr(icpu.s, r) & 0xFFFF, (
            f"{r}: lifted={getattr(ccpu.s, r) & 0xFFFF:04X} "
            f"interp={getattr(icpu.s, r) & 0xFFFF:04X}")
    assert ccpu.s.ax == 0x000A and ccpu.s.bx == 0xBEEF
    assert ccpu.s.sp & 0xFFFF == (SP0 + 2) & 0xFFFF   # args cleaned by callee

    base = DS << 4
    assert bytes(cmem.data[base:base + 8]) == bytes(imem.data[base:base + 8])
    assert cmem.rw(DS, 0x0002) == 0x000A

    # the virtual clock agrees: `call far` + one API dispatch, and the dispatch
    # cost is the one win16.lift declares.
    assert ccpu.instruction_count == icpu.instruction_count
    assert f"plat.farcall(0x{THUNK_SEG:04x}, 0x0000" in rec_src


def test_boundary_farcall_without_a_contract_refuses_loud() -> None:
    """The frontier this repo must keep honest: a thunk slot whose API has no
    derivable argbytes (a raw API, or a gap) yields no contract, and the call
    to it REFUSES rather than inventing a cleanup."""
    scan = _scan(_BOUNDARY_CALL)
    with pytest.raises(Refusal, match="platform-farcall-contract-unknown"):
        check_promotable(scan, plat_far_segs=frozenset({THUNK_SEG}),
                         plat_farcalls={})


def test_the_thunk_segment_is_the_one_win16_actually_loads_into() -> None:
    """win16.lift re-exports the boundary segment so a consumer never has to
    restate it, and it is the segment the machine really installs thunks at."""
    from win16.machine import THUNK_SEG as MACHINE_THUNK_SEG
    assert THUNK_SEG == MACHINE_THUNK_SEG
