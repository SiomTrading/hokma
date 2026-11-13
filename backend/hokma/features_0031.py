"""Everything new in 0.0.31 plugs into the app through this one module, so `core/app.py` only calls `attach`."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Callable

from fastapi import APIRouter, FastAPI

log = logging.getLogger("hokma.features")


def routers() -> list[APIRouter]:
    from hokma.backup.api import create_router as backup_router
    from hokma.github_connect.api import router as github_router
    from hokma.power.api import create_router as power_router
    from hokma.telegram.api import create_router as telegram_router

    return [power_router(), telegram_router(), github_router, backup_router()]


async def on_startup(app: FastAPI) -> None:
    from hokma.backup.cloud_runner import loop as backup_loop
    from hokma.engine.api import get_engine
    from hokma.power.api import watch
    from hokma.scheduler import loop
    from hokma.telegram.core.approvals_ui import _redact_telegram_error_text
    from hokma.telegram.glue import TelegramGlue

    app.state.power_task = asyncio.create_task(watch(app, app.state.power_busy))

    app.state.backup_task = asyncio.create_task(backup_loop(app))
    glue = app.state.telegram = TelegramGlue(lambda: app.state.db, get_engine)
    loop.OUTCOME_LISTENERS.append(glue.notify_job)

    async def connect() -> None:             # in the background: a slow network must not delay the start
        try:
            await glue.start()
        except Exception as exc:                 # no traceback: a network error may carry the bot token
            log.warning("telegram did not connect: %s", _redact_telegram_error_text(exc))

    app.state.telegram_task = asyncio.create_task(connect())
    app.state.telegram_outbox_task = asyncio.create_task(glue.retry_loop())


async def on_shutdown(app: FastAPI) -> None:
    from hokma.power import keepawake
    from hokma.scheduler import loop

    for name in ("power_task", "backup_task", "telegram_outbox_task"):
        task = getattr(app.state, name, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    keepawake.keep_awake().set(False)

    glue = getattr(app.state, "telegram", None)
    task = getattr(app.state, "telegram_task", None)
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if glue is not None:
        if glue.notify_job in loop.OUTCOME_LISTENERS:
            loop.OUTCOME_LISTENERS.remove(glue.notify_job)
        await glue.stop()


def attach(app: FastAPI, busy: Callable[[], bool]) -> None:
    """Add the routes and run `on_startup` / `on_shutdown` inside the app's own lifespan (after the database opens,
    before it closes)."""
    app.state.power_busy = busy
    for router in routers():
        app.include_router(router)
    inner: Any = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a: FastAPI):
        async with inner(a):
            await on_startup(a)
            try:
                yield
            finally:
                await on_shutdown(a)

    app.router.lifespan_context = lifespan
