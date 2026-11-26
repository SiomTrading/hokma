"""Loading of bars for a backtest (SPEC-020: B-16, N-39). The bars come through `hokma.data`; the controls are the ones of `hokma.data.quality`."""
import ast
import math
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from hokma.backtest import data_feed
from hokma.backtest.data_feed import DataFeedError
from hokma.data import ohlcv, providers, quality
from hokma.data.providers import BarService, RateLimitError, RawBars

UTC = timezone.utc
START, END, AS_OF = datetime(2025, 11, 10, 14, 30, tzinfo=UTC), datetime(2025, 11, 10, 14, 50, tzinfo=UTC), datetime(2025, 11, 10, 21, 0, tzinfo=UTC)
MINUTES = ["14:30", "14:31", "14:32", "14:33", "14:34", "14:37", "14:38"]      # 14:35 and 14:36 had no trade


def bars_frame(minutes=MINUTES):
    index = pd.DatetimeIndex([pd.Timestamp(f"2025-11-10 {m}", tz="UTC") for m in minutes], name="time")
    close = pd.Series([25000.0 + i for i in range(len(minutes))], index=index)
    return pd.DataFrame({"open": close - 1, "high": close + 2, "low": close - 2, "close": close, "volume": 10.0})


def fetch_with(frame, calls=None):
    def fetcher(symbol, interval):
        if calls is not None:
            calls.append((symbol, interval))
        return RawBars(frame, datetime(2025, 11, 10, 21, 0, 5, tzinfo=UTC))

    service = BarService({"fixture": fetcher})
    return lambda symbol, interval, **kw: ohlcv.bars(symbol, interval, service=service, **kw)


def load(frame, **kw):
    return data_feed.load("fixture|NQ", "1m", start=START, end=END, as_of=AS_OF, fetch=fetch_with(frame), **kw)


def test_good_bars_load_and_minutes_without_trades_stay_missing():
    loaded = load(bars_frame())
    assert len(loaded.frame) == 7 and list(loaded.frame.index.strftime("%H:%M")) == MINUTES
    assert not any(loaded.frame.index == pd.Timestamp("2025-11-10 14:35", tz="UTC"))        # nothing was filled
    assert loaded.source == "fixture" and loaded.bar == "1m" and not loaded.from_catalog
    assert len(loaded.data_hash) == 64 and loaded.data_hash == data_feed.data_hash(loaded.frame)


def test_the_same_bars_give_the_same_hash_and_other_bars_do_not():
    assert load(bars_frame()).data_hash == load(bars_frame()).data_hash
    other = bars_frame()
    other.loc[other.index[0], "close"] += 0.25
    assert load(other).data_hash != load(bars_frame()).data_hash


def test_a_high_below_the_low_fails_with_the_reason_and_the_date():
    bad = bars_frame()
    bad.loc[bad.index[3], ["high", "low"]] = [24990.0, 25010.0]
    with pytest.raises(DataFeedError) as error:
        load(bad)
    assert error.value.code == "invalid_ohlc" and error.value.at.startswith("2025-11-10T14:33") and "high" in str(error.value)


def test_a_missing_price_fails_with_the_date():
    bad = bars_frame()
    bad.loc[bad.index[5], "close"] = math.nan
    with pytest.raises(DataFeedError) as error:
        load(bad)
    assert error.value.code == "invalid_ohlc" and error.value.at.startswith("2025-11-10T14:37")


def test_every_bar_invalid_fails_too():
    bad = bars_frame()
    bad["high"] = bad["low"] - 1
    with pytest.raises(DataFeedError) as error:
        load(bad)
    assert error.value.code == "invalid_ohlc"


def test_a_negative_volume_fails_with_the_date():
    bad = bars_frame()
    bad.loc[bad.index[1], "volume"] = -5.0
    with pytest.raises(DataFeedError) as error:
        load(bad)
    assert error.value.code == "negative_volume" and error.value.at.startswith("2025-11-10T14:31")


def test_the_quality_controls_run_in_the_data_module_and_the_feed_has_none_of_its_own(monkeypatch):
    seen = []
    original = quality.drop_invalid_bars

    def spy(frame):
        seen.append(len(frame))
        return original(frame)

    monkeypatch.setattr(quality, "drop_invalid_bars", spy)
    load(bars_frame())
    assert seen == [7]                                              # the controls of hokma.data.quality ran on the 7 rows
    source = Path(data_feed.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    comparisons = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Compare)]
    assert not [c for c in comparisons if "'high'" in c or "'low'" in c or "'open'" in c or "'close'" in c]
    assert "hokma.data" in source and "isnan" not in source and "isfinite" not in source


def test_the_feed_has_no_retries_of_its_own_and_the_data_module_owns_them():
    calls = []

    def throttled(symbol, interval):
        calls.append(1)
        raise RateLimitError("slow down")

    service = BarService({"fixture": throttled})
    with pytest.raises(DataFeedError) as error:
        data_feed.load("fixture|NQ", "1m", start=START, end=END, as_of=AS_OF, fetch=lambda s, i, **kw: ohlcv.bars(s, i, service=service, **kw))
    assert error.value.code == "data_unavailable" and calls == [1]            # one call: the feed never loops
    source = Path(data_feed.__file__).read_text(encoding="utf-8")
    assert "sleep" not in source and "retry" not in source and "while " not in source
    sleeps, attempts = [], []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise RateLimitError("slow down")
        return "ok"

    assert providers.retry_with_backoff(flaky, base_delay=1.0, sleep=sleeps.append) == "ok" and sleeps == [1.0, 2.0]   # growing wait


def test_no_data_an_unsupported_bar_and_a_missing_column_are_failures_with_a_code():
    with pytest.raises(DataFeedError) as error:
        load(bars_frame([]))
    assert error.value.code == "no_data"
    with pytest.raises(DataFeedError) as error:
        data_feed.load("fixture|NQ", "1w", start=START, end=END, as_of=AS_OF, fetch=fetch_with(bars_frame()))
    assert error.value.code == "unsupported_bar"
    with pytest.raises(DataFeedError):
        data_feed.load("fixture|NQ", "2m", start=START, end=END, as_of=AS_OF)


def test_a_request_above_the_cell_limit_is_refused(monkeypatch):
    from hokma.quant.errors import BadInput

    monkeypatch.setattr(data_feed.limits, "MAX_INPUT_CELLS", 10)
    with pytest.raises(BadInput):
        load(bars_frame())


def test_a_share_or_etf_canonical_id_is_asked_to_the_market_source_as_its_ticker():
    assert data_feed.market_symbol("ETF:SPY") == "SPY" and data_feed.market_symbol("EQ:AAPL") == "AAPL"
    assert data_feed.market_symbol("FUT:NQ:2026-12") == "FUT:NQ:2026-12" and data_feed.market_symbol("CONT:NQ") == "CONT:NQ"
    assert data_feed.market_symbol("ETF:") == "ETF:" and data_feed.market_symbol("SPY") == "SPY"
