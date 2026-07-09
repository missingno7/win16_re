# win16_re documentation

The reverse-engineering method for 16-bit Windows (NE) games, adapted from
[`dos_re`](../dos_re) (whose docs are the deeper reference for the
shared principles — evidence ladders, the lifting loop, the enhanced-layer
endgame). Read these when they apply; don't re-derive what dos_re already
wrote down.

Reading order for a newcomer: repo [README](../README.md) → this index →
[`win16_layer.md`](win16_layer.md) → [`methodology.md`](methodology.md) →
[`bringing_up_a_game.md`](bringing_up_a_game.md) → [`pitfalls.md`](pitfalls.md).

| Doc | What it covers |
|---|---|
| [`win16_layer.md`](win16_layer.md) | **The Win16-specific architecture** — how a Windows game differs from a DOS one: NE loading, the OS-API-as-hook-layer, the message-loop frame boundary, the selector memory model (static single-app protected mode), DIB/palette rendering, the `GetTickCount` busy-wait clock, and **child-window compositing** (windows within a window). |
| [`methodology.md`](methodology.md) | The naming/altitude discipline inherited from dos_re: the evidence ladder, the status ladder (GUESS → CANONICAL), fail-loud over guessed fallback, and how a lifted hook earns acceptance. |
| [`bringing_up_a_game.md`](bringing_up_a_game.md) | **The bring-up frontier loop** — the concrete procedure for getting a new NE game to boot: probe → hit a fail-loud frontier → identify the API from its call site → implement the observed contract → repeat. Includes how to read a call site. |
| [`pitfalls.md`](pitfalls.md) | The real mistakes this project made and the rule that fixed each — wrong probe channels, weak paint gates, ordinal-name mismatches, the frozen busy-wait clock, brittle instruction-count gates. |
| [`lifted_islands.md`](lifted_islands.md) | The per-game hot-path hook method (the dos_re island technique on Win16): PC-sample → live-trace the hot loop → lift as a Python island → A/B pixel-oracle gate. The worked example is `simant/hooks.py`. |

Related, outside `docs/`:

- [`../CLAUDE.md`](../CLAUDE.md) — the operational brief (what we're doing, the
  non-negotiables, where things stand).
- [`../AGENTS.md`](../AGENTS.md) — working rules for this repo.
- [`simant/run_status.md`](simant/run_status.md) — the journal (newest on
  top) and the standing-mechanisms registry. Check it before building any new
  tooling.

## The shared method (deeper reference in dos_re)

These are documented once, in dos_re, and apply here unchanged. Read them there
when you reach that phase:

- [`dos_re/docs/methodology.md`](../dos_re/docs/methodology.md) — the
  full crystallization pyramid and hook lifecycle.
- [`dos_re/docs/ai_porting_charter.md`](../dos_re/docs/ai_porting_charter.md)
  — the complete method: the proof spine, the determinism trap, the phased
  roadmap, the equivalence contracts (gameplay byte-exact, rendering
  pixel-exact, audio event-exact, input semantic-exact).
- [`dos_re/docs/pitfalls.md`](../dos_re/docs/pitfalls.md) — the 24
  general mistakes; ours in [`pitfalls.md`](pitfalls.md) are the Win16 additions.
