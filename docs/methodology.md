# Methodology — the naming/altitude discipline

This is the honesty discipline that keeps recovery from drifting into fiction.
It is dos_re's methodology applied to Win16; the full crystallization pyramid,
hook lifecycle, and altitude rules live in
[`dos_re/docs/methodology.md`](../dos_re/docs/methodology.md) and are not
repeated here. This page is the Win16-flavoured summary.

## The one rule

```text
Do not write a native reimplementation first and hope it matches.
Exhaust truth from the original first, then let the reimplementation crystallize from that evidence.
```

The original executable is the oracle. A clean Python routine is a *hypothesis*
until it is diffed against the original ASM (for lifted game routines) or
verified against observed behaviour (for API services). Never infer behaviour
from what "probably" happens in other Windows programs — the only oracle is
*this* executable and *this* API call's observed argument/return contract.

## Two kinds of recovery in this repo

Win16 recovery has two distinct fronts, and the evidence standard differs:

1. **The OS API surface (`win16/`).** These are not lifted from the game — they
   are Python implementations of documented Windows behaviour. The evidence is
   the **call site**: the arguments the game pushes and the return it reads
   (see [`bringing_up_a_game.md`](bringing_up_a_game.md)). The bar is "the game
   takes its correct path"; the frontier loop and the fixture games keep the
   surface honest. This layer is game-agnostic.
2. **The game's own routines (a game package's `hooks.py` / `recovered/`).**
   These *are* lifted from the game, and the bar is byte/pixel-exact against the
   original ASM — the A/B oracle in [`lifted_islands.md`](lifted_islands.md).

## Status ladder

Every recovered *game* routine carries an explicit confidence status; a name
climbs it on evidence, never on appearance:

```text
GUESS        hypothesised from a reference/heuristic, not yet checked vs ASM
OBSERVED     behaviour watched in the running ASM, not yet reimplemented
RECOVERED    reimplemented as clean Python, not yet diffed vs ASM
VERIFIED     pixel/byte-exact vs the original ASM under an A/B run over real play
CANONICAL    verified and adopted as the source of truth (ASM retired for it)
```

For API services the analogous states are: identified-from-call-site →
implemented-to-observed-contract → exercised-by-a-game (and, for the shared
layer, pinned by a unit test where practical).

## Fail-loud over guessed fallback

A fail-loud frontier turns unknown behaviour into a precise stop: the API/opcode
name, CS:IP, the call log, the trace tail. Do not replace it with a plausible
stub to keep the game running. When one triggers, it is a new oracle candidate:
identify it from its call site, implement the observed contract, and keep the
message specific if any branch stays unknown.

The corollary, learned the hard way (see [`pitfalls.md`](pitfalls.md)): a stub
that returns a wrong-but-plausible value is worse than a loud stop, because it
sends the game down a path you then debug blind. `MessageBox` returning `IDOK`
for a Yes/No box, `GetTickCount` frozen during a busy-wait — both looked fine
and both silently broke the game.

## Presentation is an approximation, and that's allowed

Rendering is pixel-exact *against the game's own surfaces*, but the rasterizer
is ours (fixed-cell fonts, numpy blits). Audio is event-exact (the `sound_log`
is authoritative) with a flexible host backend. This is the dos_re equivalence
contract: gameplay byte-exact, rendering pixel-exact-but-mechanism-flexible,
audio event-exact-but-mixer-flexible, input semantic-exact. Do not confuse "the
window looks right" with "the game logic is verified" — the pixel gate and the
owner's playtests catch what state probes miss.
