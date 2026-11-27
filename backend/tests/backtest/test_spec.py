"""BacktestSpec rules (SPEC-020: B-10, B-15, B-34, V-07, V-10, N-19, P-14)."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from hokma.backtest.spec import (BacktestSpec, CostModel, FillRules, ForbiddenFill, SpecError, round_qty,
                                 sequence_same_bar, validate_bar)
from hokma.contracts.identity import ContractSheet

T0, T1 = datetime(2025, 1, 2, tzinfo=timezone.utc), datetime(2025, 2, 3, tzinfo=timezone.utc)


def make(line="DT", bar="1m", **extra) -> BacktestSpec:
    fields = dict(strategy_id="s", strategy_version=1, params={}, line=line, instruments=("FUT:NQ:2025-03",), bar=bar,
                  start=T0, end=T1, as_of=T1, capital_usd=Decimal("50000"), costs=CostModel("ibkr_futures"))
    return BacktestSpec(**{**fields, **extra})


def sheet(asset_type="future") -> ContractSheet:
    return ContractSheet("X:1", asset_type, "NQ", None, Decimal("20"), Decimal("0.25"), Decimal("5"), None, None, None, "USD", "t", T0)


def test_any_time_interval_is_a_valid_bar_on_any_line_and_only_a_non_interval_fails():   # the line decides the session, never the bar
    for line in ("DT", "SWC", "SWO", "LP", "RF"):
        for bar in ("1m", "2m", "7m", "45m", "4h", "1d", "1w"):
            assert make(line, bar).bar == bar
    validate_bar("1d", "DT")
    for bad in ("", "x", "1M", "0m", "r:10t", "10r"):
        with pytest.raises(SpecError):
            validate_bar(bad, "DT")
    with pytest.raises(SpecError):
        validate_bar("1m", "XX")
    from hokma.backtest.spec import bar_seconds
    assert (bar_seconds("7m"), bar_seconds("1h"), bar_seconds("1d"), bar_seconds("1w")) == (420, 3600, 86400, 604800)


def test_hedging_is_only_for_swc_and_netting_is_the_default():                           # B-10, N-19
    assert make("SWC", "1d").oms == "netting"
    assert make("SWC", "1d", oms="hedging").oms == "hedging"
    with pytest.raises(SpecError):
        make("DT", "1m", oms="hedging")


def test_fractional_size_is_only_for_lp():                                              # B-34
    assert make("LP", "1d", qty_mode="fractional").qty_mode == "fractional"
    for line, bar in (("DT", "1m"), ("SWC", "1d")):
        with pytest.raises(SpecError):
            make(line, bar, qty_mode="fractional")


def test_filling_at_the_close_of_the_signal_bar_is_forbidden():                         # B-15
    with pytest.raises(ForbiddenFill) as caught:
        FillRules(trade_on_close=True)
    assert caught.value.code == "forbidden_fill"
    for bad in (dict(stop_first=False), dict(gap_fills_at_open=False), dict(next_open_ticks=0), dict(model="best_price")):
        with pytest.raises(ForbiddenFill):
            FillRules(**bad)
    assert make().finalize == "mark_to_market"


def test_the_fingerprint_is_stable_and_changes_with_any_field():
    a, b = make(), make()
    assert a.fingerprint() == b.fingerprint() and len(a.fingerprint()) > 20
    assert replace(a, seed=1).fingerprint() != a.fingerprint()
    assert a.canonical()["capital_usd"] == "50000" and a.canonical()["start"].startswith("2025-01-02")


def test_the_spec_checks_its_dates():
    with pytest.raises(SpecError):
        make(start=T1, end=T0)
    with pytest.raises(SpecError):
        BacktestSpec("s", 1, {}, "DT", ("FUT:NQ:2025-03",), "1m", T0, T1, T0, Decimal("1"), CostModel("x"))


def test_sizes_round_down_to_the_lot_and_the_remainder_is_a_warning():                  # V-07
    assert round_qty(Decimal("2.7"), sheet("future")) == (Decimal("2"), ("size_rounded_down:2.7->2",))
    assert round_qty(Decimal("3"), sheet("future")) == (Decimal("3"), ())
    assert round_qty(Decimal("0.4"), sheet("future"))[0] == Decimal("0")
    assert round_qty(Decimal("37"), sheet("equity"), lot=Decimal("10")) == (Decimal("30"), ("size_rounded_down:37->30",))
    assert round_qty(Decimal("0.37"), sheet("equity"), mode="fractional") == (Decimal("0.37"), ())
    with pytest.raises(SpecError):
        round_qty(Decimal("-1"), sheet())


class Intent:
    def __init__(self, side):
        self.side = side


def test_an_exit_and_an_entry_on_the_same_bar_close_first_then_open():                  # V-10
    ordered, warnings = sequence_same_bar(["exit"], [Intent("buy")], holding_side="buy")
    assert ordered[0] == "exit" and ordered[1].side == "buy" and warnings == ()


def test_with_accumulation_off_a_second_entry_on_the_same_side_is_ignored():            # V-10
    ordered, warnings = sequence_same_bar([], [Intent("buy")], holding_side="buy")
    assert ordered == [] and warnings == ("entry_ignored_no_accumulation",)
    ordered, warnings = sequence_same_bar([], [Intent("buy"), Intent("buy")], holding_side=None)
    assert len(ordered) == 1 and warnings == ("entry_ignored_no_accumulation",)
    ordered, _ = sequence_same_bar([], [Intent("buy")], holding_side="buy", accumulate=True)
    assert len(ordered) == 1


def test_the_slippage_ticks_of_the_costs_must_match_the_ticks_against_of_the_fill_rules():
    with pytest.raises(SpecError):
        make(costs=CostModel("ibkr_futures", slippage_ticks=2))
    assert make(costs=CostModel("ibkr_futures", slippage_ticks=2), fill=FillRules(next_open_ticks=2)).fill.next_open_ticks == 2
