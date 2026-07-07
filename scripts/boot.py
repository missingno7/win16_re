"""Boot any test game through the win16 layer and report the frontier.

    python scripts/boot.py <game> [max_steps]

Loads the NE, runs it, and prints how far the interpreter got and what stopped
it (unimplemented API / opcode / DOS service) with CS:IP, the last trace lines,
and the API call log — the honest bring-up report for hardening the win16
layer against a new game.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import ppython._env  # noqa: E402,F401  (dos_re on sys.path)

from scripts.games import GAMES, game_exe, game_winflags  # noqa: E402
from win16.api.system import Win16System  # noqa: E402
from win16.app import create_machine  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(f"usage: boot.py <game> [max_steps]\n"
                         f"games: {', '.join(sorted(GAMES))}")
    name = sys.argv[1]
    max_steps = int(sys.argv[2]) if len(sys.argv) > 2 else 3_000_000
    exe = game_exe(name)
    if not exe.exists():
        raise SystemExit(f"{exe} not found")

    machine = create_machine(exe, winflags=game_winflags(name))
    Win16System(machine)
    cpu = machine.cpu
    hdr = machine.exe.header
    print(f"[{name}] {exe.name}: {hdr.segment_count} segs, entry "
          f"seg{hdr.entry_seg}:{hdr.entry_ip:04X}, modules "
          f"{', '.join(machine.exe.modules)}")
    print(f"[{name}] segment bases: {[f'{b:04X}' for b in machine.seg_bases[1:]]}, "
          f"osfixup sites {len(machine.osfixups)}")
    cpu.trace_enabled = True
    try:
        steps = cpu.run(max_steps)
        print(f"\n[{name}] ran {steps} steps without stopping; "
              f"at {cpu.s.cs:04X}:{cpu.s.ip:04X}")
    except Exception as exc:  # noqa: BLE001 — the probe reports everything
        print(f"\n[{name}] STOP after {cpu.instruction_count} instructions at "
              f"{cpu.s.cs:04X}:{cpu.s.ip:04X}\n    {type(exc).__name__}: {exc}")
    print("\nlast trace:")
    for line in cpu.trace[-16:]:
        print("   ", line)
    if machine.api.call_log:
        print("\nlast API calls:")
        for line in machine.api.call_log[-24:]:
            print("   ", line)


if __name__ == "__main__":
    main()
