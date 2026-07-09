"""Full-machine snapshots: memory + CPU + the Win16 OS object graph.

Unlike a DOS machine, part of the Win16 world state lives in Python objects
(windows, surfaces, handle table, timers, file overlay).  A snapshot is
therefore three artifacts in a directory:

    memory.bin      the VM memory image
    state.json      CPUState (incl. x87), allocator frontier, metadata
    system.pickle   the Win16System object graph (machine ref stripped)

A snapshot can be taken at ANY CPU instruction boundary — memory + CPUState (SP/
IP included) fully capture the VM, and no Python handler loop spans two top-level
steps, so the object graph pickles cleanly.  The interactive host (play.py, F9)
parks the CPU thread at either a GetMessage boundary OR an instruction-chunk
boundary (see InteractiveDriver.check_pause), so snapshots work even while the
game busy-polls PeekMessage (SimAnt's menus / in-game loops).

The ONE exception is a modal DialogBox/MessageBox: it runs a NESTED Python
message loop, so its call-stack state is not on the resumable path.  Taking a
snapshot while one is open is refused loudly here (use an inspection snapshot +
demos for those).
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


def snapshot_game(snap_dir: str | Path) -> str:
    """The game name recorded in a snapshot (empty if pre-v3 / unknown)."""
    meta = json.loads((Path(snap_dir) / "state.json").read_text())
    return meta.get("game", "")


def save_snapshot(machine, out_dir: str | Path, *, note: str = "",
                  game: str = "") -> Path:
    from win16.api.dialogs import Dialog
    out = Path(out_dir)
    sysobj = machine.api.services["system"]
    if any(isinstance(o, Dialog) for o in sysobj.handles._objects.values()):
        raise SnapshotError("cannot snapshot while a modal dialog is open")
    out.mkdir(parents=True, exist_ok=True)

    (out / "memory.bin").write_bytes(bytes(machine.mem.data))

    meta = {
        "kind": "win16-snapshot",
        "version": 3,
        "note": note,
        "game": game,                   # canonical game name (scripts/games.py)
        "exe": machine.exe.path.name,
        "cpu": asdict(machine.cpu.s),
        "instruction_count": machine.cpu.instruction_count,
        "free_para": machine.free_para,
        # Game-observable polled input (GetAsyncKeyState reads these).
        "async_keys": sorted(machine.api.services.get("async_keys", set())),
        "async_keys_tapped": sorted(
            machine.api.services.get("async_keys_tapped", set())),
        "digest": digest(machine),
    }
    (out / "state.json").write_text(json.dumps(meta, indent=1))

    # Detach the live host wiring before pickling: message_source, input_drainer
    # and yield_check are bound methods of the interactive driver, which holds a
    # threading.Condition (an RLock) that cannot be pickled.  They are re-attached
    # by whoever resumes the snapshot.
    _HOST_ATTRS = ("machine", "message_source", "input_drainer", "yield_check")
    saved = {a: getattr(sysobj, a, None) for a in _HOST_ATTRS}
    for a in _HOST_ATTRS:
        setattr(sysobj, a, None)
    try:
        (out / "system.pickle").write_bytes(pickle.dumps(sysobj))
    finally:
        for a, v in saved.items():
            setattr(sysobj, a, v)
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

    # Re-wire the selector heap: the VM Memory must consult the RESTORED
    # heap's selector->linear map (the pickle made a copy; the fresh boot's
    # dict knows nothing of the snapshot's allocations).
    if sysobj.huge_heap is not None:
        # Re-key the restored map to descriptors (RPL masked off): Memory looks
        # selectors up RPL-agnostically, and older snapshots were keyed by the
        # exact selector, so collapse any RPL aliases to the descriptor form.
        from .hugeheap import descriptor
        hh = sysobj.huge_heap
        rekeyed = {descriptor(k): v for k, v in hh.sel_base.items()}
        hh.sel_base.clear()
        hh.sel_base.update(rekeyed)
        machine.mem.sel_base = hh.sel_base
        machine.mem.sel_min = hh.first_selector & 0xFFFC

    # Polled key state (game-observable via GetAsyncKeyState).
    machine.api.services["async_keys"] = set(meta.get("async_keys", []))
    machine.api.services["async_keys_tapped"] = set(
        meta.get("async_keys_tapped", []))

    got = digest(machine)
    if got != meta["digest"]:
        raise SnapshotError(
            f"restored digest {got[:16]} != saved {meta['digest'][:16]} — "
            "snapshot did not restore bit-exact")
    return machine
