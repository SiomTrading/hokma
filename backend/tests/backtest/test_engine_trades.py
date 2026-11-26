"""One netting of fills (SPEC-020: B-14, L-36): the engine builds its trades with `results.group_trades`, the same function the runner uses.

The oracle below is the first in, first out netting that the engine had of its own before (kept here as the reference of what it gave): on real
NQ bars with entries that add, partial closes and a reversal, the trades, the profit and loss and the open quantity are the ones it gave."""
from decimal import Decimal as D

import pytest
from backtest import support
from backtest.runner_support import Fixed, source_of, spec
from backtest.session_support import MULT, TICK, run, session_bars
from backtest.support import requires_engine
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.backtest import engine, results, runner
from hokma.backtest.fills import Decision

pytestmark = requires_engine


def reference_netting(out: engine.RunOutput, multiplier: D) -> tuple[list[tuple], D, D]:
    """The old netting of the engine: closed pairs (side, qty, entry price, exit price, price result, commission of the pair), the signed
    open quantity and the profit and loss net of commission."""
    execs = sorted([(p.fill.ts_ns, p.fill.side, p.fill.qty, p.price, p.commission) for p in out.priced]
                   + [(engine._ns(s.ts), "buy" if s.qty > 0 else "sell", abs(s.qty), s.price, s.commission) for s in out.settlements], key=lambda e: e[0])
    lots: list[list] = []                                  # [side, qty left, qty at entry, price, commission]
    pairs: list[tuple] = []
    for _ts, side_name, qty, price, commission in execs:
        side = "long" if side_name == "buy" else "short"
        left = qty
        while left > 0 and lots and lots[0][0] != side:
            lot = lots[0]
            take = min(left, lot[1])
            move = price - lot[3] if lot[0] == "long" else lot[3] - price
            pairs.append((lot[0], take, lot[3], price, move * take * multiplier, lot[4] * take / lot[2] + commission * take / qty))
            lot[1] -= take
            left -= take
            if lot[1] == 0:
                lots.pop(0)
        if left > 0:
            lots.append([side, left, left, price, commission * left / qty])
    open_qty = sum((l[1] if l[0] == "long" else -l[1] for l in lots), D(0))
    return pairs, open_qty, sum((p[4] - p[5] for p in pairs), D(0))


def at_close(minute: str) -> str:
    return f"2025-11-10T{minute}:00Z"


PLANS = {
    "one round trip": {at_close("14:59"): [Decision("enter", "long")], at_close("15:29"): [Decision("exit")]},
    "an entry that adds, then one exit": {at_close("14:59"): [Decision("enter", "long")], at_close("15:09"): [Decision("enter", "long")],
                                          at_close("15:39"): [Decision("exit")]},
    "a reversal that nets through zero": {at_close("14:59"): [Decision("enter", "long")], at_close("15:19"): [Decision("enter", "short", D(2))],
                                          at_close("15:49"): [Decision("exit")]},
    "a partial close and a position left open": {at_close("14:59"): [Decision("enter", "long", D(3))], at_close("15:19"): [Decision("enter", "short")]},
    "a short, a long and a flat account": {at_close("14:59"): [Decision("enter", "short")], at_close("15:19"): [Decision("exit")],
                                            at_close("15:29"): [Decision("enter", "long")], at_close("15:59"): [Decision("exit")]},
}


@pytest.mark.parametrize("name", PLANS)
def test_the_trades_of_the_engine_are_the_ones_of_its_old_netting_B_14(name):
    out = run("T1", PLANS[name], bars=session_bars("day"))
    pairs, open_qty, realized = reference_netting(out, MULT)
    assert out.open_qty == open_qty and out.realized_pnl_usd == realized and out.fills
    closed_qty = sum((p[1] for p in pairs), D(0))
    # the closed positions hold the same quantity, the same price result and the same commission as the pairs of the old netting
    positions = [t for t in out.trades if t.exit_at is not None]
    if open_qty == 0:
        assert sum((t.qty for t in positions), D(0)) == closed_qty
        assert sum((t.pnl_usd + t.costs_usd for t in positions), D(0)) == sum((p[4] for p in pairs), D(0))
        assert sum((t.costs_usd for t in positions), D(0)) == sum((p[5] for p in pairs), D(0))
        assert sum((t.pnl_usd for t in positions), D(0)) == out.realized_pnl_usd
    if name == "one round trip":
        ((side, qty, entry, exit_, gross, fee),) = pairs
        (trade,) = positions
        assert (trade.side, trade.qty, trade.entry_price, trade.exit_price) == (side, qty, entry, exit_)
        assert trade.pnl_usd == gross - fee and trade.costs_usd == fee and trade.exit_tag == "exit" and trade.entry_tag is None


def test_the_engine_nets_with_group_trades_and_has_no_netting_of_its_own_B_14(monkeypatch):
    assert not hasattr(engine, "_trades")
    calls = []
    real = results.group_trades
    monkeypatch.setattr(engine, "group_trades", lambda *a, **k: calls.append(1) or real(*a, **k))
    out = run("T1", PLANS["one round trip"], bars=session_bars("day"))
    assert len(calls) == 1 and len(out.trades) == 1


def test_the_runner_and_the_engine_give_the_same_closed_trades_B_14(db, tmp_path):
    bars = session_bars("day")
    one = spec(bars)
    saved = runner.run(one, strategy=Fixed(enter_at=10, exit_at=30), source=source_of({"CONT:NQ": bars}), db=db, curve_dir=tmp_path)
    plan = {bars[9].ts.strftime("%Y-%m-%dT%H:%M:%SZ"): [Decision("enter", "long")], bars[29].ts.strftime("%Y-%m-%dT%H:%M:%SZ"): [Decision("exit")]}
    out = engine.run(one, bars, support.plan(bars, plan), tick_size=TICK, multiplier=MULT, symbol="NQ", protective_orders=False)
    (a,), (b,) = saved.trades, out.trades
    assert (a.side, a.qty, a.entry_price, a.exit_price, a.pnl_usd, a.costs_usd) == (b.side, b.qty, b.entry_price, b.exit_price, b.pnl_usd, b.costs_usd)
    assert saved.realized_pnl_usd == out.realized_pnl_usd
