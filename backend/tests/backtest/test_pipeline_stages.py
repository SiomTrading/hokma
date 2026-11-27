"""One test per stage of the backtest service: data, signal, size, fill and result (SPEC-020: P-15).

Each stage is tested by itself (its success and its failure) and a failure is checked to be reported in its own stage and in no other. Every
expected value is worked out by hand from the real NQ bars of the fixtures."""
from datetime import datetime, timezone
from decimal import Decimal as D

import pandas as pd
import pytest
from backtest import support
from backtest.runner_support import NQ, Fixed, Ticks, data_of, source_of, spec
from backtest.session_support import session_bars
from backtest.support import requires_engine
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.backtest import data_feed, results, runner, trials
from hokma.backtest.data_feed import DataFeedError
from hokma.data import ohlcv
from hokma.data.providers import BarService, RawBars
from hokma.factory.orders import Size, SizeType, buy

UTC = timezone.utc


def go(bars, strategy, db, tmp_path, **over):
    kw = {k: over.pop(k) for k in ("source", "desk_limits") if k in over}
    kw.setdefault("source", source_of({NQ: bars}))
    return runner.run(spec(bars, **over), strategy=strategy, db=db, curve_dir=tmp_path, **kw)


def rows(db, sql, **params):
    with db.read() as u:
        return u.session.query(sql, params)


pytestmark = requires_engine


# ---------------------------------------------------------------------------------------------------------------- failures by stage
def failed_run(db, bars, strategy, tmp_path, source=None, **over):
    result = go(bars, strategy, db, tmp_path, source=source or source_of({NQ: bars}), **over)
    (saved,) = rows(db, "SELECT * FROM backtest_runs WHERE id = :i", i=result.run_id)
    return result, saved


class Boom(Fixed):
    def on_bar(self, ctx):
        raise ValueError("the signal blew up")


class Peeks(Fixed):
    def on_bar(self, ctx):
        return [] if ctx.data.close[ctx.index] else []                         # reads a bar that is not closed yet


class NeedsAtr(Fixed):
    def on_bar(self, ctx):
        from hokma.factory.orders import ATR

        return [buy(NQ, 1, sl=ATR(2))] if ctx.index == 2 else []             # an ATR distance with no ATR value


class Limit(Fixed):
    def on_bar(self, ctx):
        return [buy(NQ, 1, limit=25000, stop=25001)] if ctx.index == 2 else []         # a stop limit: the engine fills market and limit entries only


def raising_source(error):
    def source(instrument, spec):
        raise error
    return source


@pytest.mark.parametrize("stage, code, make", [
    ("data", "no_data", lambda bars: dict(strategy=Fixed(), source=raising_source(DataFeedError("no_data", "no bars"))) ),
    ("data", "invalid_ohlc", lambda bars: dict(strategy=Fixed(), source=raising_source(DataFeedError("invalid_ohlc", "bad bar", "2025-11-10T14:59")))),
    ("signal", "valueerror", lambda bars: dict(strategy=Boom())),
    ("signal", "look_ahead", lambda bars: dict(strategy=Peeks())),
    ("signal", "bad_strategy", lambda bars: dict(strategy=object())),
    ("size", "bad_unit", lambda bars: dict(strategy=NeedsAtr())),
    ("fill", "order_type_unsupported", lambda bars: dict(strategy=Limit())),
    ("fill", "protective_in_fast_mode", lambda bars: dict(strategy=Fixed(sl=Ticks(20), protective=False))),
])
def test_a_failure_is_reported_in_its_own_stage_and_only_there_P_15(db, tmp_path, stage, code, make):
    bars = support.bars_of("T1")
    plan = make(bars)
    result, saved = failed_run(db, bars, plan.pop("strategy"), tmp_path, **plan)
    assert result.status == "failed" and saved["status"] == "failed"
    assert f"failed_stage:{stage}" in result.warnings and f"failed_code:{code}" in result.warnings
    assert [w for w in result.warnings if w.startswith("failed_stage:") and w != f"failed_stage:{stage}"] == []
    assert saved["error"].startswith(f"{stage}:{code}:")
    assert rows(db, "SELECT * FROM backtest_trades") == [] and result.trades == ()
    assert trials.count("t") == 1 and len(trials.failed("t")) == 1           # a failed run is a trial too, with its message
    assert trials.failed("t")[0].error == saved["error"]


def test_a_failure_while_saving_is_a_failure_of_the_result_stage_P_15(db, tmp_path, monkeypatch):
    bars = support.bars_of("T1")

    def refuse(*args, **kwargs):
        raise results.ResultError("bad_run", "the disk is full")

    monkeypatch.setattr(results, "record_result", refuse)
    result, saved = failed_run(db, bars, Fixed(), tmp_path)
    assert "failed_stage:result" in result.warnings and saved["error"].startswith("result:bad_run:")


def test_the_data_stage_reads_the_bars_through_the_data_feed_and_keeps_their_hash(db, tmp_path):
    bars = support.bars_of("T1")
    frame = pd.DataFrame({k: [float(getattr(b, k)) for b in bars] for k in ("open", "high", "low", "close", "volume")},
                         index=pd.DatetimeIndex([b.ts for b in bars], name="time"))
    service = BarService({"fixture": lambda symbol, interval: RawBars(frame, datetime(2025, 11, 12, tzinfo=UTC))})
    fetch = lambda symbol, interval, **kw: ohlcv.bars(symbol, interval, service=service, **kw)
    sheet_bars = spec(bars, instruments=("fixture|NQ",))
    from hokma.backtest import runner as rr
    original = rr.sheet_of
    rr.sheet_of = lambda instrument, on: original(NQ, on)                   # the sheet of the future; the symbol is the one of the fake provider
    try:
        result = runner.run(sheet_bars, strategy=Fixed(contract="fixture|NQ"), fetch=fetch, db=db, curve_dir=tmp_path)
    finally:
        rr.sheet_of = original
    assert result.status == "done", rows(db, "SELECT error FROM backtest_runs")
    loaded = data_feed.load("fixture|NQ", "1m", start=bars[0].ts, end=bars[-1].ts, as_of=bars[-1].ts, fetch=fetch)
    assert result.realized_pnl_usd == D("100.50") and result.data_hash == loaded.data_hash and set(loaded.warnings) <= set(result.warnings)


def test_an_instrument_with_no_bars_at_the_provider_fails_in_the_data_stage(db, tmp_path):
    bars = support.bars_of("T1")
    result, saved = failed_run(db, bars, Fixed(), tmp_path, source=lambda instrument, spec: runner.InstrumentData(instrument, (), "", runner.sheet_of(NQ, bars[0].ts.date())))
    assert "failed_stage:data" in result.warnings and saved["error"].startswith("data:no_data:")



# ---------------------------------------------------------------------------------------------------------------- each stage on its own
def test_the_data_stage_gives_the_bars_as_decimals_the_sheet_and_the_hash_of_the_rows():
    frame = pd.DataFrame({"open": [25637.25, 25634.5], "high": [25647.0, 25642.0], "low": [25623.5, 25624.75], "close": [25634.75, 25636.5],
                          "volume": [2814.0, 1932.0]}, index=pd.DatetimeIndex(["2025-11-10 15:00", "2025-11-10 15:01"], tz="UTC", name="time"))
    bars = runner.bars_of(frame)
    assert [b.open for b in bars] == [D("25637.25"), D("25634.50")] and bars[0].ts == datetime(2025, 11, 10, 15, 0, tzinfo=UTC)
    sheet = runner.sheet_of("CONT:NQ", datetime(2025, 11, 10).date())
    assert (sheet.multiplier, sheet.tick_size, sheet.asset_type, sheet.root, sheet.expiration.isoformat()) == (D(20), D("0.25"), "future", "NQ", "2025-12-01")
    data = data_of(bars)
    assert data.data_hash == data_feed.data_hash(frame) and data.symbol == "CONTNQ"


def test_the_default_source_builds_the_continuous_series_through_the_data_feed_and_a_run_uses_it(db, tmp_path):
    day = session_bars("day")
    frame = pd.DataFrame({k: [float(getattr(b, k)) for b in day] for k in ("open", "high", "low", "close", "volume")},
                         index=pd.DatetimeIndex([b.ts for b in day], name="time"))
    asked = []

    def fetcher(symbol, interval):
        asked.append(symbol)
        return RawBars(frame if "2025-12" in symbol else frame.iloc[0:0], datetime(2025, 11, 11, tzinfo=UTC))      # only December 2025 has bars

    service = BarService({"p": fetcher})
    fetch = lambda symbol, interval, **kw: ohlcv.bars(f"p|{symbol}", interval, service=service, **kw)
    plan = spec(day, start=day[0].ts, end=day[-1].ts)
    data = runner.default_source(fetch=fetch)(NQ, plan)
    direct = data_feed.load("p|FUT:NQ:2025-12", "1m", start=plan.start, end=plan.end, as_of=plan.as_of, fetch=fetch)
    assert asked[:3] == ["FUT:NQ:2025-09", "FUT:NQ:2025-12", "FUT:NQ:2026-03"] and any(w.startswith("single_contract:FUT:NQ:2025-12") for w in data.warnings)      # one contract and no continuous series of the provider: no roll
    assert len(data.bars) == len(direct.frame) and data.bars[0].ts == day[0].ts
    # decided at the close of bar 2 and filled at the open of bar 3 plus a tick; closed at the open of bar 10 less a tick
    result = runner.run(plan, strategy=Fixed(enter_at=3, exit_at=10), fetch=fetch, db=db, curve_dir=tmp_path)
    entry, exit_ = data.bars[3].open + D("0.25"), data.bars[10].open - D("0.25")
    assert result.status == "done" and result.realized_pnl_usd == (exit_ - entry) * 20 - D("4.50") and result.data_hash == data.data_hash


def test_the_signal_stage_reveals_the_bars_one_by_one_and_returns_decisions_without_an_engine():
    bars = support.bars_of("T1")
    seen = []

    class Watcher(Fixed):
        def on_bar(self, ctx):
            seen.append((ctx.index, ctx.series("close").last, ctx.as_of, ctx.position))
            return super().on_bar(ctx)

    driver = runner.StrategyDriver(Watcher(), spec(bars), data_of(bars))
    decisions = [driver.decide(i, b, None) for i, b in enumerate(bars[:3])]
    assert [(i, c) for i, c, _t, _p in seen] == [(1, 25652.0), (2, 25638.25), (3, 25634.75)]
    assert [d.action for ds in decisions for d in ds] == ["enter"]                      # the one entry of the strategy, after the close of the second bar
    assert seen[1][2] == datetime(2025, 11, 10, 15, 0, tzinfo=UTC)                       # the time of the strategy is the close of the last closed bar


def test_the_size_stage_turns_an_intent_into_a_quantity_with_the_rules_of_the_factory():
    bars = support.bars_of("T1")

    class ByRisk(Fixed):
        def on_bar(self, ctx):
            return [buy(NQ, Size(SizeType.RISK_USD, D(1000)), sl=Ticks(20))] if ctx.index == 2 else []

    driver = runner.StrategyDriver(ByRisk(), spec(bars), data_of(bars))
    assert driver.decide(0, bars[0], None) == []
    (decision,) = driver.decide(1, bars[1], None)              # two bars are closed: the strategy orders
    # risk 1000 USD with a stop of 20 ticks = 5 points = 100 USD per contract: 10 contracts
    assert (decision.side, decision.qty, decision.stop_points) == ("long", D(10), D("5.00"))


def test_the_fill_stage_is_the_engine_under_the_rules_of_hokma():
    bars = support.bars_of("T2")
    driver = runner.StrategyDriver(Fixed(enter_at=2, exit_at=None, sl=Ticks(20), tp=Ticks(20), protective=True), spec(bars), data_of(bars))
    out = runner.run_with(spec(bars), driver, bars)
    assert [(f.kind, f.price) for f in out.fills] == [("entry", D("25472.00")), ("stop", D("25466.50"))] and out.ambiguous_bars == 1
    assert out.realized_pnl_usd == D("-114.50")


def test_the_result_stage_builds_trades_curve_and_statistics_from_the_output_of_the_fill_stage():
    bars = support.bars_of("T1")
    driver = runner.StrategyDriver(Fixed(), spec(bars), data_of(bars))
    out = runner.run_with(spec(bars), driver, bars)
    result = runner.build_result(7, spec(bars), driver, out, status="done")
    assert result.run_id == 7 and result.realized_pnl_usd == out.realized_pnl_usd == D("100.50")
    assert sum(t.pnl_usd for t in result.trades) == result.realized_pnl_usd and len(result.equity.timestamps) == len(bars)
    assert set(result.stats) >= {"sharpe", "psr", "drawdown"} and result.engine_version == out.engine_version


def test_a_failure_of_the_statistics_is_a_warning_and_never_a_failure_of_the_run(monkeypatch):
    bars = support.bars_of("T1")
    driver = runner.StrategyDriver(Fixed(), spec(bars), data_of(bars))
    out = runner.run_with(spec(bars), driver, bars)

    def refuse(*args, **kwargs):
        raise ValueError("the table could not be built")

    monkeypatch.setattr(results, "summary", refuse)
    result = runner.build_result(1, spec(bars), driver, out, status="done")
    assert result.stats == {} and "stats_unavailable:valueerror" in result.warnings and len(result.trades) == 1
