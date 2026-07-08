"""Recovered SimAnt routines — VM-free clean-room ports of hot ASM.

The endgame form of a lifted island: pure Python (no cpu/mem/hooks/offsets) that
behaves like the original C source function.  `simant/hooks.py` adapts these to
the running interpreter; a native VM-less port calls them directly.
"""
