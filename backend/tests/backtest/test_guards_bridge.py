"""The filter before an order, run by the backtest (SPEC-020: L-48, N-20, L-50; decision 1215): the guards of H2 decide, not code of the backtest."""
import ast
import dataclasses
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest
from exec_support import NQ, SPY, db, desks_db, env, proposal, ready  # noqa: F401  (fixtures)

from hokma.backtest import guards_bridge
from hokma.backtest.guards_bridge import SimAccount, SimPosition, blocked_code, check, monitor_state
from hokma.contracts import identity
from hokma.exec import prefilter
from hokma.exec.guards import cooldown as cooldown_guard
from hokma.exec.guards.account import AccountGuard
from hokma.exec.proposal import Leg, OrderProposal

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
AAPL = "EQ:AAPL"
START = D(1_000_000)


def prop(contract=AAPL, side="buy", qty="1", effect="open", line="DT", **kw):
    return OrderProposal(1, line, "desk", (Leg(contract, side, effect, D(qty)),), kw.pop("order_type", "market"), created_by="x", **kw)


def account(equity=START, peak=START, positions=(), **kw):
    return SimAccount(cash=equity, equity=equity, peak_equity=peak, positions=tuple(positions), **kw)


def run(p, acct, limits, **kw):
    sheets = {c: identity.resolve(c) for c in (NQ, AAPL)}
    return check(p, acct, limits=limits, sheets=sheets, marks=kw.pop("marks", {NQ: D(21000), AAPL: D(200)}), now=NOW, capital=START, **kw)


# --- L-48: the drawdown of the account stops the openings, and the guard of the account is the one that decides --------------------------

def test_a_run_with_a_drawdown_limit_of_10_stops_opening_when_it_passes_10_percent(monkeypatch):
    seen_by_guard = []
    real = AccountGuard.check

    def spy(self, ctx):
        out = real(self, ctx)
        seen_by_guard.append((ctx.book.equity, out.code))
        return out

    monkeypatch.setattr(AccountGuard, "check", spy)
    acct, seen = account(), []
    for equity in (950_000, 900_000, 899_000, 920_000, 1_050_000, 944_000, 945_000):
        acct = acct.with_equity(D(equity))              # the peak only goes up: 1000000, then 1050000 from the fifth step
        result = run(prop(), acct, {"max_drawdown_pct": 10}, state=monitor_state(NOW))
        seen.append((equity, result.verdict, blocked_code(result)))
    assert seen == [(950_000, "PASS", None),                       # 5.0 percent under the peak of 1000000
                    (900_000, "PASS", None),                       # exactly 10.0: not above the limit
                    (899_000, "BLOCK", "max_drawdown_pct"),        # 10.1 percent
                    (920_000, "PASS", None),                       # 8 percent under the same peak
                    (1_050_000, "PASS", None),                     # a new peak
                    (944_000, "BLOCK", "max_drawdown_pct"),        # 106000 of 1050000 is 10.095 percent
                    (945_000, "PASS", None)]                       # 105000 of 1050000 is exactly 10
    assert [code for _equity, code in seen_by_guard if code != "ok"] == ["max_drawdown_pct", "max_drawdown_pct"]      # the guard of the account said it
    assert len(seen_by_guard) == 7


def test_the_bridge_has_no_drawdown_rule_of_its_own_and_a_closing_order_is_never_stopped():
    tree = ast.parse(Path(guards_bridge.__file__).read_text(encoding="utf-8"))
    words = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and len(n.value) < 40]
    words += [n.id for n in ast.walk(tree) if isinstance(n, ast.Name)] + [n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)]
    assert not [w for w in words if "drawdown" in w or "daily_loss" in w or "max_position" in w or "sector" in w]
    low = account(D(850_000), START, positions=(SimPosition(AAPL, D(100), multiplier=D(1), market_value=D(20_000)),))
    out = run(prop(side="sell", effect="close", qty="100"), low, {"max_drawdown_pct": 10}, state=monitor_state(NOW))
    assert out.verdict == "PASS" and out.closing and [r.code for r in out.results if r.guard == "account"] == ["closing_exempt"]
    assert blocked_code(run(prop(), low, {"max_drawdown_pct": 10}, state=monitor_state(NOW))) == "max_drawdown_pct"
    assert blocked_code(run(prop(), low, {"max_drawdown_pct": 10})) == "monitor_never_measured"      # no monitor reading, no opening: as live


def test_the_limits_of_the_desk_decide_in_the_backtest_with_the_codes_of_live():
    acct = account()
    assert blocked_code(run(prop("FUT:NQ:2026-12", qty="2"), acct, {"max_position_pct": 50})) == "max_position_pct"      # 2 x 21000 x 20 is 84 percent
    assert blocked_code(run(prop(qty="1000"), acct, {"max_symbol_pct": 10})) == "symbol_cap"                                # 200000 is 20 percent
    assert blocked_code(run(prop(qty="1000"), acct, {"max_sector_pct": 10, "sectors": {"AAPL": "technology"}})) == "sector_cap"
    assert blocked_code(run(prop(qty="1"), account(D(900_000), D(900_000)), {"capital_floor_usd": 950_000})) == "capital_floor"
    assert blocked_code(run(prop(side="sell", effect="close", qty="1", reduce_only=True), acct, {})) == "nothing_to_reduce"
    assert blocked_code(run(prop(), account(day_pnl=D(-60_000)), {"max_daily_loss_pct": 5})) == "daily_loss_pct"
    assert run(prop(), acct, {}).verdict == "PASS"


def test_a_limit_that_needs_a_fact_the_account_does_not_have_blocks_and_the_account_is_never_a_zero():
    assert blocked_code(run(prop(), account(), {"max_position_pct": 50}, marks={})) == "unpriced"
    assert blocked_code(run(prop(), account(), {"max_symbol_pct": 10}, marks={})) == "unpriced"
    unvalued = account(positions=(SimPosition(AAPL, D(10)),))
    assert unvalued.snapshot(NOW).positions_value is None and "positions" in unvalued.snapshot(NOW).missing
    assert unvalued.snapshot(NOW).open_contracts == 10


def test_a_reduce_only_order_is_cut_to_the_position_as_in_live():
    held = account(positions=(SimPosition(AAPL, D(30), multiplier=D(1), market_value=D(6000)),))
    out = run(prop(side="sell", effect="close", qty="50", reduce_only=True), held, {})
    assert out.verdict == "PASS" and out.proposal.legs[0].qty == 30 and out.clamps[0]["limit"] == "reduce_only" and out.closing


# --- N-20: the same decision as the live filter for the same proposal and state --------------------------------------------------------

def sim_account_of(ctx):
    """The state of the live account as a simulated account: the backtest is given what the live filter read, nothing else."""
    book = ctx.book
    positions = tuple(SimPosition(x.contract_id, D(str(x.qty)), None if x.multiplier is None else D(str(x.multiplier)),
                                  None if x.market_value is None else D(str(x.market_value))) for x in ctx.positions)
    return SimAccount(book.cash, book.equity, book.peak_equity, book.day_pnl, book.total_profit, positions, ctx.orders_today)


def summary(result):
    return (result.verdict, blocked_code(result), result.needs_owner, result.closing, tuple((r.guard, r.verdict, r.code) for r in result.results),
            result.clamps, result.proposal.canonical())


CASES = [
    ("no limits", {}, dict(), "PASS"),
    ("position ceiling", {"max_position_pct": 50}, dict(qty="2"), "max_position_pct"),
    ("contracts of a funded account clamp", {"funding": {"max_contracts": 3}}, dict(qty="5"), "PASS"),
    ("capital floor", {"capital_floor_usd": 2_000_000}, dict(), "capital_floor"),
    ("drawdown limit and no monitor reading", {"max_drawdown_pct": 10}, dict(), "monitor_never_measured"),
    ("symbol cap", {"max_symbol_pct": 10}, dict(contract=SPY, qty="1000"), "symbol_cap"),
    ("sector cap", {"max_sector_pct": 10, "sectors": {"SPY": "index"}}, dict(contract=SPY, qty="1000"), "sector_cap"),
    ("sector cap with no sector", {"max_sector_pct": 10}, dict(contract=SPY, qty="1"), "no_sector"),
    ("reduce only with nothing to reduce", {}, dict(side="sell", effect="close", reduce_only=True), "nothing_to_reduce"),
    ("order size against the capital", {"max_order_pct": 10}, dict(), None),
    ("guards the desk lists", {"guards": ["whitelist", "max_position"], "max_position_pct": 50}, dict(qty="2"), "max_position_pct"),
    ("cooldown", {"cooldown_seconds": 60}, dict(), "cooldown"),
]


@pytest.mark.parametrize("name,limits,kwargs,code", CASES, ids=[c[0] for c in CASES])
def test_the_bridge_and_the_live_filter_return_the_same_decision(ready, name, limits, kwargs, code):
    with ready.db.write() as u:
        u.broker.set_limits(ready.desk_id, limits)
    sheets = {c: identity.resolve(c) for c in (NQ, SPY)}
    p = proposal(ready, **kwargs)
    if name == "cooldown":
        cooldown_guard.record_sent(ready.desk_id, [NQ], at=NOW - timedelta(seconds=10), db=ready.db)
    live = prefilter.run(ready.desk_id, p, db=ready.db, now=NOW, sheets=sheets)
    ctx = prefilter.load_context(ready.desk_id, p, db=ready.db, now=NOW, sheets=sheets)
    simulated = check(p, sim_account_of(ctx), limits=ctx.limits, sheets=ctx.contracts, marks=ctx.marks, now=ctx.now, capital=ctx.capital, level=ctx.level,
                      assets=ctx.assets, windows=ctx.windows, state=ctx.state, desk_status=ctx.desk_status, orders_enabled=ctx.orders_enabled,
                      account_ok=ctx.account_ok, open_orders=ctx.open_orders)
    assert summary(simulated) == summary(live)
    if code is not None:
        assert (blocked_code(live) or live.verdict) == code
    if name == "contracts of a funded account clamp":
        assert live.clamps and live.proposal.legs[0].qty == 3
