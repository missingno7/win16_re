"""Repository guards for the win16_re dos_re 3.0 architecture.

The Win16 analogue of dos_re's own architecture-contract test.  These pin the
migration's structural invariants so a later change cannot silently
reintroduce a retired path or a layer cycle.

Scope note: win16_re carries no game, so the "single player" and
legacy-CLI-token guards live in the consuming game project (simant_port).
What this file guards is the FRAMEWORK: the retired tick-demo machinery
stays gone, the replay/continuation/driver layers stay acyclic, and the
CPU-free wall (machine.py must not import the CPU carrier) holds.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WIN16 = ROOT / "win16"

#: Retired framework modules that must not come back (tick demos were deleted
#: in the dos_re 3.0 migration, mirroring dos_re's own tick_demo removal).
REMOVED_PATHS = (
    "win16/tick_demo.py",
)

#: Tokens no ACTIVE win16 source may reference (docs/history is exempt — see
#: the token scan).  The retired tick-demo vocabulary.
REMOVED_TOKENS = (
    "win16.tick_demo",
    "TickDemoRecorder",
    "TickDemoDriver",
    "tick_recorder",
    "tick_driver",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            # Resolve win16-relative imports to absolute dotted names.
            if node.level and node.module:
                out.add(f"win16.{node.module}")
            elif node.level:
                out.add("win16")
            else:
                out.add(node.module or "")
    return out


def test_removed_framework_paths_do_not_exist() -> None:
    present = [p for p in REMOVED_PATHS if (ROOT / p).exists()]
    assert not present, f"retired win16 paths reappeared: {present}"


def test_no_retired_tokens_in_active_source() -> None:
    offenders: list[str] = []
    for py in WIN16.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for token in REMOVED_TOKENS:
            if token in text:
                offenders.append(f"{py.relative_to(ROOT)}: {token}")
    assert not offenders, "retired tick-demo tokens in active source:\n" + \
        "\n".join(offenders)


def test_replay_format_layer_is_below_drivers_and_evidence() -> None:
    """win16/replay.py is the format adapter — it must not import the higher
    driver/evidence/continuation layers that build ON it (a cycle would make
    the format depend on its consumers)."""
    imports = _imports(WIN16 / "replay.py")
    forbidden = {"win16.replay_driver", "win16.evidence", "win16.continuation"}
    assert not (imports & forbidden), \
        f"win16/replay.py imports a higher layer: {imports & forbidden}"


def test_continuation_codec_does_not_import_the_driver() -> None:
    """win16/continuation.py is a state codec; the ReplayDriver imports IT,
    never the reverse."""
    imports = _imports(WIN16 / "continuation.py")
    assert "win16.replay_driver" not in imports


def test_cpu_free_machine_record_never_imports_the_cpu_carrier() -> None:
    """The CPU-free host holds a machine record importable behind the CPUless
    wall: win16/machine.py must not import the interpreter/CPU carrier
    (dos_re.cpu builds a CPU8086 at module load)."""
    for module in ("machine.py", "cpuless.py"):
        imports = _imports(WIN16 / module)
        assert "dos_re.cpu" not in imports, \
            f"win16/{module} imports dos_re.cpu (breaks the CPU-free wall)"


def test_replay_driver_declares_the_projection_contract() -> None:
    """The 3.0 verification seam: the ReplayDriver must expose a projection
    contract so verify_interval can compare across compositions."""
    from win16.replay_driver import (PROJECTION_CONTRACT,
                                     Win16ReplayDriver)
    assert PROJECTION_CONTRACT.schema_id == "win16-re-observable-v1"
    assert hasattr(Win16ReplayDriver, "verification_projection_contract")
    assert hasattr(Win16ReplayDriver, "project")
