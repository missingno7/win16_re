"""Microman snapshot roundtrip under the selector heap — bit-exact resume.

The selector-model snapshot must restore the huge heap's selector->linear
map into the VM Memory (the pickle copies the dict, so load_snapshot has to
re-wire it) — this gate proves a resumed machine continues bit-exact, both
pure-ASM and with the lifted islands installed.
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import ppython._env  # noqa: E402,F401

from gamehooks import install_game_hooks  # noqa: E402
from scripts.games import game_exe, game_winflags  # noqa: E402
from win16.app import create_machine  # noqa: E402
from win16.vmsnap import digest, load_snapshot, save_snapshot  # noqa: E402

MICROMAN = game_exe("microman")

pytestmark = pytest.mark.skipif(not MICROMAN.exists(),
                                reason="microman assets not present")


def _factory():
    m = create_machine(MICROMAN, winflags=game_winflags("microman"))
    m.cpu.trace_enabled = False
    return m


def test_snapshot_resume_is_bit_exact_plain_and_hooked(tmp_path):
    m = _factory()
    m.cpu.run(3_000)                        # boot -> running title
    save_snapshot(m, tmp_path / "snap", note="microman selector-heap gate")
    m.cpu.run(500)
    expected = digest(m)

    # Pure-ASM resume.
    m2 = load_snapshot(tmp_path / "snap", _factory)
    m2.cpu.run(500)
    assert digest(m2) == expected, "plain resume diverged"

    # Resume WITH the lifted islands: same state, fewer instructions.
    m3 = load_snapshot(tmp_path / "snap", _factory)
    assert install_game_hooks("microman", m3) == 17
    m3.cpu.run(500)
    assert digest(m3) == expected, "hooked resume diverged"
