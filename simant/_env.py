"""Locate the sibling dos_re framework checkout and put it on sys.path.

Override with the DOS_RE_PATH environment variable.  Nothing is vendored:
this repo uses the framework in place, per dos_re/START_HERE.md's
separate-repo workflow.  (Same shim as ppython/_env.py — each game package
is self-contained so it can be run without importing the other.)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_DEFAULT = Path(r"D:\Games\DOS\dos_re")


def ensure_dos_re() -> Path:
    root = Path(os.environ.get("DOS_RE_PATH", _DEFAULT))
    if not (root / "dos_re" / "cpu.py").exists():
        raise ImportError(
            f"dos_re framework not found at {root} — set DOS_RE_PATH to its checkout")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


ensure_dos_re()
