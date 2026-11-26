"""Shared pieces of the tests of the strategy creator (SPEC-030): a database on each engine, a sheet of the idea of the owner and its saved spec.

The `db` fixture runs every test that uses it twice: on SQLite, and on the Postgres of HOKMA_TEST_POSTGRES_URL (skipped when there is none)."""
from __future__ import annotations

import copy
from typing import Any

import pytest
from dbsupport import env  # noqa: F401  (fixture)
from pgsupport import pg_admin_url, pg_template, pg_url_migrated  # noqa: F401  (fixtures)

from hokma.db import get_database, migrate
from hokma.db.dbconfig import DatabaseConfig, save_config

IDEA = ("I buy NQ when the 9 EMA crosses above the 21 EMA on the 5 minute chart, only from 9:45 to 11:30 New York time, at most 2 trades a day, "
        "stop at 1.5 ATR and target at 2R.")
NS = {"value": "not_specified"}


@pytest.fixture(params=["sqlite", "postgres"])
def db(request, env):  # noqa: F811
    """A migrated database with the agents of the tests, on SQLite and on Postgres."""
    if request.param == "postgres":
        url = request.getfixturevalue("pg_url_migrated")
        save_config(DatabaseConfig("postgres", url))
    migrate()
    database = get_database()
    assert database.engine == request.param
    with database.write() as u:
        for slug in ("hokma", "joel", "vera", "iris"):
            u.agents.upsert_kit(slug, slug.capitalize(), None, "balanced", f"agents/{slug}.md")
            u.agents.set_status(slug, "active")
    return database


def src(pid: int, value: Any) -> dict[str, Any]:
    return {"value": value, "origin": "source", "cite": [pid]}


def proposed(value: Any, why: str = "the source does not say it, so Hokma proposes a conservative value") -> dict[str, Any]:
    return {"value": value, "origin": "proposed", "why": why}


def sheet(pid: int) -> dict[str, Any]:
    """The sheet of IDEA: complete, mapped to pieces of the factory, one field proposed (the size) and the execution proposed."""
    return {
        "schema": 1, "summary": "Buys NQ when a fast average crosses above a slow one on 5 minute bars, in the morning session only, with at most two entries a day.",
        "line": "DT", "instruments": src(pid, ["NQ"]), "timeframe": src(pid, "5m"),
        "signal_logic": {"kind": src(pid, "trend"), "rule": src(pid, "fast average crosses above the slow average"), "inputs": proposed(["close"]),
                         "lookback": src(pid, 21), "map": {"alpha": "ema_cross", "params": {"fast": 9, "slow": 21}}},
        "entry": {"trigger": src(pid, "the cross is confirmed when the bar closes"), "direction": src(pid, "long_only"),
                  "filters": src(pid, [{"kind": "session", "start": "09:45", "end": "11:30"}]), "timing": NS},
        "exit": {"stop": src(pid, "1.5 ATR"), "target": src(pid, "2R"), "time_exit": NS, "reversal": NS, "trailing": NS,
                 "map": {"kind": "atr", "length": 14, "stop_atr": 1.5, "r_multiple": 2.0}},
        "sizing": {"method": proposed("fixed risk of 0.5 percent of equity", "the idea does not size the position, so Hokma proposes a small fixed risk"),
                   "map": {"kind": "fixed_risk", "risk_pct": 0.5}},
        "risk_controls": {"max_trades_per_day": src(pid, 2), "flat_by_time": NS, "max_daily_loss": NS, "map": [{"kind": "max_trades_per_day", "n": 2}]},
        "execution": {**proposed("market", "the idea does not say how to enter, so Hokma enters at market"), "map": {"kind": "market"}}, "needs_new_rule": []}


def pending_sheet(pid: int) -> dict[str, Any]:
    """A sheet whose entry rule no alpha of the factory does: the rule is pending and there is no map for it."""
    data = copy.deepcopy(sheet(pid))
    data["signal_logic"].pop("map")
    data["signal_logic"]["rule"] = src(pid, "enter when the cumulative delta diverges from the price")
    data["needs_new_rule"] = [{"component": "signal_logic", "rule": "enter when the cumulative volume delta diverges from the price", "cite": [pid]}]
    return data


def spec_yaml(strategy_id: str = "idea_ema_morning", **over: Any) -> str:
    """The saved spec that stands for sheet(): the same alpha, parameters and parts, with its two kinds of tests."""
    from e2e_h4.support import SIGNALS, SIZES

    parts = over.get("parts") or ("parts:\n  sizer: {kind: fixed_risk, risk_pct: 0.5}\n  exit: {kind: atr, length: 14, stop_atr: 1.5, r_multiple: 2.0}\n"
                                   "  execution: {kind: market}\n  filters:\n    - {kind: session, start: '09:45', end: '11:30'}\n    - {kind: max_trades_per_day, n: 2}\n")
    return (f"strategy_id: {strategy_id}\nline: {over.get('line', 'DT')}\nalpha: {over.get('alpha', 'ema_cross')}\nparams: {over.get('params', '{fast: 9, slow: 21}')}\n"
            f"hypothesis: A fast average above a slow one means the move continues.\n{parts}tests:\n  signal:{SIGNALS}  position_size:{SIZES}")
