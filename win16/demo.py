"""Deterministic demos: record and replay the game's full input timeline.

The Win16 analogue of dos_re's input demos.  The frame boundary is
GetMessage: a demo records, in order, (a) every message GetMessage returned
("m" records — WM_TIMER/WM_PAINT/input, with their virtual-clock stamps) and
(b) every event a modal dialog loop consumed ("d" records).  Replaying feeds
exactly that stream back, so a replay is bit-identical to the recorded run —
including through dialogs.

Divergence detection is structural and loud: if the machine asks for a
message when the demo says a dialog event comes next (or vice versa, or the
dialog name differs), the replay raises DemoDivergence immediately.

Format: JSON lines.  Header, then one record per line:
    {"kind": "win16-demo", "version": 1, "exe": "PYTHON.EXE"}
    {"t": "m", "v": [hwnd, msg, wparam, lparam, time, pt]}
    {"t": "d", "dlg": "myd_high_scores", "v": ["command", 1, 0]}
    {"t": "quit"}          (the recorded session ended in WM_QUIT/None)
"""
from __future__ import annotations

import json
from pathlib import Path


class DemoDivergence(RuntimeError):
    """Replay and machine disagreed about what happens next."""


class DemoEnded(RuntimeError):
    """The demo ran out of records while the machine wanted more input."""


class DemoRecorder:
    def __init__(self, path: str | Path, exe_name: str) -> None:
        self.path = Path(path)
        self._fh = open(self.path, "w", encoding="ascii")
        self._fh.write(json.dumps(
            {"kind": "win16-demo", "version": 1, "exe": exe_name}) + "\n")
        self.records = 0

    def message(self, msg) -> None:
        """Tap for every GetMessage return (None = WM_QUIT)."""
        if msg is None:
            self._fh.write('{"t": "quit"}\n')
        else:
            self._fh.write(json.dumps({"t": "m", "v": list(msg)}) + "\n")
        self._fh.flush()
        self.records += 1

    def dialog_event(self, dlg_name: str, event) -> None:
        self._fh.write(json.dumps(
            {"t": "d", "dlg": dlg_name, "v": list(event)}) + "\n")
        self._fh.flush()
        self.records += 1

    def close(self) -> None:
        self._fh.close()


class DemoPlayer:
    """Serves the recorded stream back.  Install as
    `system.message_source = player.next_message` and
    `services["demo_player"] = player` (the dialog engine consults it)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        lines = self.path.read_text(encoding="ascii").splitlines()
        header = json.loads(lines[0])
        if header.get("kind") != "win16-demo":
            raise ValueError(f"{path}: not a win16 demo")
        self.exe = header.get("exe")
        self.records = [json.loads(line) for line in lines[1:] if line.strip()]
        self.pos = 0

    @property
    def exhausted(self) -> bool:
        return self.pos >= len(self.records)

    def _peek(self) -> dict:
        if self.exhausted:
            raise DemoEnded(
                f"demo exhausted after {self.pos} records — machine wanted more input")
        return self.records[self.pos]

    def next_message(self, sysobj):
        rec = self._peek()
        if rec["t"] == "quit":
            self.pos += 1
            return None
        if rec["t"] != "m":
            raise DemoDivergence(
                f"record {self.pos}: machine called GetMessage but the demo "
                f"has {rec['t']!r} ({rec.get('dlg', '')}) next")
        self.pos += 1
        msg = tuple(rec["v"])
        sysobj.clock_ms = max(sysobj.clock_ms, msg[4])
        return msg

    def next_dialog_event(self, dlg_name: str):
        rec = self._peek()
        if rec["t"] != "d" or rec["dlg"] != dlg_name:
            raise DemoDivergence(
                f"record {self.pos}: dialog {dlg_name!r} wanted an event but "
                f"the demo has {rec!r} next")
        self.pos += 1
        return tuple(rec["v"])
