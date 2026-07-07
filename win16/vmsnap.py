"""Full-machine snapshots: memory + CPU + the Win16 OS object graph.

Unlike a DOS machine, part of the Win16 world state lives in Python objects
(windows, surfaces, handle table, timers, file overlay).  A snapshot is
therefore three artifacts in a directory:

    memory.bin      the VM memory image
    state.json      CPUState (incl. x87), allocator frontier, metadata
    system.pickle   the Win16System object graph (machine ref stripped)

Snapshots must be taken at a message boundary (inside GetMessage, before the
next message) — that is the only architected quiescent point.  Taking one
while a modal dialog is open is refused loudly (dialog event queues are
transient host state).
"""
from __future__ import annotations

import hashlib
import json
import pickle
from dataclasses import asdict
from pathlib import Path


class SnapshotError(RuntimeError):
    pass


def digest(machine) -> str:
    """Fingerprint of the GAME-OBSERVABLE state: memory, CPU registers, every
    window surface, the virtual clock and the armed timer intervals.  The
    pump's internal schedule (timer_due) is deliberately excluded — a demo
    replay dictates message timing instead of scheduling it, and the game
    cannot observe the difference."""
    h = hashlib.sha256()
    h.update(bytes(machine.mem.data))
    state = asdict(machine.cpu.s)
    h.update(json.dumps(state, sort_keys=True, default=repr).encode())
    sysobj = machine.api.services.get("system")
    if sysobj is not None:
        for win in sysobj.windows:
            h.update(bytes(win.surface.pixels))
        h.update(str(sorted(sysobj.timers.items())).encode())
        h.update(str(sysobj.clock_ms).encode())
    return h.hexdigest()


def save_snapshot(machine, out_dir: str | Path, *, note: str = "") -> Path:
    from win16.api.dialogs import Dialog
    out = Path(out_dir)
    sysobj = machine.api.services["system"]
    if any(isinstance(o, Dialog) for o in sysobj.handles._objects.values()):
        raise SnapshotError("cannot snapshot while a modal dialog is open")
    out.mkdir(parents=True, exist_ok=True)

    (out / "memory.bin").write_bytes(bytes(machine.mem.data))

    meta = {
        "kind": "win16-snapshot",
        "version": 1,
        "note": note,
        "exe": machine.exe.path.name,
        "cpu": asdict(machine.cpu.s),
        "instruction_count": machine.cpu.instruction_count,
        "free_para": machine.free_para,
        "digest": digest(machine),
    }
    (out / "state.json").write_text(json.dumps(meta, indent=1))

    saved_machine, saved_source = sysobj.machine, sysobj.message_source
    sysobj.machine = None
    sysobj.message_source = None
    try:
        (out / "system.pickle").write_bytes(pickle.dumps(sysobj))
    finally:
        sysobj.machine = saved_machine
        sysobj.message_source = saved_source
    return out


def load_snapshot(snap_dir: str | Path, machine_factory):
    """Rebuild a machine from a snapshot.  `machine_factory` is the game
    adapter's create_machine (fresh loader + API registry); the snapshot then
    overlays memory, CPU and the OS object graph."""
    from dos_re.cpu import CPUState
    snap = Path(snap_dir)
    meta = json.loads((snap / "state.json").read_text())
    if meta.get("kind") != "win16-snapshot":
        raise SnapshotError(f"{snap}: not a win16 snapshot")

    machine = machine_factory()
    mem_image = (snap / "memory.bin").read_bytes()
    if len(mem_image) != len(machine.mem.data):
        raise SnapshotError("memory image size mismatch")
    machine.mem.data[:] = mem_image
    machine.cpu.s = CPUState(**meta["cpu"])
    machine.cpu.instruction_count = meta["instruction_count"]
    machine.free_para = meta["free_para"]

    sysobj = pickle.loads((snap / "system.pickle").read_bytes())
    sysobj.machine = machine
    machine.api.services["system"] = sysobj

    got = digest(machine)
    if got != meta["digest"]:
        raise SnapshotError(
            f"restored digest {got[:16]} != saved {meta['digest'][:16]} — "
            "snapshot did not restore bit-exact")
    return machine
