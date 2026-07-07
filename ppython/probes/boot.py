"""Boot probe: run PYTHON.EXE from its NE entry point until the frontier.

Prints how far the interpreter gets and what stopped it (unsupported opcode,
unimplemented Win16 API, ...).  This is the honest bring-up report, run it
after any loader/API change:

    python -m ppython.probes.boot [max_steps]
"""
from __future__ import annotations

import sys

from ppython.runtime import create_machine


def main() -> None:
    max_steps = int(sys.argv[1]) if len(sys.argv) > 1 else 2_000_000
    m = create_machine()
    cpu = m.cpu
    print(f"entry {cpu.s.cs:04X}:{cpu.s.ip:04X}  ds/es={cpu.s.ds:04X} "
          f"ss:sp={cpu.s.ss:04X}:{cpu.s.sp:04X}")
    print(f"segment bases: {[f'{b:04X}' for b in m.seg_bases[1:]]}, "
          f"free_para={m.free_para:04X}, osfixup sites={len(m.osfixups)}")
    cpu.trace_enabled = True
    try:
        steps = cpu.run(max_steps)
        print(f"\nran {steps} steps without stopping; at {cpu.s.cs:04X}:{cpu.s.ip:04X}")
    except Exception as exc:  # noqa: BLE001 — probe reports everything
        print(f"\nSTOP after {cpu.instruction_count} instructions: "
              f"{type(exc).__name__}: {exc}")
    print("\nlast trace:")
    for line in cpu.trace[-25:]:
        print("  ", line)
    if m.api.call_log:
        print("\nAPI calls so far:")
        for line in m.api.call_log[-40:]:
            print("  ", line)


if __name__ == "__main__":
    main()
