"""Replay a recorded demo headlessly — the deterministic evidence tool.

    python scripts/replay.py DEMO.jsonl [--budget N] [--png DIR] [--snapshot DIR]

Feeds the recorded message/dialog-event stream back into a fresh machine and
reports how far it got and the game-observable state digest.  A divergence
(machine asking for something the demo doesn't have next) raises loudly with
the record index.  This is the baseline every future hook/native replacement
must reproduce bit-exact.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ppython.runtime import assets_present, create_machine
from win16.demo import DemoDivergence, DemoEnded, DemoPlayer
from win16.vmsnap import digest, save_snapshot


def main() -> None:
    ap = argparse.ArgumentParser(description="Replay a win16 demo headlessly.")
    ap.add_argument("demo", help="demo file recorded by play.py --record")
    ap.add_argument("--budget", type=int, default=200_000_000,
                    help="max instructions to execute")
    ap.add_argument("--png", metavar="DIR", default=None,
                    help="dump every window surface to DIR when done")
    ap.add_argument("--snapshot", metavar="DIR", default=None,
                    help="save a machine snapshot of the end state")
    args = ap.parse_args()
    if not assets_present():
        raise SystemExit("assets/PYTHON.EXE not found")

    player = DemoPlayer(args.demo)
    print(f"[replay] {args.demo}: {len(player.records)} records (exe {player.exe})")

    machine = create_machine()
    machine.cpu.trace_enabled = False
    sysobj = machine.api.services["system"]
    sysobj.message_source = player.next_message
    machine.api.services["demo_player"] = player

    outcome = "budget exhausted"
    try:
        machine.cpu.run(args.budget)
    except DemoEnded as exc:
        outcome = f"demo ended: {exc}"
    except DemoDivergence as exc:
        print(f"[replay] DIVERGENCE: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:  # noqa: BLE001 — report and re-raise, fail loud
        print(f"[replay] VM stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"[replay] at record {player.pos}/{len(player.records)}, "
              f"instruction {machine.cpu.instruction_count}", file=sys.stderr)
        raise
    if machine.cpu.halted:
        outcome = "app exited cleanly"

    print(f"[replay] {outcome}")
    print(f"[replay] records consumed: {player.pos}/{len(player.records)}")
    print(f"[replay] instructions: {machine.cpu.instruction_count:,}")
    print(f"[replay] clock: {sysobj.clock_ms} ms, windows: "
          f"{[w.wndclass.name for w in sysobj.windows]}")
    print(f"[replay] digest: {digest(machine)}")

    if args.png:
        from win16.png import write_png
        out = Path(args.png)
        out.mkdir(parents=True, exist_ok=True)
        for i, win in enumerate(sysobj.windows):
            s = win.surface
            path = out / f"replay{i}_{win.wndclass.name}.png"
            write_png(path, s.w, s.h, bytes(s.pixels))
            print(f"[replay] wrote {path}")
    if args.snapshot:
        save_snapshot(machine, args.snapshot, note=f"end of demo {args.demo}")
        print(f"[replay] snapshot saved to {args.snapshot}")


if __name__ == "__main__":
    main()
