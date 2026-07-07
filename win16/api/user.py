"""USER services — windowing, messages, dialogs. Implemented per observed call."""
from __future__ import annotations

from .core import ApiRegistry, CallContext


def install(api: ApiRegistry) -> None:
    @api.register("USER", 5, args="word")               # InitApp(hInstance)
    def InitApp(ctx: CallContext) -> int:
        # Creates the task's message queue in real USER; queue state lives in
        # the Python system object here.
        return 1
