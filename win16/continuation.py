"""Win16 ContinuationState codec: full-machine state as a replay continuation.

dos_re 3.0 separates a profile-private ``ContinuationState`` (everything a
deterministic resume needs; cached inside a ``ReplayArtifact`` as a base plus
changed pages) from the ``CanonicalState`` comparison projection.  For a Win16
machine the continuation is exactly what a directory snapshot captured — the
guest memory image, the CPU register record, and the Python-side OS object
graph — re-expressed as artifact regions:

    regions["memory"]   the 4 MB guest address-space image
    regions["system"]   the pickled Win16System graph (host wiring detached)
    metadata            the machine-state record (CPUState incl. x87,
                        instruction count, pending callback frames, allocator
                        frontier, polled keys, loaded libraries)

The one capture refusal carries over: a machine parked inside a modal
DialogBox/MessageBox is not on the resumable path and raises.
"""
from __future__ import annotations

from dos_re.replay import ContinuationState

from .vmsnap import (machine_meta, pickle_system, refuse_modal_dialog,
                     restore_machine_payload)

#: The Win16 continuation schema.  Bump ONLY with a deliberate format change:
#: profiles with different schemas never share bases or caches.
CONTINUATION_SCHEMA = "win16-re-continuation-v1"


def capture_continuation(machine, *, event_cursor: int,
                         note: str = "", game: str = "") -> ContinuationState:
    """Capture the machine as a profile-private replay continuation."""
    refuse_modal_dialog(machine)
    sysobj = machine.api.services["system"]
    return ContinuationState(
        schema_id=CONTINUATION_SCHEMA,
        metadata=machine_meta(machine, note=note, game=game),
        regions={
            "memory": bytes(machine.mem.data),
            "system": pickle_system(sysobj),
        },
        event_cursor=int(event_cursor),
    ).normalized()


def apply_continuation(machine, state: ContinuationState) -> None:
    """Overlay a captured continuation onto a freshly constructed machine.

    The caller owns machine construction (the execution profile's bootstrap
    provider decides EXE load vs boot image) and any integrity gate beyond
    the artifact's own page hashes."""
    if state.schema_id != CONTINUATION_SCHEMA:
        raise ValueError(
            f"continuation schema {state.schema_id!r} is not "
            f"{CONTINUATION_SCHEMA!r} — captured under a different backend")
    restore_machine_payload(machine, dict(state.metadata),
                            state.regions["memory"], state.regions["system"])
