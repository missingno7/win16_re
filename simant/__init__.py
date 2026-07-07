"""SimAnt for Windows (SIMANTW.EXE) — game package.

Maxis's SimAnt is the framework's stress target: a full commercial Win16
application (6 code segments, KEYBOARD + WIN87EM imports, raw INT 21h file I/O,
programmatic menus, 16-colour DIBs).  Bringing it up hardens the `win16/`
layer well past the smaller fixtures.  This package holds its boot wiring
(`runtime`) and tests; recovered hot-path hooks would live here too.
"""
from . import _env  # noqa: F401  (puts the dos_re framework on sys.path)
