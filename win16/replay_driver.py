"""Win16 ReplayDriver: one execution profile as a dos_re 3.0 replay adapter.

Implements the ``dos_re.replay.ReplayDriver`` protocol for a Win16 machine so
``verify_interval`` can run THE SAME ReplayArtifact against different
execution profiles (the interpreted oracle, a plan-bound detached
composition, ...) and compare their ``CanonicalState`` projections.

The projection schema ``win16-re-observable-v1`` is the game-observable
state, comparison-safe ACROSS compositions:

* fields — the CPU register record (incl. x87), the VIRTUAL instruction
  count (generated implementations preserve it instruction-exactly, so it is
  a comparison field, not carrier noise), the virtual clock, the armed timer
  intervals, the window list, and one content hash per window surface;
* region ``memory`` — the guest address-space image with the recovered-code
  ranges MASKED to zero (the EXE-independence comparison seam: a poisoned
  boot image can only be compared against an EXE-full oracle under the
  mask; on the poisoned side the mask is a no-op by construction).

Interval support: the exactly reproducible stops are the timeline BASE and
END (position = events applied; the end also runs the machine to input
exhaustion).  Mid-timeline stops need a host-boundary parking protocol and
raise until built — never an approximate stop.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict

from dos_re.replay import (CanonicalState, ContinuationState, ReplayArtifact,
                           ReplayError, ReplayExecutionIdentity, ReplayPoint)
from dos_re.verification_contract import (VerificationProjectionContract,
                                          VerificationRepresentation)

from .continuation import apply_continuation, capture_continuation
from .replay import ReplayExhausted, input_driver_for

PROJECTION_SCHEMA = "win16-re-observable-v1"

PROJECTION_CONTRACT = VerificationProjectionContract(
    projection_id="win16-observable/v1",
    representation=VerificationRepresentation.SEMANTIC_STATE,
    schema_id=PROJECTION_SCHEMA,
    required_fields=("cpu", "instruction_count", "clock_ms", "timers",
                     "windows", "surfaces"),
    required_regions=("memory",),
    excluded_internal_state=("recovered-code-bytes (masked)",
                             "pump-internal timer_due schedule",
                             "host wiring"),
)


class Win16ReplayDriver:
    """One Win16 execution profile driving a ReplayArtifact.

    ``machine_factory`` builds THIS profile's machine (the interpreted
    oracle's EXE boot, or a plan-bound detached boot) — a restored
    continuation is overlaid on a fresh factory machine.  ``mask_ranges``
    are (linear_start, length) recovered-code ranges hashed as zero in the
    projection (from the boot manifest; identical for every profile of one
    program so the projections stay comparable).
    """

    def __init__(self, *, profile: ReplayExecutionIdentity, machine_factory,
                 mask_ranges=(), run_chunk: int = 200_000):
        self._profile = profile
        self._factory = machine_factory
        self._mask = tuple(mask_ranges)
        self._chunk = int(run_chunk)
        self.machine = None
        self._input = None
        self._timeline = None
        self._end_ordinal = None
        self._finished = False

    # -- ReplayDriver protocol --------------------------------------------

    @property
    def profile(self) -> ReplayExecutionIdentity:
        return self._profile

    def _cursor(self) -> int:
        if self._finished:
            return self._end_ordinal
        if self._input is not None and self._input.sys is not None:
            return self._input.current_ordinal
        return getattr(self, "_pending_seek", 0)

    @property
    def current_point(self) -> ReplayPoint:
        if self._timeline is None:
            raise ReplayError("driver has no position: restore() first")
        return ReplayPoint(self._cursor(), self._timeline)

    def capture(self) -> ContinuationState:
        return capture_continuation(self.machine, event_cursor=self._cursor())

    def restore(self, state: ContinuationState, point: ReplayPoint) -> None:
        self.machine = self._factory()
        apply_continuation(self.machine, state)
        self._timeline = point.timeline_id
        self._finished = False
        self._pending_seek = point.ordinal

    def replay_to(self, artifact: ReplayArtifact, point: ReplayPoint) -> None:
        if self._timeline is None:
            raise ReplayError("driver has no machine: restore() first")
        sysobj = self.machine.api.services["system"]
        # Build + install the input driver exactly ONCE per restored machine
        # (install() chains sysobj.yield_check into _on_yield; installing a
        # second time would capture our own hook as _prev_yield and recurse).
        if sysobj.demo_driver is None:
            self._input = input_driver_for(artifact)
            self._input.install(sysobj)
            self._input.seek(getattr(self, "_pending_seek", point.ordinal))
        self._end_ordinal = artifact.end_point.ordinal
        if point.ordinal == self._input.current_ordinal:
            return
        if point.ordinal != self._end_ordinal:
            raise ReplayError(
                f"win16 drivers stop exactly at the timeline base or end; "
                f"mid-timeline point {point.ordinal} needs the host-boundary "
                f"parking protocol (not built yet)")
        cpu = self.machine.cpu
        try:
            while True:
                cpu.run(self._chunk)
                if cpu.halted:
                    break
        except ReplayExhausted:
            pass
        self._finished = True

    def project(self) -> CanonicalState:
        machine = self.machine
        sysobj = machine.api.services["system"]
        if self._mask:
            data = bytearray(machine.mem.data)
            for start, length in self._mask:
                data[start:start + length] = bytes(length)
            memory = bytes(data)
        else:
            memory = bytes(machine.mem.data)
        surfaces = [hashlib.sha256(bytes(w.surface.pixels)).hexdigest()
                    for w in sysobj.windows]
        cpu_state = {k: (v if isinstance(v, (int, str, bool)) else repr(v))
                     for k, v in sorted(asdict(machine.cpu.s).items())}
        return CanonicalState(
            schema_id=PROJECTION_SCHEMA,
            event_cursor=self._cursor(),
            fields={
                "cpu": cpu_state,
                "instruction_count": machine.cpu.instruction_count,
                "clock_ms": sysobj.clock_ms,
                "timers": sorted(f"{k}:{v}" for k, v in sysobj.timers.items()),
                "windows": [w.wndclass.name for w in sysobj.windows],
                "surfaces": surfaces,
            },
            regions={"memory": memory},
        ).normalized()

    def verification_projection_contract(self) -> VerificationProjectionContract:
        return PROJECTION_CONTRACT
