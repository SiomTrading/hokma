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


def thresholds_of(limits: dict[str, Any], equity: Decimal | None = None) -> dict[str, float]:
    """The thresholds of a desk from its limits: `max_drawdown_pct` is the halt level (warn at 60 percent of it, reduce at 80), and the funded
    account USD caps become USD levels. A scale of steps (SPEC-026, A8) is worked out from `equity`, as the account guard does; with no equity
    it gives the smallest step."""
    from hokma.exec import thresholds as scale

    base = equity if equity is not None and equity > 0 else None
    out: dict[str, float] = {}
    if limits.get("max_drawdown_pct") is not None:
        halt = float(limits["max_drawdown_pct"])
        out.update(halt_drawdown_pct=halt, reduce_drawdown_pct=halt * 0.8, warn_drawdown_pct=halt * 0.6)
    funding = limits.get("funding") or {}
    loss = scale.effective_usd(funding.get("daily_loss_usd"), limits.get("daily_loss_steps"), base)
    if loss is not None:
        out["halt_day_loss_usd"] = float(loss)
    trailing = scale.effective_usd(funding.get("trailing_drawdown_usd"), limits.get("drawdown_steps"), base)
    if trailing is not None:          # in USD it stands alone: no percent threshold is needed
        out["halt_drawdown_usd"] = float(trailing)
    return out


_RANK = {"ok": 0, "warn": 1, "reduce": 2, "halt": 3}


def check_desk(desk_id: int, *, db: Database | None = None) -> monitor.Level:
    """The worst level among the accounts of the desk, measured with the thresholds of the desk. A desk with no account is `ok` (it
    sends nothing, so there is nothing to stop). The measure is the whole account's: the share of each desk of a shared account is SPEC-026."""
    return check_desk_accounts(desk_id, db=db)[1]


def check_desk_accounts(desk_id: int, *, db: Database | None = None) -> tuple[int | None, monitor.Level]:
    """(the account that gave the worst level, that level). `measured` is true only when EVERY account of the desk was measured."""
    database = db or get_database()
    with database.read() as u:
        desk = u.desks.find(desk_id)
        accounts = u.broker.accounts_of_desk(desk_id)
        limits = u.broker.get_limits(desk_id)
    if desk is None or not accounts or desk.status != "active":
        return None, monitor.Level("ok", "")
    if not thresholds_of(limits):
        return None, monitor.Level("ok", "")
    worst: tuple[int | None, monitor.Level] = (None, monitor.Level("ok", ""))
    measured_all = True
    for account in accounts:
        level = _check_account(desk_id, account, limits, database)
        measured_all = measured_all and level.measured
        if worst[0] is None or _RANK.get(level.name, 0) > _RANK.get(worst[1].name, 0):
            worst = (account.id, level)
    return worst[0], dataclasses.replace(worst[1], measured=measured_all)


def _check_account(desk_id: int, account: Any, limits: dict[str, Any], database: Database) -> monitor.Level:
    """The BACKUP of the live freeze (SPEC-026 v2): the loss of the day and the drawdown are the ones of hokma.exec.live, worked out from what the BROKER
    reports (its equity of the start of the session and its equity now). Without data from the broker nothing is measured and the level stays unmeasured."""
    from hokma.exec import live

    m = live.measure(account.id, db=database)
    thresholds = thresholds_of(limits, m.equity_start if m.equity_start is not None else m.equity_live)
    peak = live.peak_of(m)
    drawdown = drawdown_usd = None
    if m.drawdown is not None and peak:
        drawdown, drawdown_usd = float(m.drawdown / peak * 100), float(m.drawdown)
    day_loss = None if m.loss is None else float(max(Decimal(0), m.loss))
    level = monitor.kill_level(drawdown_pct=drawdown, day_loss_usd=day_loss, thresholds=thresholds, drawdown_usd=drawdown_usd)
    drawdown_armed = any(k in thresholds for k in ("halt_drawdown_pct", "halt_drawdown_usd"))
    day_armed = "halt_day_loss_usd" in thresholds
    measured = ((drawdown is not None) if drawdown_armed else True) and ((day_loss is not None) if day_armed else True) \
        and (drawdown_armed or day_armed)                                           # EVERY armed threshold was compared with a real value
    return dataclasses.replace(level, measured=measured)


def trip(desk_id: int, reason: str, *, db: Database | None = None, account_id: int | None = None) -> None:
    """Tell the owner and stop. With `account_id` the stop is the ACCOUNT's (SPEC-026, 3390c): the account is closed for the day, which stops EVERY desk
    that uses it and leaves the desks active (the other accounts of those desks go on). It is idempotent and never touches the positions: closing
    them is the `on_trip` of the rule, run by hokma.exec.live. Without an account (a desk that has none) the desk is paused, as before."""
    from hokma.broker import day_close
    from hokma.desks import service as desks
    from hokma.exec import telegram_approval

    database = db or get_database()
    if account_id is not None:
        with database.read() as u:
            already = day_close.is_closed(u, account_id)
        if not already:
            day_close.close_day(account_id, "killswitch", None, "block_entries", database)
            with database.write() as u:
                telegram_approval.notice(u, f"Emergency switch: account {account_id} was closed for the day. {reason}")
        return
    targets = [desk_id]
    if account_id is not None:
        with database.read() as u:
            targets += [d for d in u.broker.desks_of_account(account_id) if d != desk_id]
    for target in targets:
        with database.read() as u:
            desk = u.desks.find(target)
        if desk is None or desk.status != "active":
            continue
        desks.update_desk(target, {"status": "paused"}, db=database)
        with database.write() as u:
            suffix = reason if target == desk_id else f"{reason} (the account is shared with desk {_desk_name(database, desk_id)})"
            telegram_approval.notice(u, f"Emergency switch: desk {desk.name} was paused. {suffix}")


def _desk_name(database: Database, desk_id: int) -> str:
    with database.read() as u:
        desk = u.desks.find(desk_id)
    return desk.name if desk is not None else str(desk_id)


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
            account_id, level = check_desk_accounts(desk_id, db=database)
            if level.name != "ok":
                out[desk_id] = level.name
            if level.name == "halt":
                trip(desk_id, level.reason, db=database, account_id=account_id)
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
