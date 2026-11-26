"""The rows of the engine run (SPEC-020): expiry (L-25), margin and cash account (N-08), latency (N-11), determinism (N-35), the variable tick
(N-03) and the session gate (L-26). Every expected value is worked out by hand in the comments, from the real NQ bars of the fixtures."""
from decimal import Decimal as D

import pytest
from backtest import support
from backtest.session_support import MULT, TICK, at, run
from backtest.support import requires_engine

from hokma.backtest import engine
from hokma.backtest.engine import Expiry, NonDeterministic
from hokma.backtest.fills import Decision
from hokma.backtest.spec import FillRules, SpecError
pytestmark = requires_engine


T1_BUY = {"2025-11-10T14:59:00Z": [Decision("enter", "long")]}
T1_ROUND = {**T1_BUY, "2025-11-10T15:29:00Z": [Decision("exit")]}


# ---------------------------------------------------------------------------------------------------------------- L-25
def test_a_position_open_when_the_contract_expires_is_closed_at_the_settlement_price_and_is_not_an_operation_L_25():
    # T2 bars 05:03 to 05:07. Decided after the close of 05:03, the long fills at the open of 05:04 (25473.50) plus 1 tick = 25473.75.
    # The contract expires at 05:07: the bars from 05:07 are not run, and the position closes at the settlement. The settlement used is
    # the close of 05:06, 25477.50 (the settlement columns of the daily table of Trading Core are empty, so no exchange figure is available).
    out = run("T2", {"2025-11-05T05:03:00Z": [Decision("enter", "long")]}, expiry_rule=Expiry(at("2025-11-05T05:07:00Z"), D("25477.50")))
    assert [f.kind for f in out.fills] == ["entry"] and len(out.fills) == 1          # the expiry execution is not a fill of the strategy
    assert [(s.tag, s.qty, s.price) for s in out.settlements] == [("expiry", D(-1), D("25477.50"))]
    (trade,) = out.trades
    assert trade.exit_tag == "expiry" and trade.exit_price == D("25477.50") and trade.entry_price == D("25473.75")
    # price result (25477.50 - 25473.75) * 20 = 75.00; commission only on the entry, 2.25; the settlement of the exchange is not charged.
    # The trade is net of its costs: 75.00 - 2.25 = 72.75
    assert trade.pnl_usd == D("72.75") and trade.costs_usd == D("2.25") and out.realized_pnl_usd == D("72.75")
    assert out.open_qty == 0 and out.mtm_pnl_usd == D("72.75") and out.warnings == ("closed_by_expiry",)
    assert out.equity[-1] == (at("2025-11-05T05:07:00Z"), D("1000072.75"))


def test_a_flat_account_at_expiry_has_nothing_to_settle_L_25():
    assert engine.expiry(D(0), Expiry(at("2025-11-05T05:07:00Z"), D("25477.50")), 3) is None
    assert engine.expiry(D(1), None, 3) is None
    out = run("T1", T1_ROUND, expiry_rule=Expiry(at("2025-11-10T15:32:00Z"), D("25649.00")))
    assert out.settlements == () and out.realized_pnl_usd == D("100.50")


def test_the_synthetic_close_at_the_end_of_the_data_is_a_fill_like_any_other_with_the_ticks_against_and_its_commission_L_25():
    # T1 long fills at 25637.50 (open 25637.25 plus a tick) and is never sold. There is no next open after the last bar, so close_at_end closes
    # it at the last close, 25649.00, one tick against a seller: 25648.75. (25648.75 - 25637.50) * 20 = 225.00 less the commission of both
    # sides, 2.25 + 2.25 = 4.50, is 220.50. The 0.25 of slippage on the close is 0.25 * 20 = 5.00.
    out = run("T1", T1_BUY, finalize="close_at_end")
    assert len(out.fills) == 1 and [(s.tag, s.price, s.commission) for s in out.settlements] == [("end_of_data", D("25648.75"), D("2.25"))]
    (trade,) = out.trades
    assert trade.exit_tag == "end_of_data" and trade.pnl_usd == D("220.50") and trade.costs_usd == D("4.50")
    assert out.realized_pnl_usd == D("220.50") == out.mtm_pnl_usd and out.open_qty == 0
    assert out.costs_usd["commission"] == D("4.50") and out.costs_usd["slippage"] == D("10.00")     # the entry was 0.25 * 20 = 5.00 against too
    assert out.warnings == ("closed_by_end_of_data",) and out.account.final_usd == D("1000220.50")
    # a short is closed by a buyer: one tick above the last close
    short = run("T1", {"2025-11-10T14:59:00Z": [Decision("enter", "short")]}, finalize="close_at_end")
    assert [(s.price, s.qty) for s in short.settlements] == [(D("25649.25"), D(1))]


def test_mark_to_market_leaves_the_position_open_and_marks_it_to_the_last_close_L_25():
    # The default: nothing is closed. Realized is zero (no closing fill), the open result is marked at the last close, 25649.00, with no tick:
    # (25649.00 - 25637.50) * 20 = 230.00 less the 2.25 of the entry = 227.75. The two figures differ and both are in the output.
    held = run("T1", T1_BUY)
    assert held.settlements == () and held.trades == () and held.open_qty == 1 and held.warnings == ()
    assert held.realized_pnl_usd == 0 and held.mtm_pnl_usd == D("227.75") and held.realized_pnl_usd != held.mtm_pnl_usd
    assert held.equity[-1][1] == D(1_000_000) + D("227.75") and held.costs_usd["commission"] == D("2.25")


def test_the_engine_closes_a_position_a_year_after_the_last_bar_and_the_run_drops_that_close_L_25():
    # The library expires the instrument 365 days after the last bar and closes what is open at that moment. That close is a hidden liquidation,
    # not a decision of the strategy: the raw outcome has it, the run does not.
    from hokma.backtest import _lib

    bars = support.bars_of("T1")
    plan = {bars[0].ts: [Decision("enter", "long")]}
    decide = lambda i, bar, side: plan.get(bar.ts, ())                       # noqa: E731 - a one line plan
    raw = _lib.run_bars(bars=bars, instrument=_lib.InstrumentSpec("NQZ5", TICK, MULT, price_precision=2), decide=decide, rules=support.spec().fill,
                        starting_balance=D(1_000_000), per_contract_fee=None)
    last_close_ns = int(bars[-1].ts.timestamp()) * 10**9 + 60 * 10**9
    assert len(raw.fills) == 2 and raw.fills[1].ts_ns >= last_close_ns + 300 * 86_400 * 10**9      # the extra close, about a year later
    kept = engine.run(support.spec(), bars, decide, tick_size=TICK, multiplier=MULT, symbol="NQZ5", protective_orders=False)
    assert len(kept.fills) == 1 and kept.open_qty == 1 and kept.trades == ()


# ---------------------------------------------------------------------------------------------------------------- N-08
def test_a_futures_run_is_a_margin_account_that_adds_up_N_08():
    out = run("T1", T1_ROUND)
    assert engine.account_kind("future") == "margin" and out.account.kind == "margin"
    # entry 25637.50, exit 25642.75: (5.25 * 20) = 105.00 gross; commission 2 * 2.25 = 4.50; 1,000,000 + 105.00 - 4.50 = 1,000,100.50
    acc = out.account
    assert (acc.capital_usd, acc.gross_pnl_usd, acc.costs_usd, acc.final_usd) == (D(1_000_000), D("105.00"), D("4.50"), D("1000100.50"))
    assert acc.final_usd == acc.capital_usd + acc.gross_pnl_usd - acc.costs_usd == out.equity[-1][1] == D(1_000_000) + out.realized_pnl_usd
    assert acc.min_cash_usd is None


def test_shares_are_a_cash_account_that_pays_in_full_and_adds_up_N_08():
    # The index prints of T1 stand in for the price of a share (tick 0.01, multiplier 1, no commission at the broker for shares).
    # 10 shares: bought at the open 25637.25 plus a tick of 0.01 = 25637.26; sold at the open 25643.00 less 0.01 = 25642.99.
    out = run("T1", {"2025-11-10T14:59:00Z": [Decision("enter", "long", D(10))], "2025-11-10T15:29:00Z": [Decision("exit")]},
              asset_type="equity", tick=D("0.01"), multiplier=D(1), costs=support.CostModel("schwab", slippage_ticks=1))
    assert engine.account_kind("equity") == engine.account_kind("etf") == "cash"
    acc = out.account
    assert acc.kind == "cash" and acc.costs_usd == 0
    assert out.trades[0].entry_price == D("25637.26") and out.trades[0].exit_price == D("25642.99")
    assert acc.gross_pnl_usd == D("57.30") == (D("25642.99") - D("25637.26")) * 10          # 5.73 * 10
    assert acc.final_usd == D(1_000_000) + D("57.30") == out.equity[-1][1]
    assert acc.min_cash_usd == D(1_000_000) - D("256372.60")                              # 10 * 25637.26 paid in full


def test_a_cash_account_does_not_sell_short_and_does_not_buy_more_than_its_cash_N_08():
    shares = dict(asset_type="equity", tick=D("0.01"), multiplier=D(1), costs=support.CostModel("schwab", slippage_ticks=1))
    # 100 shares at 25637.25 are 2,563,725 and the capital is 1,000,000: the order is refused, nothing fills
    big = run("T1", {"2025-11-10T14:59:00Z": [Decision("enter", "long", D(100))]}, **shares)
    assert big.fills == () and big.rejected == {"reject_prob": 0, "cash_account": 1}
    short = run("T1", {"2025-11-10T14:59:00Z": [Decision("enter", "short", D(10))]}, **shares)
    assert short.fills == () and short.rejected["cash_account"] == 1
    # the same short on a margin account (a future) is allowed
    assert len(run("T1", {"2025-11-10T14:59:00Z": [Decision("enter", "short")]}).fills) == 1


# ---------------------------------------------------------------------------------------------------------------- N-11
def test_the_latency_gives_the_next_price_of_the_ticks_after_the_arrival_N_11():
    bar = support.bars_of("T1")[2]                                 # 15:00: open 25637.25, high 25647.00, low 25623.50
    assert engine.latency(bar, "buy", 0).kind == "open" and engine.latency(bar, "buy", 0).price == D("25637.25")
    # 200 ms after the open tick: the next tick of the bar. A buy meets the high first, a sell the low first
    buy, sell = engine.latency(bar, "buy", 200_000_000), engine.latency(bar, "sell", 200_000_000)
    assert (buy.kind, buy.price) == ("high", D("25647.00")) and (sell.kind, sell.price) == ("low", D("25623.50"))
    # an arrival after the last tick of the bar meets the close
    assert engine.latency(bar, "buy", 120 * 10**9).kind == "close"
    with pytest.raises(SpecError):
        engine.latency(bar, "buy", -1)


def test_an_order_sent_at_the_open_tick_with_200_ms_of_latency_fills_at_the_next_tick_N_11():
    # entry: 15:00 high 25647.00 plus 1 tick = 25647.25. exit: 15:30 (open 25643.00, low 25637.75) a sell meets the low, less 1 tick = 25637.50.
    out = run("T1", T1_ROUND, latency_ms=200)
    assert [(t.entry_price, t.exit_price) for t in out.trades] == [(D("25647.25"), D("25637.50"))]
    # (25637.50 - 25647.25) * 20 = -195.00 gross, less 4.50 of commission; the slippage against the open is what the latency cost
    assert out.realized_pnl_usd == D("-199.50")
    plain = run("T1", T1_ROUND)
    assert plain.realized_pnl_usd == D("100.50") and [(t.entry_price, t.exit_price) for t in plain.trades] == [(D("25637.50"), D("25642.75"))]
    assert [f.ts_ns for f in out.fills] == [f.ts_ns for f in plain.fills]               # same orders, same times: only the price moves
    with pytest.raises(SpecError):
        run("T1", T1_ROUND, latency_ms=-5)


# ---------------------------------------------------------------------------------------------------------------- N-35
def test_two_runs_of_the_same_spec_are_identical_byte_for_byte_N_35():
    bars = support.bars_of("T1")
    maker = lambda: support.plan(bars, T1_ROUND)
    first = engine.deterministic(support.spec(), bars, maker, tick_size=TICK, multiplier=MULT, symbol="NQZ5", protective_orders=False)
    again = engine.run(support.spec(), bars, maker(), tick_size=TICK, multiplier=MULT, symbol="NQZ5", protective_orders=False)
    assert engine.fingerprint(first) == engine.fingerprint(again) and first == again
    assert first.realized_pnl_usd == D("100.50")


def test_a_run_that_differs_between_two_copies_is_refused_N_35():
    bars = support.bars_of("T1")
    calls = iter([T1_ROUND, {}])
    maker = lambda: support.plan(bars, next(calls))
    with pytest.raises(NonDeterministic):
        engine.deterministic(support.spec(), bars, maker, tick_size=TICK, multiplier=MULT, symbol="NQZ5", protective_orders=False)
    with pytest.raises(SpecError):
        engine.deterministic(support.spec(), bars, lambda: support.plan(bars, T1_ROUND), repeats=1, tick_size=TICK, multiplier=MULT,
                             symbol="NQZ5", protective_orders=False)


def test_the_seed_changes_only_what_is_random_N_35():
    outs = {seed: run("T1", T1_ROUND, seed=seed) for seed in (1, 2, 3)}
    assert len({engine.fingerprint(o) for o in outs.values()}) == 1                    # the one tick model draws nothing: the seed changes nothing
    refusing = {seed: run("T1", T1_ROUND, seed=seed, fill=FillRules(reject_prob=0.5)) for seed in range(8)}
    assert len({o.rejected["reject_prob"] for o in refusing.values()}) > 1             # the draw of the broker rejections does depend on the seed
    same = run("T1", T1_ROUND, seed=3, fill=FillRules(reject_prob=0.5))
    assert engine.fingerprint(same) == engine.fingerprint(refusing[3])                 # and a seed always draws the same


# ---------------------------------------------------------------------------------------------------------------- N-03
def test_the_backtest_snaps_an_option_fill_to_the_grid_of_its_price_level_N_03():
    # 25637.25 is not an option price: this checks the grid only. A buy rounds up and a sell down to 0.05 above 3 USD.
    assert engine._on_grid("option", "buy", D("3.41"), D("0.01")) == D("3.45")
    assert engine._on_grid("option", "sell", D("3.44"), D("0.01")) == D("3.40")
    assert engine._on_grid("option", "buy", D("2.504"), D("0.01")) == D("2.51")
    assert engine._on_grid("future", "buy", D("25637.30"), D("0.25")) == D("25637.30")       # a future is left as the engine priced it
