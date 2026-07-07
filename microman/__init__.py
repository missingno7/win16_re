"""MicroMan (Win16) game package — all knowledge of MICROMAN.EXE lives here.

MicroMan is a WAP (Windows Animation Package) demo used to harden the
game-agnostic `win16/` layer and to rehearse the dos_re lifted-island method
on a real graphics engine (the road to SimAnt).  Unlike ppython (the byte-
exact RE target), microman is a fixture: this package holds its boot wiring
(`runtime`), its lifted-island hooks (`hooks`), and its tests (`tests/`).
"""
from . import _env  # noqa: F401  (puts the dos_re framework on sys.path)
