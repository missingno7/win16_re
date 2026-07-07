"""Run the game N steps and dump every window surface to artifacts/*.png.

    python -m ppython.probes.screenshot [steps]
"""
from __future__ import annotations

import sys
from pathlib import Path

from ppython.runtime import REPO_ROOT, create_machine
from win16.png import write_png


def main() -> None:
    steps = int(sys.argv[1]) if len(sys.argv) > 1 else 2_000_000
    m = create_machine()
    stop = "ran to step budget"
    try:
        m.cpu.run(steps)
    except Exception as exc:  # noqa: BLE001 — evidence probe
        stop = f"{type(exc).__name__}: {exc}"
    out_dir = REPO_ROOT / "artifacts"
    out_dir.mkdir(exist_ok=True)
    sys_obj = m.api.services["system"]
    print(f"after {m.cpu.instruction_count} instructions ({stop}); "
          f"clock={sys_obj.clock_ms}ms, windows={len(sys_obj.windows)}")
    for i, win in enumerate(sys_obj.windows):
        s = win.surface
        name = f"window{i}_{win.wndclass.name}_{win.handle:04X}.png"
        write_png(out_dir / name, s.w, s.h, bytes(s.pixels))
        print(f"  {name}: {s.w}x{s.h} title={win.title!r} visible={win.visible} "
              f"at ({win.x},{win.y})")


if __name__ == "__main__":
    main()
