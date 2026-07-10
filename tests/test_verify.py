"""win16.verify: the Win16 runtime shim dos_re's HookVerifier consumes.

Game-free: `clone_machine` needs a loaded NE, so its end-to-end proof lives in a
game-port project (simant_port's scripts/liftverify.py, run against the real
binary + a recorded demo).  What is asserted here is the contract that lets the
DOS verifier drive a Win16 machine at all — the duck-typed runtime shape, and
that our cloner (not dos_re's DOS cloner) is the one wired in.
"""
from __future__ import annotations

import types

from dos_re.verification import HookVerifierConfig
from win16.verify import Win16Runtime, install_lift_verifier


def _fake_machine():
    """The three attributes the shim + verifier installation touch."""
    cpu = types.SimpleNamespace(
        replacement_hooks={}, hook_names={}, hook_verifier=None,
        hook_verifier_verify_nested_calls=True,
    )
    mem = types.SimpleNamespace(data=bytearray(16))
    return types.SimpleNamespace(cpu=cpu, mem=mem, api=types.SimpleNamespace(services={}))


def test_runtime_shim_exposes_what_the_verifier_reads():
    m = _fake_machine()
    rt = Win16Runtime.of(m)
    # dos_re's HookVerifier only ever touches these two on a runtime.
    assert rt.cpu is m.cpu
    assert rt.program.memory is m.mem
    assert rt.machine is m          # ours: the cloner reaches the OS graph through it


def test_install_lift_verifier_wires_our_cloner_not_the_dos_one():
    m = _fake_machine()
    key = (0x1234, 0x5678)
    verifier = install_lift_verifier(m, _fake_machine, hooks={key})

    # installed on the machine's own CPU (bound methods compare by __self__)
    assert m.cpu.hook_verifier.__self__ is verifier
    assert verifier.config.hooks == {key}
    assert verifier.config.clone_runtime is not None, "must override the DOS cloner"
    assert verifier.config.auto_continuation, "strict mode: no hand-written stop metadata"
    assert verifier.config.full_memory


def test_cloner_receives_the_live_runtime_and_returns_a_shim(monkeypatch):
    import win16.verify as V
    m = _fake_machine()
    clone = _fake_machine()
    seen = []
    monkeypatch.setattr(V, "clone_machine", lambda src, factory: seen.append(src) or clone)

    verifier = install_lift_verifier(m, _fake_machine, hooks={(0, 0)})
    out = verifier.config.clone_runtime(Win16Runtime.of(m))

    assert seen == [m], "the cloner must be handed the LIVE machine"
    assert isinstance(out, Win16Runtime) and out.machine is clone
    assert out.program.memory is clone.mem


def test_strict_config_carries_the_cloner_through():
    """Regression: HookVerifierConfig.strict() must not drop clone_runtime."""
    sentinel = object()
    cfg = HookVerifierConfig.strict(hooks=set(), clone_runtime=sentinel)
    assert cfg.clone_runtime is sentinel


def test_api_thunks_are_registered_passthrough_not_verified():
    """The Windows API is hooks over INT3 tripwires: they are the environment,
    not a replacement for game ASM. They must be passthrough (never verified,
    never cleared from the ASM oracle) and the ASM side must keep them."""
    from win16.loader import THUNK_SEG
    m = _fake_machine()
    thunk = (THUNK_SEG, 0x00F0)
    game = (0x430E, 0xC256)
    m.cpu.replacement_hooks[thunk] = lambda cpu: None
    m.cpu.replacement_hooks[game] = lambda cpu: None

    verifier = install_lift_verifier(m, _fake_machine, hooks={game})

    assert thunk in m.cpu.hook_verifier_passthrough
    assert game not in m.cpu.hook_verifier_passthrough, "the routine under test IS verified"
    assert verifier.config.asm_keeps_passthrough_hooks,         "the ASM oracle must keep the OS hooks or it executes the tripwire"
