"""The temporality is picked for each run and any time interval works (task 3427): a backtest in 2m and 7m builds its bars from the minute bars of the provider,
a temporality the data does not reach says so, and the length of one bar follows the interval."""
from datetime import datetime, timezone

import pandas as pd
import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from backtest.runner_support import D, NQ, Fixed, session_bars, spec

from hokma.backtest import data_feed, runner
from hokma.backtest.data_feed import DataFeedError
from hokma.backtest.spec import span_ns
from hokma.data import ohlcv
from hokma.data.providers import BarService, RawBars

UTC = timezone.utc


def feed_of(day):
    frame = pd.DataFrame({k: [float(getattr(b, k)) for b in day] for k in ("open", "high", "low", "close", "volume")},
                         index=pd.DatetimeIndex([b.ts for b in day], name="time"))
    asked = []

    def fetcher(symbol, interval):
        asked.append(interval)
        return RawBars(frame if "2025-12" in symbol else frame.iloc[0:0], datetime(2025, 11, 11, tzinfo=UTC))

    service = BarService({"p": fetcher})
    return frame, asked, lambda symbol, interval, **kw: ohlcv.bars(f"p|{symbol}", interval, service=service, **kw)


@pytest.mark.parametrize("bar,minutes", [("2m", 2), ("7m", 7)])
def test_a_run_in_2m_and_7m_builds_the_bars_from_the_minute_bars_and_runs(db, tmp_path, bar, minutes):
    day = session_bars("day")
    frame, asked, fetch = feed_of(day)
    plan = spec(day, start=day[0].ts, end=day[-1].ts, bar=bar)
    data = runner.default_source(fetch=fetch)(NQ, plan)
    assert asked and set(asked) <= {"1m", "5m", "15m"} and "1m" in asked or minutes % 5 == 0
    assert 0 < len(data.bars) < len(frame) and len(data.bars) >= len(frame) // minutes - 1
    direct = data_feed.load("p|FUT:NQ:2025-12", bar, start=plan.start, end=plan.end, as_of=plan.as_of, fetch=fetch)
    assert len(direct.frame) == len(data.bars) and any(w.startswith("built_from_") and w.endswith(f"_{bar}") for w in direct.warnings)
    result = runner.run(plan, strategy=Fixed(enter_at=3, exit_at=10), fetch=fetch, db=db, curve_dir=tmp_path)
    assert result.status == "done" and result.data_hash == data.data_hash
    assert span_ns(bar) == minutes * 60 * 10**9


def test_a_temporality_without_enough_data_is_a_notice_with_the_base_the_provider_and_the_bars_never_a_refusal():
    day = session_bars("day")
    short = day[:1]
    frame, _, fetch = feed_of(short)
    plan = spec(day, start=day[0].ts, end=day[-1].ts, bar="45m")
    with pytest.raises(DataFeedError) as caught:
        data_feed.load("p|FUT:NQ:2025-12", "45m", start=plan.start, end=plan.end, as_of=plan.as_of, fetch=fetch)
    assert caught.value.code == "insufficient_data" and "45m" in str(caught.value) and "p" in str(caught.value)
    with pytest.raises(runner.StageError) as stage:
        runner.load_data(plan, runner.default_source(fetch=fetch))
    assert stage.value.stage == "data" and "45m" in str(stage.value)


def test_the_span_of_a_bar_follows_its_interval():
    assert [span_ns(b) for b in ("1m", "5m", "1h", "1d", "1w", "3m", "2h")] == [n * 10**9 for n in (60, 300, 3600, 86400, 604800, 180, 7200)]
