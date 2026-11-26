"""The tick scheme of a contract at a price level (SPEC-020: N-03). Every expected value is worked out by hand in the comments."""
from decimal import Decimal as D

import pytest

from hokma.broker import sim_engine
from hokma.broker.sim_engine import Quote
from hokma.contracts.ticks import tick_scheme
from hokma.exec.money import round_to_tick


def test_an_option_ticks_by_a_cent_below_three_dollars_and_by_five_cents_from_three():
    assert tick_scheme("option", D("2.50")) == D("0.01")
    assert tick_scheme("option", D("2.99")) == D("0.01")
    assert tick_scheme("option", D("3.00")) == D("0.05")           # 3 USD is already on the five cent grid
    assert tick_scheme("option", D("3.40")) == D("0.05")


def test_a_future_or_a_share_keeps_the_tick_of_its_sheet_whatever_the_price():
    assert tick_scheme("future", D("25000.25"), D("0.25")) == D("0.25")
    assert tick_scheme("future", D("1.10"), D("0.25")) == D("0.25")
    assert tick_scheme("equity", D("450.00"), D("0.01")) == D("0.01")
    assert tick_scheme("equity", D("450.00")) is None             # no tick in the sheet: no grid


def test_a_bad_option_price_is_refused():
    with pytest.raises(ValueError):
        tick_scheme("option", D("-1"))
    with pytest.raises(ValueError):
        tick_scheme("option", D("NaN"))


def test_the_prices_of_an_option_round_to_the_grid_of_their_level():
    assert round_to_tick(D("2.504"), tick_scheme("option", D("2.504")), direction="nearest") == D("2.50")      # 250.4 steps of 0.01 -> 250
    assert round_to_tick(D("3.42"), tick_scheme("option", D("3.42")), direction="nearest") == D("3.40")        # 68.4 steps of 0.05 -> 68
    assert round_to_tick(D("3.43"), tick_scheme("option", D("3.43")), direction="nearest") == D("3.45")        # 68.6 steps -> 69


def _leg(asset: str, side: str, tick: str) -> dict:
    return {"contract_id": f"X:{asset}", "side": side, "qty": "1", "multiplier": "100" if asset == "option" else "20", "tick_size": tick,
            "asset_type": asset}


def _fill(asset: str, side: str, tick: str, bid: str, ask: str, bps: float) -> D:
    quote = Quote(D(bid), D(ask), None, 0.0)
    return sim_engine.fill_leg(_leg(asset, side, tick), order_type="market", limit=None, stop=None, quote=quote, slippage_bps=bps,
                               commission_cfg={"type": "per_share", "value": 0}).price


def test_the_simulated_broker_fills_an_option_on_the_grid_of_its_price_level():
    # buy at the ask 2.50 plus 1% = 2.525: 252.5 steps of 0.01 round up to 253 -> 2.53
    assert _fill("option", "buy", "0.01", "2.40", "2.50", 100) == D("2.53")
    # buy at the ask 3.40 plus 1% = 3.434: 68.68 steps of 0.05 round up to 69 -> 3.45
    assert _fill("option", "buy", "0.01", "3.30", "3.40", 100) == D("3.45")
    # sell at the bid 3.40 less 1% = 3.366: 67.32 steps of 0.05 round down to 67 -> 3.35
    assert _fill("option", "sell", "0.01", "3.40", "3.50", 100) == D("3.35")


def test_the_simulated_broker_still_uses_the_sheet_tick_for_a_future():
    # buy at the ask 25000.25 plus 1 bp = 25002.75025: 100011.001 steps of 0.25 round up to 100012 -> 25003.00
    assert _fill("future", "buy", "0.25", "25000.00", "25000.25", 1) == D("25003.00")


def test_the_backtest_and_the_simulated_broker_read_the_same_function():
    from hokma.backtest import engine

    assert engine.tick_scheme is tick_scheme and sim_engine.tick_scheme is tick_scheme
