"""SOUND.DRV — the Windows 3.0 voice/note API.

Two layers:
  * the deterministic event log (`services["sound_log"]`, virtual-clock
    stamped) — the authoritative, replayable model; and
  * an optional host audio backend (`services["sound_backend"]`, e.g.
    win16.audio.SquareWaveBackend) that turns queued notes into sound.

SetVoiceNote/Accent semantics (note value, note length, tempo) are decoded
here, in the game-agnostic SOUND layer; the backend only plays (freq, ms)
tones, so any synthesis backend can drive it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .core import ApiRegistry, CallContext
from .system import Win16System

# note 1 == C3; each step is a semitone.  The base octave is a presentation
# choice (the relative melody is exact either way); C3 keeps notes 1..15 in a
# clear, audible mid-range.
NOTE_BASE_HZ = 130.8128
DEFAULT_TEMPO = 120


def note_to_freq(note: int) -> float:
    if note <= 0:
        return 0.0                      # rest
    return NOTE_BASE_HZ * (2.0 ** ((note - 1) / 12.0))


def note_to_ms(length: int, tempo: int) -> float:
    """SOUND.DRV note length (1=whole, 4=quarter, 8=eighth, ...) at `tempo`
    quarter-notes-per-minute -> milliseconds."""
    if length <= 0:
        return 0.0
    tempo = tempo or DEFAULT_TEMPO
    quarter_ms = 60_000.0 / tempo
    return (4.0 / length) * quarter_ms


@dataclass
class _Voice:
    tempo: int = DEFAULT_TEMPO
    volume: int = 255
    queue: list[tuple[float, float]] = field(default_factory=list)  # (freq, ms)


@dataclass
class SoundState:
    voices: dict[int, _Voice] = field(default_factory=dict)

    def voice(self, n: int) -> _Voice:
        return self.voices.setdefault(n, _Voice())


def _log(ctx: CallContext, event: str, *args: int) -> None:
    sys: Win16System = ctx.registry.services["system"]
    ctx.registry.services.setdefault("sound_log", []).append(
        (sys.clock_ms, event, args))


def _state(ctx: CallContext) -> SoundState:
    return ctx.registry.services.setdefault("sound_state", SoundState())


def _backend(ctx: CallContext):
    return ctx.registry.services.get("sound_backend")


def install(api: ApiRegistry) -> None:
    @api.register("SOUND", 1)                           # OpenSound()
    def OpenSound(ctx: CallContext) -> int:
        _log(ctx, "open")
        _state(ctx).voices.clear()
        return 1                    # one voice available (PC-speaker model)

    @api.register("SOUND", 2)                           # CloseSound()
    def CloseSound(ctx: CallContext) -> int:
        _log(ctx, "close")
        backend = _backend(ctx)
        if backend is not None:
            backend.stop()
        return 0

    @api.register("SOUND", 3, args="word word")         # SetVoiceQueueSize(v, n)
    def SetVoiceQueueSize(ctx: CallContext) -> int:
        _log(ctx, "queue_size", *ctx.args)
        return 0

    @api.register("SOUND", 4, args="word word word word")
    def SetVoiceNote(ctx: CallContext) -> int:          # (voice, note, len, cdots)
        _log(ctx, "note", *ctx.args)
        voice, note, length, _cdots = ctx.args
        v = _state(ctx).voice(voice)
        v.queue.append((note_to_freq(note), note_to_ms(length, v.tempo)))
        return 0

    @api.register("SOUND", 5, args="word word word word word")
    def SetVoiceAccent(ctx: CallContext) -> int:        # (voice, tempo, vol, mode, pitch)
        _log(ctx, "accent", *ctx.args)
        voice, tempo, volume, _mode, _pitch = ctx.args
        v = _state(ctx).voice(voice)
        v.tempo, v.volume = tempo or DEFAULT_TEMPO, volume
        return 0

    @api.register("SOUND", 9)                           # StartSound()
    def StartSound(ctx: CallContext) -> int:
        _log(ctx, "start")
        state = _state(ctx)
        backend = _backend(ctx)
        # One voice in this game; mixing multiple voices is a later refinement.
        for v in state.voices.values():
            if v.queue and backend is not None:
                backend.play_sequence(list(v.queue))
            v.queue.clear()
        return 0

    @api.register("SOUND", 10)                          # StopSound()
    def StopSound(ctx: CallContext) -> int:
        _log(ctx, "stop")
        for v in _state(ctx).voices.values():
            v.queue.clear()
        backend = _backend(ctx)
        if backend is not None:
            backend.stop()
        return 0
