"""Write what made a conversation fail into `<data>/logs/engine.log` (task 3171).

A turn that fails used to reach the chat as a bare "something failed" and leave nothing behind. Now the code, the
message and, for a bug in Hokma, the traceback go to engine.log. Never raises: a log problem must not stop the chat."""
from __future__ import annotations

import traceback


def log(message: str, exc: BaseException | None = None, scope: str = "chat") -> None:
    try:
        from hokma.core import config
        from hokma.core.supervisor import EngineLog

        text = message
        if exc is not None:
            text += "\n" + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:]
        EngineLog(config.engine_log_path()).write(f"{scope}: " + text)
    except Exception:          # noqa: BLE001
        pass
