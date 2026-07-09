"""Locate the vendored dos_re CPU/memory VM (a git submodule of this repo) and
put it on sys.path.  Imported by win16/__init__.py, so anything that imports
`win16` transparently gets `dos_re` importable too — a consuming game-port
project only needs to know about win16_re, never about dos_re directly.

dos_re is pinned in-repo at `dos_re/` (a real git submodule of
https://github.com/missingno7/dos_re.git) — `git clone --recurse-submodules`
(or `git submodule update --init`) is all a fresh checkout needs.

For active co-development of dos_re itself, set DOS_RE_PATH to point at a
separate working checkout instead (e.g. one with uncommitted framework changes
being tested against this repo before they land upstream) — this is a
deliberate opt-in escape hatch, not the default.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_SUBMODULE = Path(__file__).resolve().parent.parent / "dos_re"


def ensure_dos_re() -> Path:
    root = Path(os.environ["DOS_RE_PATH"]) if "DOS_RE_PATH" in os.environ else _SUBMODULE
    if not (root / "dos_re" / "cpu.py").exists():
        hint = ("DOS_RE_PATH points at a bad checkout" if "DOS_RE_PATH" in os.environ
                else "run `git submodule update --init` in this repo")
        raise ImportError(f"dos_re framework not found at {root} — {hint}")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


ensure_dos_re()
