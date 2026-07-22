# dos_re 3.0 in the Win16 context — the concept mapping

dos_re 3.0 (merged upstream at `a6ae58c`) replaces the 2.0 staged pipeline
(oracle → VMless → CPUless walls, tick demos, island registries, per-stage
players) with one evidence-driven model: stable identities, a single replay
artifact format, an Execution Atlas projection, one implementation catalog,
and an immutable execution plan that binds exactly one owner to every
reachable identity.  This document maps each 3.0 concept onto its Win16
equivalent and records the adaptation decisions.  It is the design contract
for the `win16-re-3.0` migration; the SimAnt-specific plan lives in the
consuming game project.

Authoritative upstream references (read them first):
`dos_re/docs/architecture.md`, `glossary.md`, `execution_atlas.md`,
`execution_planner.md`, `execution_regions.md`, `override_architecture.md`,
`progressive_replacement.md`, `replay_architecture.md`,
`verification_contracts.md`, and the worked example
`dos_re/examples/tiny_frame_game/walkthrough.py`.

## What win16_re consumes unchanged

These dos_re modules are backend-neutral by declared dependency guards
(enforced by `dos_re/tests/test_architecture_contract.py`) and are consumed
directly — win16_re must never fork or re-implement them:

| Module | Role for Win16 |
|---|---|
| `dos_re.identity` | All stable identities.  `address_space` is a free string; win16 mints its own (below). |
| `dos_re.replay` | THE replay format: `ReplayArtifact`, `ReplayPoint(Coordinate)`, `ReplayEvent`, `ContinuationState`, `CanonicalState`, `ReplayDriver` protocol, `verify_interval` / `verify_checkpointed` / `bisect_divergence`. |
| `dos_re.execution` | Catalogs, configuration, policy profiles, `plan_execution`, `bind_plan_implementations`, `DetachmentReport`. Carrier ids are plain strings — win16 declares its own. |
| `dos_re.atlas` | The Execution Atlas.  Win16 Recovery IR is already dos_re-IR-shaped (`win16.irgen` is a front-end over `dos_re.lift.irgen_core`), so `import_recovery_ir` ingests it with the win16 address space. |
| `dos_re.regions` | Long-lived execution islands (`RegionDispatcher`, `RegionSession`). |
| `dos_re.features` | Planned optional behavior (`FeatureController`); behavioral features are replay-recorded on project channels. |
| `dos_re.verification_contract` | Projection contracts shared by replay verification and planning. |
| `dos_re.materialized_plan`, `dos_re.export`, `dos_re.bootstrap_runtime` | Closed-world packaging. |
| `dos_re.detachment_guard` | The development-time import wall (`extra_forbidden` carries the win16/game prefixes). |
| `dos_re.runtime_miss` | `RuntimeExecutionFrontier` — the runtime miss witness raised by any carrier. |
| `dos_re.observable` | Observable-effect digests for checkpointed verification. |

## The mapping

| dos_re 3.0 concept | Win16 realization |
|---|---|
| `ProgramIdentity` | The game project's key (e.g. `simant:1.0`).  Game-side. |
| `ImageIdentity` | The NE executable: label + sha256 of the EXE file. |
| `FunctionIdentity` address | Paragraph `CS:IP` exactly as the whole recovery pipeline already keys it (`win16.loader` maps NE segments at fixed paragraph bases from `IMAGE_BASE_PARA`), formatted by `identity.real_mode_address`.  Address space string: **`win16-para`**.  The NE logical `(segment, offset)` pair stays attached as node metadata; paragraph addressing is deterministic because the loader layout is fixed and recorded in the boot manifest's `seg_bases`. |
| `RuntimeCodeSlotIdentity` | Runtime-minted thunk slots (`mint_proc_thunk` / MakeProcInstance products) in the thunk segment. |
| `BoundaryIdentity` namespaces | `api` (import-thunk transition into KERNEL/USER/GDI/...), `callback` (host→guest re-entry: WndProc, DialogProc, TimerProc, EnumProc), `interrupt` (raw INT 21h etc.), `message` (message-dispatch boundaries where declared). |
| ReplayArtifact | THE Win16 recording format (replaces the v4 JSONL demo).  Event channels (project-visible, win16-owned): `win16.input` (posted message arrival `[hwnd, msg, wparam, lparam, tick, pt]`), `win16.clock` (GetTickCount sample), `win16.dialog` (modal dialog event), `win16.messagebox` (MessageBox button result), `win16.quit`.  Game projects add their own channels for behavioral features. |
| `ReplayPointCoordinate` schema | **`win16-re:guest-instruction-count:v1`** — the guest instruction count that already keys v4 demos.  It is a guest coordinate, not a host dispatch count, so it is legal under the 3.0 rule.  A semantic coordinate (completed sim tick / message-pump boundary) can be introduced later per game as a second timeline schema. |
| `ContinuationState` | The vmsnap capture re-expressed: `schema_id = "win16-re-continuation-v1"`; regions `{"memory": <4 MB image>, "system": <pickled Win16System graph, host wiring detached>}`; metadata = CPUState (incl. x87), instruction count, callback frames, free_para, async key state, loaded libraries; `event_cursor` = replay event cursor.  Modal-dialog-open remains the one refusal (a capture there raises). |
| `CanonicalState` projection | Game-observable digest fields: CPU register file, virtual clock, armed timer intervals, per-window surface hashes; regions: guest memory (optionally with poison mask ranges excluded via the projection contract's `excluded_internal_state`). |
| Execution carrier ids | `win16-interpreted-cpu` (CPU8086 + replacement hooks), `win16-generated-vmless-cpu` (lifted graph on the CPU carrier), `win16-cpuless` (`CpuFreeCarrier` + `Win16CpulessPlatform`), `native-state` (recovered logic over `NativeGameState`-style owned images). |
| `ImplementationOrigin` / properties | interpreted baseline (origin `interpreted`); lifted/generated graphs and CPUless corpora (origin `generated`, recovery level `generated-vmless` / `generated-cpuless`); hand-recovered islands and native bodies (origin `authored`, category REQUIRED: faithful / enhancement / behavioral / instrumentation). |
| Hook | A `replacement_hooks` entry at a cross-owner edge, installed by a `BackendAdapter.activate` for the interpreted/vmless carriers.  It is derived from the plan, not a registry: when one implementation owns both endpoints the seam collapses. |
| Execution region | A long-lived Win16 island (e.g. a game's sim-tick TimerProc subsystem): `ExecutionRegionContract` with entries at callback targets, exits at their far-return continuations, `replay_boundaries` at message-pump/timer boundaries, verification via the interior semantic projection + exit continuation seams. |
| `BootstrapProvider` | `ExeBootstrapProvider` = NE load from the original EXE (`win16.loader`); `BuildImageBootstrapProvider` = the EXE-free boot image (`win16.bootimage`); composite = image + assets (resources kept by the stripped program identity). |
| Fallback wall | `FallbackPolicy.FORBIDDEN` ⇒ `cpu.interp_forbidden = True` (both the CPU8086 and the CPU-free carrier honour the plan); an actual miss raises `RuntimeExecutionFrontier`. |
| Import wall | `dos_re.detachment_guard.import_guard(extra_forbidden=("win16.loader", "win16.bootimage", <game generated/lifted packages>, ...))`. |
| One player | ONE game-side `scripts/play.py`; `--profile development|verification|detached|release` selects composition through `ExecutionConfiguration`.  Per-composition players (`play_vmless.py`, `play_cpuless.py`) are retired, mirroring dos_re's architecture contract which bans those names. |
| Tick demo | RETIRED (dos_re deleted `tick_demo`; `win16/tick_demo.py` deleted in this migration).  Mode-independent equivalence is expressed as replay verification through a shared `CanonicalState` schema between profiles. |
| Hook verifier | `dos_re.verification.install_hook_verifier` + `win16.verify.clone_machine` survive unchanged as the per-call development differential. |

## Win16-specific facts the mapping must respect

- **The OS is the first hook layer.**  A Win16 app has no all-original
  baseline below the API surface: KERNEL/USER/GDI are Python services from
  instruction zero.  In catalog terms the API surface is a set of
  `RuntimeService`s plus `api:*` boundary transitions — never implementations
  claiming game targets.  The "oracle" profile is the interpreted CPU over
  those same services.
- **Control flow re-enters the guest.**  DispatchMessage/SendMessage/TimerProc
  call back into game code (`win16.callback.call_far`).  Atlas edges of kind
  `callback` carry these host→guest transitions; the CPU-free carrier routes
  them through `win16.cpuless.install_callback_dispatch` into recovered
  bodies resolved by the plan.
- **Windows-object state lives outside guest memory.**  The window tree,
  surfaces, menus, timers, message queues and MCI state are Python objects.
  They are part of `ContinuationState` (the `system` region) and are compared
  through projected surface hashes / clock / timer fields — not byte-compared
  as machine memory.
- **Instruction-keyed determinism is composition-specific.**  A replay
  captured under one composition replays bit-faithfully only under an
  execution profile with the same `ReplayExecutionIdentity`; the artifact's
  event stream is portable, its continuations are profile-local.  This is the
  3.0 formulation of the old "demos are hook-config-specific" rule.

## Implementation status (what has landed)

The mapping above is realized in code:

| Layer | Module(s) | State |
|---|---|---|
| Replay format | `win16/replay.py` (channels, coordinate schema, recorder, input driver, `ArtifactRecorder`), `win16/continuation.py` (`ContinuationState` codec) | DONE; the v4 gate demo converts byte-identically (`scripts/demo2replay.py`) |
| Execution evidence | `win16/evidence.py` (function-visit + dispatch/callback observation probe) | DONE; feeds `dos_re/tools/atlas.py ingest-replay` |
| Verification driver | `win16/replay_driver.py` (`ReplayDriver` + `win16-re-observable-v1` projection contract) | DONE; `verify_interval` proves oracle ≡ detached candidate |
| Catalog + plan | `simant/execution.py` (game-side), `dos_re.execution` | DONE; islands are authored-faithful, generated corpora are catalog entries, one `ExecutionPlan` per profile |
| One player | `scripts/play.py` / `scripts/replay.py` `--profile {development,detached}` | DONE; `boot_detached` is the single canonical detached construction |

## Migration bridges (temporary, marked in code)

- `win16.cpuless.module_name/load_recovered/run_deep` — corpus loading residue
  from the deleted `dos_re.lift.standalone`; removed when the
  ImplementationCatalog materializes callables at plan time.
- `win16.demo` (v4 JSONL) is now READ-ONLY residue: the v4 recorder is
  retired (recording is `ArtifactRecorder`), and the v4 reader (`DemoDriver`)
  survives only until the byte-exact analysis scripts
  (`checkpoints`/`liftverify`/`verifyislands`/`adaptverify`) migrate to
  `input_driver_for(ReplayArtifact)` — which has the identical interface.
  dos_re 3.0 itself retains no legacy replay reader; old recordings convert
  once or are re-recorded.
