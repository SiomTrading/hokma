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
    from hokma.mail.api import router as mail_router
    from hokma.power.api import create_router as power_router
    from hokma.telegram.api import create_router as telegram_router

    return [power_router(), telegram_router(), github_router, mail_router, backup_router()]


async def on_startup(app: FastAPI) -> None:
    from hokma.backup.cloud_runner import loop as backup_loop
    from hokma.engine.api import get_engine
    from hokma.power.api import watch
    from hokma.scheduler import loop
    from hokma.telegram.glue import TelegramGlue

    app.state.power_task = asyncio.create_task(watch(app, app.state.power_busy))

    from hokma.exec import notices

    loop_now = asyncio.get_running_loop()
    engine_now = get_engine()
    notices.set_publisher(lambda event: asyncio.run_coroutine_threadsafe(engine_now.broadcast_all(event), loop_now) and None)
    try:
        await asyncio.to_thread(notices.rearm_all, app.state.db)          # a restart keeps the wait of the notices that were pending
    except Exception:                                                  # the app starts with the database down (it says why); the next start re-arms them
        log.exception("could not re-arm the order notices that were waiting")

    # Analysis of an asset (SPEC-028): runs a previous process left open are closed, and the requests of the chat tools are picked up in the tick
    # the scheduler already has, so the cycle runs here, in the process that pushes the progress to the panels.
    from hokma.desks.swo import analysis

    try:
        analysis.recover_orphans(app.state.db)
    except Exception:
        log.exception("could not close the analyses a previous process left open")
    loop.TICK_HOOKS.append(analysis.drain_requests)

    app.state.backup_task = asyncio.create_task(backup_loop(app))
    from hokma.exec import live

    from hokma.broker import sim_engine
    from hokma.exec import killswitch, prefilter

    prefilter.set_mark_source(sim_engine.mark_source)          # the filter reads the last quote of a contract from the board (the side of a stop at market, the marks of an order)
    app.state.killswitch = killswitch.KillSwitchTask(app.state.db)          # second line: it closes the day by itself and leaves the monitor's actions alone (idempotent)
    app.state.killswitch.start()
    app.state.live_freeze_task = asyncio.create_task(live.keep_measuring(app))          # SPEC-026: the monitor of the loss of the day, on the data of the broker
    from hokma.broker import pnl

    app.state.broker_pnl_task = asyncio.create_task(pnl.keep_reading(app))          # SPEC-026 v2, 3390e1: the P&L the brokers report, every 5 s
    glue = app.state.telegram = TelegramGlue(lambda: app.state.db, get_engine)
    loop.OUTCOME_LISTENERS.append(glue.notify_job)

    # in the background (a slow network must not delay the start); it keeps retrying if Telegram is unreachable
    app.state.telegram_task = asyncio.create_task(glue.keep_connected())
    app.state.telegram_outbox_task = asyncio.create_task(glue.retry_loop())


async def on_shutdown(app: FastAPI) -> None:
    from hokma.power import keepawake
    from hokma.scheduler import loop

    from hokma.exec import live

    live.stop()
    task_ks = getattr(app.state, "killswitch", None)
    if task_ks is not None:
        await task_ks.stop()
    for name in ("power_task", "backup_task", "telegram_outbox_task", "live_freeze_task", "broker_pnl_task"):
        task = getattr(app.state, name, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    keepawake.keep_awake().set(False)
    from hokma.exec import notices

    notices.set_publisher(None)

    from hokma.desks.swo import analysis

    if analysis.drain_requests in loop.TICK_HOOKS:
        loop.TICK_HOOKS.remove(analysis.drain_requests)
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
    from hokma.validation import attach as attach_validation

    attach_validation(app)       # 422 answers in the panel's language, same envelope as the routes' own errors
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
