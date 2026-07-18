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
* **far-pointer PROVENANCE** (:mod:`win16.farptr`) — the dynamic half of the
  boundary.  ``GetProcAddress`` / ``MakeProcInstance`` hand the program a far
  pointer it stores and later calls through, and a promoter that sees only
  ``call far [bp-4]`` refuses.  win16 minted the pointer, so win16 can say what
  it denotes; the fixtures pin the denotations, the evidence-file shape the
  consumer reads, and the refusals that must SURVIVE (a NULL, an unbound thunk,
  a guest address — none of which may acquire an invented contract).
* **the raw-API values forms** — a raw ``-register`` API declares no argument
  list, so no far-call contract is derivable, so every far call to it refuses.
  ``USER.420 wsprintf`` (cdecl varargs), ``KERNEL.102 DOS3Call`` (INT 21h
  passthrough) and ``KERNEL.91 InitTask`` (a multi-register result) each get an
  args-in/result-out form, and each is differentialled: the REAL handler run
  down both the interpreter's stack-and-``ret_far`` shim and the CPU-free path,
  over identical memory.  ``WIN87EM.1 __fpMath`` deliberately keeps refusing.

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
    ordinals are Windows facts, not any program's.

    The raw example is ``WIN87EM.1 __fpMath``, which is the one API in the
    surface that is DELIBERATELY still raw: it is the x87 emulator entry, and
    the FP frontier is unbuilt by design (``win16/fpu.py`` is grown against a
    real frontier, not speculatively).  It was ``KERNEL.91 InitTask`` until
    InitTask got the ``ret="regs"`` values form and therefore a contract — the
    assertion is unchanged, it just has to point at an API that is still raw to
    keep testing what it says it tests.
    """
    reg = build_registry(winflags=WINFLAGS_NO_FPU)
    slots = {
        "USER.1": 0x0000,        # MessageBox  — pascal, arg_sizes [2,4,4,2]
        "WIN87EM.1": 0x0004,     # __fpMath    — a RAW handler (no arg_sizes)
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
        ("WIN87EM.1", "raw-api"),
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


# ============================================================================
# FAR-POINTER PROVENANCE — what a dynamically-obtained FARPROC denotes
# ============================================================================
#
# The static `call far THUNK_SEG:slot` above is the easy half of the Win16
# boundary.  The hard half is dynamic linking: GetProcAddress / MakeProcInstance
# hand the program a far pointer, it stores it, and later it calls THROUGH the
# variable.  A promoter sees `call far [bp-4]`, cannot name the target, and
# refuses.  win16 minted that pointer and knows what it is; these fixtures pin
# that it can say so, and say it in the shape the consumer reads.

from win16.api.core import ApiRegistry                             # noqa: E402
from win16.farptr import (API_THUNK, GUEST, NULL, PROC_THUNK,      # noqa: E402
                          UNBOUND_THUNK, FarPointerLog,
                          indirect_farcall_document)
from win16.lift import entry_argbytes                              # noqa: E402


class _FakeCpu:
    """The minimum a registry needs to mint thunks and run a handler: memory,
    registers, the hook tables.  It executes nothing — the CPU-free carrier's
    shape, written locally so these fixtures depend on no boot image."""

    def __init__(self, mem=None, **regs):
        self.mem = mem if mem is not None else Memory()
        self.s = CPUState(**regs)
        self.replacement_hooks: dict = {}
        self.hook_names: dict = {}
        self.instruction_count = 0
        self.win16_current_api = ("?", 0)


def _installed_registry(*imports):
    """A real API registry with `imports` resolved into thunk slots and
    installed on a CPU-free carrier — the state a running Win16 program has."""
    reg = build_registry(winflags=WINFLAGS_NO_FPU)
    for module, ordinal in imports:
        reg.resolve_import(module, ordinal)
    cpu = _FakeCpu()
    reg.install(cpu, THUNK_SEG)
    return reg, cpu


def _fp(seg: int, off: int) -> int:
    return ((seg & 0xFFFF) << 16) | (off & 0xFFFF)


# ------------------------------------------------------- describe() ---------

def test_provenance_resolves_a_static_import_thunk_to_its_api_contract() -> None:
    """The decisive case.  A stored FARPROC that points at an import slot is
    the SAME call a static `call far` would have made, so it must resolve to
    the API and carry the identical pascal cleanup the static producer derives
    — one Win16 fact, reachable two ways, and it may never disagree with
    itself."""
    reg, _cpu = _installed_registry(("USER", 1), ("USER", 63))
    log = FarPointerLog(reg)

    slot = reg.slots[("USER", 63)]
    fp = log.describe(_fp(THUNK_SEG, slot))
    assert fp.kind == API_THUNK
    assert fp.name == "USER.63"
    assert fp.callable_by_platform
    # not restated here: taken from the same producer the static path uses
    static, _skipped = plat_farcall_contracts(THUNK_SEG, reg.slots, reg)
    assert fp.argbytes == static[fp.key]["argbytes"] == 4


def test_provenance_needs_no_instrumentation_for_a_thunk_target() -> None:
    """describe() reads the registry's OWN tables, so a thunk target resolves
    off a registry that was never armed.  That is why provenance costs the
    production path nothing: the decisive lookup is after-the-fact."""
    reg, _cpu = _installed_registry(("USER", 1))
    unarmed = FarPointerLog(reg)                       # no arm() anywhere
    assert not unarmed.mints
    assert unarmed.describe(
        _fp(THUNK_SEG, reg.slots[("USER", 1)])).kind == API_THUNK


def test_provenance_resolves_a_runtime_minted_proc_thunk() -> None:
    """A GetProcAddress-minted by-name proc is not in the import table, so no
    static analysis could find it — but it dispatches identically and its
    contract is just as derivable."""
    reg, _cpu = _installed_registry(("USER", 1))
    log = FarPointerLog(reg).arm()
    module, name = next(iter(reg.named_procs))       # whatever the surface offers

    value = reg.mint_proc_thunk(module, name)
    assert value, "the surface must offer at least one by-name proc"
    fp = log.describe(value)
    assert fp.kind == PROC_THUNK
    assert fp.name == f"{module}.{name}"
    assert fp.origin == "GetProcAddress"
    assert fp.argbytes == entry_argbytes(reg.named_procs[(module, name)])
    assert log.mints[-1].value == value


def test_provenance_records_makeprocinstance_without_changing_it() -> None:
    """MakeProcInstance hands back the program's OWN far pointer unchanged (one
    instance, fixed DGROUP).  Recording must be transparent: the guest sees the
    identical value, and the pointer resolves as GUEST code with NO invented
    contract — promoting a call to it means promoting that function."""
    reg, cpu = _installed_registry(("KERNEL", 51))
    entry = reg.entries[("KERNEL", 51)]
    proc = _fp(0x1234, 0x5678)

    with FarPointerLog(reg) as log:
        ax, dx = reg.invoke_values(cpu, entry, "KERNEL.51", (proc, 0x0100))
    assert (dx << 16 | ax) == proc, "MakeProcInstance must pass the proc through"

    fp = log.describe(proc)
    assert fp.kind == GUEST
    assert fp.origin == "MakeProcInstance"
    assert fp.argbytes is None and fp.name is None
    assert not fp.callable_by_platform


def test_provenance_refuses_to_guess_a_null_or_unbound_pointer() -> None:
    """The two honest frontier answers.  NULL is what GetProcAddress returns
    for a proc we do not implement (a program that stores and calls it is
    calling address zero); an unbound thunk offset is a pointer into the
    boundary segment that this registry did not mint.  Neither may acquire a
    contract."""
    reg, _cpu = _installed_registry(("USER", 1))
    log = FarPointerLog(reg)

    assert log.describe(0).kind == NULL
    assert not log.describe(0).callable_by_platform
    stray = log.describe(_fp(THUNK_SEG, 0x7FFC))
    assert stray.kind == UNBOUND_THUNK
    assert stray.argbytes is None
    assert not stray.callable_by_platform


def test_arming_is_reversible_and_leaves_the_registry_identical() -> None:
    """Provenance must not perturb the production path.  After disarm the
    registry holds the very same callables it held before — not equivalent
    ones, the same objects."""
    reg, _cpu = _installed_registry(("KERNEL", 51))
    before_handler = reg.entries[("KERNEL", 51)].handler
    assert "mint_proc_thunk" not in reg.__dict__     # the class method, unshadowed

    log = FarPointerLog(reg).arm()
    assert "mint_proc_thunk" in reg.__dict__                    # armed
    assert reg.entries[("KERNEL", 51)].handler is not before_handler
    log.disarm()

    # not "an equivalent callable is installed" — the shadow is GONE and the
    # handler is the original object, so the registry is what it was.
    assert "mint_proc_thunk" not in reg.__dict__
    assert reg.entries[("KERNEL", 51)].handler is before_handler


# --------------------------------------------------- the evidence file ------

_PROBE_SITE = "18C0:0BA0"          # a synthetic guest CS:IP, not any program's


def _document_over(reg, targets):
    log = FarPointerLog(reg)
    return log, indirect_farcall_document(
        log, [{"site": _PROBE_SITE, "targets": targets}], demo="synthetic")


def test_indirect_document_carries_dos_res_dyn_evidence_shape() -> None:
    """`dyn_evidence` is dos_re's existing closure-walk input — "CS:IP" site ->
    list of target keys — and must arrive in exactly that shape, because the
    consumer side is built against it."""
    import json

    reg, _cpu = _installed_registry(("USER", 1), ("USER", 63))
    key = f"{THUNK_SEG:04X}:{reg.slots[('USER', 63)]:04X}"
    _log, doc = _document_over(reg, {key: 7})

    assert doc["dyn_evidence"] == {_PROBE_SITE: [key]}
    assert doc["demo"] == "synthetic"
    assert doc["thunk_seg"] == f"{THUNK_SEG:04X}"
    assert doc["_notice"].startswith("GENERATED")
    assert json.loads(json.dumps(doc)) == doc      # serializable as produced


def test_indirect_document_contracts_match_the_static_producer_exactly() -> None:
    """The contracts an indirect site gets are the SAME contracts the static
    import table gets — same keys, same argbytes, same cost, same names.  An
    indirect call to a thunk is not a weaker fact than a direct one."""
    reg, _cpu = _installed_registry(("USER", 1), ("USER", 63))
    keys = {f"{THUNK_SEG:04X}:{reg.slots[api]:04X}"
            for api in (("USER", 1), ("USER", 63))}
    _log, doc = _document_over(reg, {k: 1 for k in keys})

    static, _skipped = plat_farcall_contracts(THUNK_SEG, reg.slots, reg)
    assert doc["contracts"] == {k: static[k] for k in keys}
    assert not doc["unresolved"], "both targets are contracted"
    for row in doc["sites"][_PROBE_SITE]["targets"]:
        assert row["kind"] == API_THUNK and row["count"] == 1


def test_indirect_document_reports_what_it_cannot_close() -> None:
    """A site whose target has no derivable contract stays on the frontier and
    is REPORTED with its reason.  Shrinking the frontier by silence is the one
    thing this channel must never do."""
    reg, _cpu = _installed_registry(("USER", 1), ("WIN87EM", 1))
    guest = "3A70:0142"
    raw = f"{THUNK_SEG:04X}:{reg.slots[('WIN87EM', 1)]:04X}"
    _log, doc = _document_over(reg, {guest: 3, "0000:0000": 1, raw: 2})

    assert doc["contracts"] == {}
    # the reason says what is MISSING, not what the target is: a raw API is
    # reported with lift.SkippedSlot's own word for the same frontier, even
    # though provenance knows perfectly well which API it is.
    assert doc["unresolved"][_PROBE_SITE] == {
        guest: GUEST, "0000:0000": NULL, raw: "raw-api"}
    rows = {r["target"]: r for r in doc["sites"][_PROBE_SITE]["targets"]}
    assert rows[raw]["name"] == "WIN87EM.1" and "argbytes" not in rows[raw]
    assert doc["dyn_evidence"][_PROBE_SITE] == ["0000:0000", raw, guest]


def test_indirect_document_accepts_both_probe_capture_spellings() -> None:
    """The serialized list-of-rows form and the in-process mapping form are the
    same capture written two ways; a caller must not have to reshape one."""
    reg, _cpu = _installed_registry(("USER", 63))
    key = f"{THUNK_SEG:04X}:{reg.slots[('USER', 63)]:04X}"
    log = FarPointerLog(reg)

    as_rows = indirect_farcall_document(log, [{"site": _PROBE_SITE,
                                               "targets": {key: 2}}])
    as_map = indirect_farcall_document(log, {_PROBE_SITE: {key: 2}})
    as_bare = indirect_farcall_document(log, {_PROBE_SITE: [key]})
    assert as_rows == as_map
    assert as_bare["contracts"] == as_rows["contracts"]      # counts aside


def test_the_indirect_far_call_site_is_still_refused_without_the_consumer(
) -> None:
    """The other half of the split, stated honestly.  Producing the evidence
    does not by itself compose the call: dos_re still refuses an indirect far
    call, and it SHOULD until its consumer side lands.  This fixture is the
    join point — when composition arrives, an indirect site holding a contract
    from the document above stops refusing, and this is where that is proven.
    """
    code = _asm(("call far [bp-4]", "ff5efc"), ("ret", "c3"))
    with pytest.raises(Refusal, match="indirect-control-flow"):
        check_promotable(_scan(code), plat_far_segs=frozenset({THUNK_SEG}))


# ============================================================================
# THE RAW-API VALUES FORMS — closing platform-farcall-contract-unknown
# ============================================================================
#
# A raw (-register) API has no declared argument list, so no far-call contract
# is derivable, so every far call to it refuses.  That is one seam, not four
# problems: give the API an args-in/result-out form and the contract follows.
# Each fixture below runs the REAL registry handler down BOTH paths — the
# interpreter's stack-and-ret_far shim and the CPU-free values form — over
# identical memory, and diffs the results.  A values form asserted only against
# itself would prove nothing.

def _thunk_call_bytes(slot: int, *pushes: int) -> bytes:
    """A guest body that pushes `pushes` (in order) and far-calls a thunk slot.
    Written with mov/push pairs so it stays 8086, and with no cleanup — who
    pops is exactly what each convention fixture is measuring."""
    body = []
    for word in pushes:
        body.append(("mov ax, imm",
                     f"b8{word & 0xFF:02x}{(word >> 8) & 0xFF:02x}"))
        body.append(("push ax", "50"))
    body.append(("call far THUNK_SEG:slot",
                 f"9a{slot & 0xFF:02x}{(slot >> 8) & 0xFF:02x}"
                 f"{THUNK_SEG & 0xFF:02x}{THUNK_SEG >> 8:02x}"))
    return _asm(*body)


def _run_thunk_call(reg, code: bytes, mem: Memory, **regs) -> CPU8086:
    """Run a guest body through CPU8086 with the real API dispatch installed,
    stopping once the far call has returned past the end of the body."""
    st = CPUState(cs=CS, ip=0, ss=SS, **regs)
    st.sp = SP0
    cpu = CPU8086(mem, st)
    for k, b in enumerate(code):
        mem.data[(CS << 4) + k] = b
    reg.install(cpu, THUNK_SEG)
    stop = len(code)
    for _ in range(64):
        if cpu.s.cs == CS and cpu.s.ip >= stop:
            return cpu
        cpu.step()
    raise AssertionError("the thunk call did not return within the step budget")


# --------------------------------------------------------- USER.420 --------

_FMT_OFF = 0x2000
_OUT_OFF = 0x2100
_ARG_OFF = 0x2200


def _wsprintf_memory() -> Memory:
    mem = Memory()
    mem.load(DS, _FMT_OFF, b"n=%d s=%s\x00")
    mem.load(DS, _ARG_OFF, b"ok\x00")
    return mem


#: cdecl pushes RIGHT-to-LEFT, and each far pointer goes high word first so the
#: dword reads back little-endian: wsprintf(out, fmt, 42, "ok")
_WSPRINTF_PUSHES = (DS, _ARG_OFF, 42, DS, _FMT_OFF, DS, _OUT_OFF)
_WSPRINTF_EXPECT = b"n=42 s=ok"


def test_wsprintf_is_cdecl_and_the_callee_cleanup_is_genuinely_zero() -> None:
    """wsprintf is variadic C: the CALLER pops.  So argbytes 0 is the truth,
    not a placeholder — and it is now DECLARED, which is what turns
    `platform-farcall-contract-unknown` into a composable call.
    `caller_cleanup` records which kind of zero it is, since the number alone
    cannot say."""
    reg = build_registry(winflags=WINFLAGS_NO_FPU)
    entry = reg.entries[("USER", 420)]
    assert entry.raw is False and entry.varargs is True
    assert entry.caller_cleanup is True
    assert entry_argbytes(entry) == 0

    contracts, skipped = plat_farcall_contracts(THUNK_SEG, {"USER.420": 0}, reg)
    assert not skipped
    assert contracts[f"{THUNK_SEG:04X}:0000"]["argbytes"] == 0


def test_wsprintf_values_form_matches_the_interpreter() -> None:
    """THE differential.  The same real handler, over the same stack image, run
    through the interpreter's shim and through the CPU-free values form: the
    formatted output and the returned length must agree, and the arguments must
    be left for the CALLER to pop in both."""
    reg, _ = _installed_registry(("USER", 420))
    slot = reg.slots[("USER", 420)]
    code = _thunk_call_bytes(slot, *_WSPRINTF_PUSHES)

    # -- the interpreter path
    m_cpu = _wsprintf_memory()
    cpu = _run_thunk_call(reg, code, m_cpu, ds=DS)
    assert cpu.s.ax == len(_WSPRINTF_EXPECT)
    # cdecl: the callee popped nothing, so all pushed words are still there
    assert cpu.s.sp & 0xFFFF == (SP0 - 2 * len(_WSPRINTF_PUSHES)) & 0xFFFF

    # -- the CPU-free values path, over the identical frame the body built.
    # sp points at the far return address, which is exactly where plat.farcall
    # leaves it for a recovered body that pushed the same arguments.
    m_val = Memory()
    m_val.data[:] = m_cpu.data[:]
    for off in range(len(_WSPRINTF_EXPECT) + 1):        # clear the output only
        m_val.data[(DS << 4) + _OUT_OFF + off] = 0
    carrier = _FakeCpu(m_val, ds=DS, ss=SS)
    carrier.s.sp = (cpu.s.sp - 4) & 0xFFFF
    ax, dx = reg.invoke_values(carrier, reg.entries[("USER", 420)],
                               "USER.420", ())

    assert (ax, dx) == (cpu.s.ax & 0xFFFF, None)
    out = bytes(m_val.data[(DS << 4) + _OUT_OFF:
                           (DS << 4) + _OUT_OFF + len(_WSPRINTF_EXPECT)])
    assert out == _WSPRINTF_EXPECT
    assert bytes(m_val.data) == bytes(m_cpu.data), \
        "the two paths wrote different memory"


# --------------------------------------------------------- KERNEL.102 -------

def test_dos3call_values_form_matches_the_interpreter() -> None:
    """DOS3Call is INT 21h by far call: registers in, registers out, no pascal
    arguments.  Its values form is a REGISTER DELTA, which is exactly
    `plat.intr`'s shape — so the CPU-free host can service it."""
    reg, _ = _installed_registry(("KERNEL", 102))
    slot = reg.slots[("KERNEL", 102)]
    entry = reg.entries[("KERNEL", 102)]

    inputs = dict(ax=0x3000, bx=0xFFFF, cx=0xFFFF)      # AH=30h: DOS GetVersion
    cpu = _run_thunk_call(reg, _thunk_call_bytes(slot), Memory(), **inputs)
    assert cpu.s.sp & 0xFFFF == SP0                     # no args, callee pops 0

    carrier = _FakeCpu(Memory(), ss=SS, **inputs)
    carrier.s.sp = SP0
    delta = reg.invoke_regs(carrier, entry, "KERNEL.102", ())

    assert delta == {"ax": 0x0005, "bx": 0x0000, "cx": 0x0000}
    for name, val in delta.items():
        assert getattr(cpu.s, name) & 0xFFFF == val, name
    assert entry_argbytes(entry) == 0                   # a contract now exists
    assert not plat_farcall_contracts(THUNK_SEG, {"KERNEL.102": 0}, reg)[1]


def test_a_regs_api_reports_the_carry_flag_as_part_of_its_result() -> None:
    """INT 21h signals failure by setting CARRY, so for a `regs` API the flags
    register is a RESULT register.  A delta that dropped it would report a
    failed DOS call as a successful one on the CPU-free path — where there is
    no shared CPU for the handler's write to land on implicitly."""
    reg = ApiRegistry()

    @reg.register("KERNEL", 9998, name="#9998", ret="regs")
    def _fails(ctx) -> None:
        ctx.cpu.s.ax = 0x0002                       # ERROR_FILE_NOT_FOUND
        ctx.cpu.s.flags |= 0x0001                   # CF: the call failed

    carrier = _FakeCpu(Memory(), ax=0, flags=0)
    delta = reg.invoke_regs(carrier, reg.entries[("KERNEL", 9998)],
                            "KERNEL.9998", ())
    assert delta == {"ax": 0x0002, "flags": 0x0001}


# --------------------------------------------------------- KERNEL.91 -------

class _FakeExeHeader:
    stack_size = 0x2000


class _FakeExe:
    header = _FakeExeHeader()


class _FakeMachine:
    exe = _FakeExe()


class _FakeSystem:
    """The synthetic slice of Win16System that InitTask reads.  Hand-written:
    no boot image, no EXE, no game."""
    h_instance = 0x0800
    h_prev_instance = 0
    cmd_show = 10
    booted = False
    machine = _FakeMachine()

    def stack_bounds(self):
        return 0x1000, 0x0200

    def ensure_psp(self):
        return 0x0700


def test_inittask_values_form_returns_the_whole_register_bundle() -> None:
    """InitTask's result does not fit (AX, DX): it is AX/BX/CX/DX/SI/DI/ES at
    once.  That is why it was raw, and why it cost every far call to it a
    contract.  `ret="regs"` is the widening — and the interpreter path must
    still land on exactly the same registers."""
    reg, _ = _installed_registry(("KERNEL", 91))
    reg.services["system"] = _FakeSystem()
    slot = reg.slots[("KERNEL", 91)]
    entry = reg.entries[("KERNEL", 91)]

    cpu = _run_thunk_call(reg, _thunk_call_bytes(slot), Memory(), ds=0x0800)
    assert cpu.s.sp & 0xFFFF == SP0                 # callee popped 0

    reg.services["system"] = _FakeSystem()          # a fresh, unbooted system
    carrier = _FakeCpu(Memory(), ss=SS, ds=0x0800)
    carrier.s.sp = SP0
    delta = reg.invoke_regs(carrier, entry, "KERNEL.91", ())

    assert delta == {"ax": 1, "bx": 0x81, "cx": 0x2000, "dx": 10,
                     "di": 0x0800, "es": 0x0700}
    for name, val in delta.items():
        assert getattr(cpu.s, name) & 0xFFFF == val, name
    assert entry_argbytes(entry) == 0


def test_the_regs_and_values_primitives_refuse_each_others_apis() -> None:
    """The seam stated as a rule.  invoke_values' (ax, dx) contract is
    UNCHANGED for every API that had one — a `regs` API cannot sneak through it
    and get its wider result silently truncated, and a value-returning API
    cannot be asked for a delta."""
    reg = build_registry(winflags=WINFLAGS_NO_FPU)
    carrier = _FakeCpu(Memory())

    with pytest.raises(ValueError, match="invoke_regs instead"):
        reg.invoke_values(carrier, reg.entries[("KERNEL", 91)], "KERNEL.91", ())
    with pytest.raises(ValueError, match="call invoke_values"):
        reg.invoke_regs(carrier, reg.entries[("USER", 1)], "USER.1", ())


def test_invoke_values_still_returns_ax_dx_for_an_ordinary_api() -> None:
    """The regression fence on the primitive the CPU path's `_invoke` is a thin
    shim over.  A plain word API and a plain long API must return exactly the
    2-tuple they always did — this is what keeps the byte-exact production gate
    untouched by everything above."""
    reg = build_registry(winflags=WINFLAGS_NO_FPU)
    carrier = _FakeCpu(Memory())

    word = reg.invoke_values(carrier, reg.entries[("KERNEL", 52)],
                             "KERNEL.52", (0x12345678,))       # FreeProcInstance
    assert word == (1, None)

    long_ = reg.invoke_values(carrier, reg.entries[("KERNEL", 3)],
                              "KERNEL.3", ())                  # GetVersion
    assert isinstance(long_, tuple) and len(long_) == 2
    assert all(isinstance(v, int) for v in long_)


def test_win87em_is_the_one_api_deliberately_left_raw() -> None:
    """__fpMath is the x87 emulator entry.  It is NOT given a values form here:
    win16/fpu.py is unbuilt by design (grown against a real FP frontier, not
    speculatively), so inventing a contract for its dispatch-on-BX protocol
    would be guessing.  It keeps refusing, and that refusal is the honest
    frontier item — pinned here so it stays a decision rather than an
    oversight."""
    reg = build_registry(winflags=WINFLAGS_NO_FPU)
    entry = reg.entries[("WIN87EM", 1)]
    assert entry.raw is True
    assert entry_argbytes(entry) is None

    _contracts, skipped = plat_farcall_contracts(THUNK_SEG, {"WIN87EM.1": 0},
                                                 reg)
    assert [(s.key, s.reason) for s in skipped] == [("WIN87EM.1", "raw-api")]
