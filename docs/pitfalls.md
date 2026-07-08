# Pitfalls — the mistakes this project made, and the rule that fixed each

The general list is [`dos_re/docs/pitfalls.md`](../dos_re/docs/pitfalls.md).
These are the **Win16-specific** additions — each one actually happened in this
repo, cost real debugging time, and left a rule.

## 1. Trusting a probe that the code never consults

A whole session concluded "no error boxes" from a probe installed as
`services["messagebox_ui"]` — a service the `MessageBox` handler never calls
(it's WinHelp-only). The box fired on every run; the real channel was the list
the handler itself appends to, `services["messagebox_log"]`. The owner's
playtest was the correct bug report.

> **Rule.** Before trusting a probe's negative result, grep the consumer and
> confirm the code path actually reads your probe. Prefer asserting on the
> artifact the code itself produces (the log the handler appends to, the
> rendered pixels) over a side-channel you installed.

## 2. A stub that returns a plausible-but-wrong value

`MessageBox` ignored the button-type nibble and always returned `IDOK(1)`.
SimAnt-style Yes/No prompts (microman's Restart) read that as "not Yes" → No.
It looked like it worked; it silently denied every confirmation.

> **Rule.** A wrong-but-plausible return is worse than a loud stop — it sends
> the game down a path you debug blind. Model the real contract (button set →
> the id the game checks), or fail loud.

## 3. A clock that freezes during a busy-wait

`GetTickCount` returned the message-boundary clock (`clock_ms`). SimAnt times
its splash with `while GetTickCount() - t0 < delay` **without pumping
messages**, so the clock never advanced and the splash spun forever.

> **Rule.** Time sources must advance on execution progress, not only at message
> boundaries. `GetTickCount` uses an instruction-derived monotonic floor.

## 4. Pixel-sum as a "did it render" gate

Boot tests tried "AntRoot surface sum > N" to detect the splash. Useless: a
solid grey background fill sums to ~96M while the mostly-black MAXIS splash sums
to ~3.6M, and substantial content lands within the first 200k instructions. The
threshold was simultaneously too high and too low.

> **Rule.** Gate a boot test on the **deep-startup API sequence** (e.g.
> `CreateFont` + `SetDIBitsToDevice`, which only get called after megabytes of
> real startup) plus a painted window — not a pixel-sum or a fixed instruction
> count.

## 5. Brittle instruction-count assertions

A boot gate asserted `instruction_count > 3M`. Then a *fix* (the GetTickCount
clock) made SimAnt progress *faster* through its now-working busy-wait, so it
reached the render — and the next frontier — in far fewer instructions, and the
assertion failed on an improvement.

> **Rule.** Assert on *what happened* (APIs called, window painted), not on how
> many instructions it took. Instruction counts move when you make the game
> better.

## 6. Call-log name mismatch

A test checked for `GDI.56:CreateFont` in the call log, but the ordinal name
wasn't in `win16/api/ordinals.py`, so the log recorded the bare `GDI.56` and
the assertion failed even though the call happened.

> **Rule.** When you add an API handler, add its ordinal name to
> `ordinals.py` in the same slice — the call-log format (`MODULE.ord:Name`) is
> load-bearing for tests and for readable frontier reports.

## 7. Guessing an ordinal from its number

`USER.50` is *not* `SetTimer` (that's `USER.10`); `USER.45` is not what its
neighbour suggested. Guessing from ordinal proximity produced wrong labels.

> **Rule.** Identify an API from its **call site** — the pushed arguments, the
> argument values at the gap, how the return is used — not from the ordinal
> number. Cross-check the number against a reference *after* the call site tells
> you what it is. (See [`bringing_up_a_game.md`](bringing_up_a_game.md) §2.)

## 8. Treating child windows as top-level

A game whose display renders into child windows (SimAnt's canvas/ribbon inside
its `AntRoot` frame) will look "detached" — pieces in separate windows — if each
window is presented on its own.

> **Rule.** Only top-level windows are real to a host; `WS_CHILD` windows
> composite into their parent via `win16/compositor.py`. See
> [`win16_layer.md`](win16_layer.md#child-windows--windows-within-a-window).

## 9. `run(N)` counts frames, not instructions, in a message loop

Once a game is in its message loop, one outer `cpu.run` step can dispatch a
whole `WndProc` (millions of nested instructions), so `run(11_000_000)` asks for
11 million *frames*, not instructions. An early session mistook a working game
for a "pathological slowdown" because of this.

> **Rule.** In the message-loop phase, drive in small batches and gate on an
> outcome (a paint, an API call), not on a large step budget.
