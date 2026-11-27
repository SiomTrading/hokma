"""Hokma 1.2.10 (task 3435): a backtest asks exact futures and continuous roots in the way the market source understands, an ETF pays like a share where the
table has no rule of its own, and a search where every trial failed says why."""
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pandas as pd
import pytest
from backtest import support
from backtest.session_support import session_bars
from backtest.runner_support import NQ
from backtest.search_support import FakeLedger

from hokma.backtest import costs, data_feed, optimize, runner
from hokma.backtest.data_feed import DataFeedError
from hokma.backtest.optimize import Axis, ParamSpace
from hokma.backtest.spec import CostModel, SpecError
from hokma.broker import fees
from hokma.data import ohlcv
from hokma.data.providers import BarService, RawBars
from hokma.market import symbols as market_symbols

UTC = timezone.utc


def frame_of(day):
    return pd.DataFrame({k: [float(getattr(b, k)) for b in day] for k in ("open", "high", "low", "close", "volume")},
                        index=pd.DatetimeIndex([b.ts for b in day], name="time"))


def fetch_serving(frame, serving, asked):
    def fetcher(symbol, interval):
        asked.append(symbol)
        return RawBars(frame if symbol in serving else frame.iloc[0:0], datetime(2025, 11, 11, tzinfo=UTC))

    service = BarService({"p": fetcher})
    return lambda symbol, interval, **kw: ohlcv.bars(f"p|{symbol}", interval, service=service, **kw)


def test_an_exact_future_is_asked_as_the_key_of_the_broker_and_shares_and_etfs_as_their_ticker():
    assert data_feed.market_symbol("FUT:NQ:2026-12") == "/NQZ26" and data_feed.market_symbol("FUT:ES:2026-03") == "/ESH26"
    assert data_feed.market_symbol("EQ:AAPL") == "AAPL" and data_feed.market_symbol("ETF:QQQ") == "QQQ"
    assert data_feed.market_symbol("CONT:NQ") == "CONT:NQ" and data_feed.market_symbol("NQ=F") == "NQ=F" and data_feed.market_symbol("p|FUT:NQ:2026-12") == "p|FUT:NQ:2026-12"


def test_the_market_source_serves_an_exact_contract_only_through_schwab():
    assert market_symbols.classify("/NQZ26") == "future" and market_symbols.classify("/NQ") is None
    assert market_symbols.to_provider("schwab", "/NQZ26", "future") == "/NQZ26" and market_symbols.to_provider("schwab", "NQ=F", "future") == "/NQ"
    assert market_symbols.to_provider("alpaca", "/NQZ26", "future") is None


def test_the_default_fetch_of_an_exact_future_reaches_the_market_source_as_the_contract(monkeypatch):
    asked = []

    def fetcher(symbol, interval):
        asked.append(symbol)
        return RawBars(pd.DataFrame(columns=ohlcv.COLUMNS), datetime(2026, 11, 26, tzinfo=UTC))

    monkeypatch.setattr(ohlcv, "DEFAULT_SERVICE", BarService({ohlcv.MARKET_PROVIDER: fetcher}))
    start, end = datetime(2026, 11, 10, tzinfo=UTC), datetime(2026, 11, 25, tzinfo=UTC)
    with pytest.raises(DataFeedError) as error:
        data_feed.load("FUT:NQ:2026-12", "5m", start=start, end=end, as_of=end)
    assert asked == ["/NQZ26"] and error.value.code == "no_data"
    assert "FUT:NQ:2026-12" in str(error.value) and "/NQZ26" in str(error.value) and "5m" in str(error.value) and "hokma_market" in str(error.value)


def test_a_continuous_root_with_only_the_front_contract_runs_on_the_series_of_the_provider_with_a_notice():
    day = session_bars("day")
    frame, asked = frame_of(day), []
    fetch = fetch_serving(frame, {"FUT:NQ:2025-12", "NQ=F"}, asked)
    plan = support.spec(start=day[0].ts, end=day[-1].ts)
    data = runner.default_source(fetch=fetch)(NQ, plan)
    assert "NQ=F" in asked and len(data.bars) == len(frame)
    assert any(w.startswith("continuous_provider_series:NQ=F") and "sin ajuste de roll" in w for w in data.warnings)
    assert "continuous_adjusted:difference" not in data.warnings


def test_a_continuous_root_with_several_contracts_builds_the_chain(monkeypatch):
    day = session_bars("day")
    frame, asked, chains = frame_of(day), [], []
    fetch = fetch_serving(frame, {"FUT:NQ:2025-09", "FUT:NQ:2025-12"}, asked)
    monkeypatch.setattr(runner.continuous, "build", lambda chain, rule, adjustment: chains.append([c.contract_id for c in chain]) or SimpleNamespace(frame=frame))
    data = runner.default_source(fetch=fetch)(NQ, support.spec(start=day[0].ts, end=day[-1].ts))
    assert chains == [["FUT:NQ:2025-09", "FUT:NQ:2025-12"]] and "NQ=F" not in asked and "continuous_adjusted:difference" in data.warnings


def test_a_continuous_root_with_nothing_fails_and_says_what_was_asked():
    day = session_bars("day")
    fetch = fetch_serving(frame_of(day), set(), [])
    with pytest.raises(DataFeedError) as error:
        runner.default_source(fetch=fetch)(NQ, support.spec(start=day[0].ts, end=day[-1].ts))
    text = str(error.value)
    assert error.value.code == "no_data" and "FUT:NQ:" in text and "NQ=F" in text and "1m" in text


def test_an_etf_pays_the_equity_rule_of_a_table_that_has_no_etf_rule_and_a_table_of_futures_still_refuses_it():
    for name in fees.tables():
        table = fees.load(name)
        if "etf" not in table.rules and "equity" in table.rules:
            assert table.rule_of("etf") == table.rules["equity"], name
    assert fees.load("schwab").rules["etf"] == fees.load("schwab").rules["equity"]
    assert costs.commission_usd(costs.fee_model(CostModel("schwab"), "etf"), qty=D(100), price=D(500)) == D(0)
    assert costs.commission_usd(costs.fee_model(CostModel("ibkr_stocks"), "etf"), qty=D(100), price=D(500)) > 0
    with pytest.raises(SpecError, match="no rule for 'etf'"):
        costs.fee_model(CostModel("ibkr_futures"), "etf")


def test_a_search_where_every_trial_fails_for_one_cause_names_that_cause():
    def boom(point):
        raise SpecError("bad_spec: fee table 'x' has no rule for 'etf'")

    result = optimize.search(support.spec(strategy_id="alpha"), ParamSpace((Axis("a", "int", 1, 3),)), method="grid", n_trials=3, objective="o",
                             presweep=False, evaluate=boom, ledger=FakeLedger())
    assert result.best is None and "failed_trials:3" in result.warnings
    assert "failed_reason:SpecError: bad_spec: fee table 'x' has no rule for 'etf' (x3)" in result.warnings
    assert optimize.failure_reasons(result.trials) == [("SpecError: bad_spec: fee table 'x' has no rule for 'etf'", 3)]


def test_the_optimize_tool_answers_with_the_cause_when_every_trial_fails(monkeypatch):
    from hokma import backtest
    from hokma.kit import backtest_tools

    cause = "SpecError: bad_spec: fee table 'schwab' has no rule for 'etf'"
    trials = tuple(optimize.Trial({"a": n}, "failed", None, None, "engine", cause) for n in range(3))
    result = optimize.OptimizationResult("alpha", "alpha", "grid", "net_pnl", ParamSpace((Axis("a", "int", 1, 3),)), trials, None, 3, 0, (), (), False, 1)
    monkeypatch.setattr(backtest, "optimize", lambda *a, **k: result)
    monkeypatch.setattr(backtest_tools, "build_spec", lambda a: None)
    monkeypatch.setattr(backtest_tools, "services", lambda: None)
    with pytest.raises(backtest_tools.ToolError) as error:
        backtest_tools._optimize(space={"axes": [{"name": "a", "kind": "int", "low": 1, "high": 3, "step": 1}], "constraints": []}, method="grid",
                                 objective="net_pnl", n_trials=3)
    assert error.value.code == "bad_input" and "all 3 trials failed" in error.value.message and cause in error.value.message
