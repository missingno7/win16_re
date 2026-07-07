"""The Win16 system API surface, implemented as Python services over the VM.

In a Win16 port the operating system itself is the first hook layer: every
NE import (KERNEL/USER/GDI/...) resolves to a thunk slot whose behaviour is a
Python function.  Unimplemented calls fail loud (`Win16ApiGap`) and name the
exact MODULE.ordinal/name frontier — they are never silently stubbed.
"""
from .core import ApiRegistry, CallContext, Win16ApiGap  # noqa: F401
