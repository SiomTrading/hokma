"""The emergency switch (SPEC-018: ECT-026). It runs as a task of the scheduler, in the process of the scheduler and not in the one that operates:
`run_once` reads each desk with a `kill` rule in its limits, measures the drawdown and the day loss from the book, and at the level `halt` puts the
desk in `paused` (the rules of SPEC-015 then refuse every order of that desk and the guards block the openings) and tells the owner. Thresholds
in USD for funded accounts come from `funding` (`daily_loss_usd`, `trailing_drawdown_usd`). It only ever tightens: it never un-pauses a desk."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from decimal import Decimal
from typing import Any

from hokma.db import Database, get_database
from hokma.exec import monitor

log = logging.getLogger("hokma.exec")
INTERVAL_SECONDS = 60.0


def thresholds_of(limits: dict[str, Any]) -> dict[str, float]:
    """The thresholds of a desk from its limits: `max_drawdown_pct` is the halt level (warn at 60 percent of it, reduce at 80), and the funded
    account USD caps become USD levels."""
    out: dict[str, float] = {}
    if limits.get("max_drawdown_pct") is not None:
        halt = float(limits["max_drawdown_pct"])
        out.update(halt_drawdown_pct=halt, reduce_drawdown_pct=halt * 0.8, warn_drawdown_pct=halt * 0.6)
    funding = limits.get("funding") or {}
    if funding.get("daily_loss_usd") is not None:
        out["halt_day_loss_usd"] = float(funding["daily_loss_usd"])
    if funding.get("trailing_drawdown_usd") is not None:          # in USD it stands alone: no percent threshold is needed
        out["halt_drawdown_usd"] = float(funding["trailing_drawdown_usd"])
    return out


def check_desk(desk_id: int, *, db: Database | None = None) -> monitor.Level:
    from hokma.backoffice import snapshot as snapshot_module
    from hokma.broker import service

    database = db or get_database()
    with database.read() as u:
        desk = u.desks.find(desk_id)
        account = u.broker.account_of_desk(desk_id)
        limits = u.broker.get_limits(desk_id)
    if desk is None or account is None or desk.status != "active":
        return monitor.Level("ok", "")
    thresholds = thresholds_of(limits)
    if not thresholds:
        return monitor.Level("ok", "")
    from hokma.backoffice import book as book_module
    from hokma.exec import prefilter

    open_ids = list(book_module.Book.load(account.id, db=database).cost_positions())
    marks = prefilter._mark_source(open_ids) if (prefilter._mark_source is not None and open_ids) else {}
    snap = snapshot_module.snapshot(account.id, db=database, prices=marks)
    drawdown = drawdown_usd = None
    if snap.equity is not None and snap.peak_equity:
        drop = snap.peak_equity - snap.equity
        drawdown, drawdown_usd = float(drop / snap.peak_equity * 100), float(drop)
    day_loss = None if snap.day_pnl is None else float(max(Decimal(0), -snap.day_pnl))
    level = monitor.kill_level(drawdown_pct=drawdown, day_loss_usd=day_loss, thresholds=thresholds, drawdown_usd=drawdown_usd)
    drawdown_armed = any(k in thresholds for k in ("halt_drawdown_pct", "halt_drawdown_usd"))
    unpriced_open = any(key.startswith("unpriced:") for key in snap.missing)      # an open position with no price leaves its loss out of the day
    day_armed = "halt_day_loss_usd" in thresholds
    measured = ((drawdown is not None) if drawdown_armed else True) and ((day_loss is not None and not unpriced_open) if day_armed else True) \
        and (drawdown_armed or day_armed)                                           # EVERY armed threshold was compared with a real value
    return dataclasses.replace(level, measured=measured)


def trip(desk_id: int, reason: str, *, db: Database | None = None) -> None:
    """Pause the desk and tell the owner. Idempotent: a desk that is already paused is left alone."""
    from hokma.desks import service as desks
    from hokma.exec import telegram_approval

    database = db or get_database()
    with database.read() as u:
        desk = u.desks.find(desk_id)
    if desk is None or desk.status != "active":
        return
    desks.update_desk(desk_id, {"status": "paused"}, db=database)
    with database.write() as u:
        telegram_approval.notice(u, f"Emergency switch: desk {desk.name} was paused. {reason}")


HEARTBEAT = "killswitch"


def beat(desk_id: int, *, db: Database | None = None) -> None:
    """The monitor read this desk just now. The guard `monitor` of the filter reads it: a monitor that stopped is a desk that does not open."""
    import json

    from hokma.db.timeutil import utcnow

    stamp = json.dumps({"at": utcnow()})
    with (db or get_database()).write() as u:
        key = {"desk_id": desk_id, "guard": HEARTBEAT, "key": "heartbeat"}
        if u.session.one("SELECT 1 AS x FROM exec_guard_state WHERE desk_id = :desk_id AND guard = :guard AND key = :key", key) is None:
            u.session.insert_row("exec_guard_state", {**key, "state_json": stamp, "updated_at": utcnow()})
        else:
            u.session.update("exec_guard_state", key, {"state_json": stamp, "updated_at": utcnow()})


def load_heartbeat(desk_id: int, *, db: Database | None = None) -> dict[str, Any] | None:
    import json

    with (db or get_database()).read() as u:
        row = u.session.one("SELECT state_json FROM exec_guard_state WHERE desk_id = :d AND guard = :g AND key = 'heartbeat'", {"d": desk_id, "g": HEARTBEAT})
    return json.loads(row["state_json"]) if row else None


def run_once(db: Database | None = None) -> dict[int, str]:
    """One pass over every active desk. Returns the level of each desk that is not ok. One desk that fails never stops the others."""
    database = db or get_database()
    with database.read() as u:
        ids = [d.id for d in u.desks.list() if d.status == "active"]
    out: dict[int, str] = {}
    for desk_id in ids:
        try:
            level = check_desk(desk_id, db=database)
            if level.name != "ok":
                out[desk_id] = level.name
            if level.name == "halt":
                trip(desk_id, level.reason, db=database)
            if level.measured:                        # a pass that compared nothing against a real value is not a heartbeat
                beat(desk_id, db=database)
        except Exception:
            log.exception("the emergency switch failed on desk %s", desk_id)
    return out


class KillSwitchTask:
    """The periodic task that the scheduler starts and stops with itself. A pass that fails is logged and the loop goes on; if the task itself
    dies, a callback starts it again and tells the owner. While it is down the heartbeat goes stale and the filter blocks new openings."""

    def __init__(self, db: Database | None = None, interval: float = INTERVAL_SECONDS) -> None:
        self._db, self.interval = db, interval
        self._task: asyncio.Task | None = None
        self._stopping = False
        self.restarts = 0

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(run_once, self._db)
            except Exception:
                log.exception("a pass of the emergency switch failed")
            await asyncio.sleep(self.interval)

    def _died(self, task: asyncio.Task) -> None:
        if self._stopping or task.cancelled():
            return
        log.error("the emergency switch task died: %r", task.exception())
        self.restarts += 1
        try:
            from hokma.exec import telegram_approval

            with (self._db or get_database()).write() as u:
                telegram_approval.notice(u, "The emergency switch stopped and was restarted. New openings are blocked until it reads the desks again.")
        except Exception:
            log.warning("the owner could not be told that the emergency switch restarted")
        self._task = asyncio.get_running_loop().create_task(self._loop(), name="hokma-killswitch")
        self._task.add_done_callback(self._died)

    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._loop(), name="hokma-killswitch")
            self._task.add_done_callback(self._died)

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            self._task = None
