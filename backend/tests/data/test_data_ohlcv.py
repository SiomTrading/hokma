"""Bars from hokma.market: closed bars, as_of cut, quality, staleness, no connections of its own (SPEC-017: TA-61, TA-75, OA-10, FF-025)."""
import socket
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from hokma.data import ohlcv, sentinels
from hokma.data.providers import BarService, NoDataError, RateLimitError, RawBars
from hokma.market.errors import RATE_LIMITED, UNKNOWN_SYMBOL, MarketError

from .support import FIXTURES, FakeMarketSource, market_source, no_network, when  # noqa: F401

NOW = when("2024-04-01T12:00:00")
RAW = pd.read_csv(FIXTURES / "bars_daily.csv")


def call(source=None, **kwargs):
    defaults = dict(start=when("2024-01-01"), end=when("2024-04-01"), as_of=when("2024-03-15"), clock=lambda: NOW)
    defaults.update(kwargs)
    return ohlcv.bars("ACME", "1D", **defaults)


def test_bars_come_from_hokma_market_and_open_no_connection_TA_61(market_source, no_network) -> None:
    source = market_source(FakeMarketSource())
    result = call()
    assert result.ok and result.source == "hokma_market"
    assert source.calls == [("ACME", "1D")]              # one call to the public market API: no loop, no connection of its own
    assert no_network == []
    from hokma.data import providers

    assert "urlopen" not in ohlcv.__dict__ and "requests" not in ohlcv.__dict__    # the module has no HTTP client of its own


def test_bars_do_not_poll_in_a_loop_even_for_a_long_window_TA_61(market_source) -> None:
    source = market_source(FakeMarketSource())
    for _ in range(3):
        call(start=when("2020-01-01"))
    assert len(source.calls) == 3


def test_as_of_cut_drops_later_bars_and_the_forming_bar_TA_61(market_source) -> None:
    market_source(FakeMarketSource())
    result = call(as_of=when("2024-03-14T20:00:00"))
    assert result.value.index[-1].date().isoformat() == "2024-03-13"       # the 14th began at 14:30 and is still forming: it is not the close
    assert "partial_bar_excluded" in result.warnings
    assert (result.value.index <= when("2024-03-14T20:00:00")).all()
    later = call(as_of=when("2024-03-15T00:00:00"))
    assert later.value.index[-1].date().isoformat() == "2024-03-14"        # once its UTC day ended it is closed
    assert "partial_bar_excluded" not in later.warnings


def test_rows_with_bad_ohlc_are_excluded_never_repaired_TA_61(market_source) -> None:
    market_source(FakeMarketSource())
    result = call()
    assert result.meta["excluded_rows"] == 2 and "excluded_rows_with_invalid_ohlc:2" in result.warnings
    assert result.value["close"].notna().all() and (result.value["high"] >= result.value["low"]).all()
    expected = len(RAW) - 2
    assert len(call(as_of=when("2024-04-01T12:00:00"), end=when("2024-04-01")).value) <= expected


def test_stale_data_is_rejected_TA_75(market_source) -> None:
    market_source(FakeMarketSource())
    result = call(start=when("2024-06-01"), end=when("2024-06-30"), as_of=when("2024-06-30"), clock=lambda: when("2024-07-01"))
    assert result.status in (sentinels.STALE, sentinels.NO_DATA, sentinels.DATA_UNAVAILABLE) and result.value is None
    stale = ohlcv.bars("ACME", "1D", start=when("2024-01-01"), end=when("2024-07-01"), as_of=when("2024-07-01"), clock=lambda: when("2024-07-02"))
    assert stale.status == sentinels.STALE and stale.reason == sentinels.REASON_STALE and stale.value is None
    assert any("stale" in w for w in stale.warnings) and stale.source == "hokma_market" and stale.received_at


def test_no_data_unavailable_and_unknown_source_carry_provider_and_reason_TA_75(market_source) -> None:
    market_source(FakeMarketSource(error=MarketError(UNKNOWN_SYMBOL, 404)))
    unknown = call()
    assert (unknown.status, unknown.source, unknown.reason) == (sentinels.NO_DATA, "hokma_market", UNKNOWN_SYMBOL)
    market_source(FakeMarketSource(error=MarketError(RATE_LIMITED, 429)))
    limited = call()
    assert (limited.status, limited.reason, limited.source) == (sentinels.DATA_UNAVAILABLE, sentinels.REASON_RATE_LIMITED, "hokma_market")
    market_source(FakeMarketSource(candles=[]))
    assert call().status == sentinels.NO_DATA


def test_a_gap_is_reported_and_never_filled_TA_75(market_source) -> None:
    market_source(FakeMarketSource())
    before = call(start=when("2023-06-01"), end=when("2024-01-01"), as_of=when("2024-03-15"))
    assert before.status == sentinels.DATA_UNAVAILABLE and before.reason == sentinels.REASON_NOT_OBSERVED     # the source never observed that window
    partial = call(start=when("2023-12-01"), end=when("2024-03-01"), as_of=when("2024-03-15"))
    assert partial.ok and any(w.startswith("coverage_starts_2024-01-02") for w in partial.warnings)
    days = partial.value.index.normalize().unique()
    assert len(days) == len(partial.value)                                  # no row was invented for the missing days


def test_delayed_or_historical_data_is_never_live_OA_10(market_source) -> None:
    market_source(FakeMarketSource())
    historical = call()
    assert historical.meta["live"] is False and "not_live" in historical.warnings
    assert historical.meta["freshness"]["delay_basis"] == "historical_request"
    market_source(FakeMarketSource(stale=True))
    old_cache = call(as_of=when("2024-04-01T12:00:00"))
    assert old_cache.meta["live"] is False and "served_from_old_cache" in old_cache.warnings
    assert old_cache.meta["freshness"]["delay_status"] == "possible"


def test_result_declares_source_and_reception_time_OA_10(market_source) -> None:
    source = FakeMarketSource(fetched_at=int(when("2024-04-01T09:00:00").timestamp()))
    market_source(source)
    result = call()
    assert result.source == "hokma_market" and result.received_at == when("2024-04-01T09:00:00")      # when it was received, not published
    assert result.as_of == when("2024-03-15") and result.meta["bar_id"] == "hokma_market|ACME"


def test_bar_id_selects_one_source_and_a_failing_one_is_not_swapped_OA_10() -> None:
    frame = RAW.assign(time=pd.to_datetime(RAW["time"], unit="s", utc=True)).set_index("time")[["open", "high", "low", "close", "volume"]].dropna()
    service = BarService({"alt": lambda symbol, interval: RawBars(frame, NOW), "down": lambda s, i: (_ for _ in ()).throw(RateLimitError("503", reason="provider_unreachable"))})
    ok = ohlcv.bars("alt|ACME", "1D", start=when("2024-01-01"), end=when("2024-04-01"), as_of=when("2024-03-15"), service=service, clock=lambda: NOW)
    assert ok.ok and ok.source == "alt" and ok.meta["bar_id"] == "alt|ACME"
    down = ohlcv.bars("down|ACME", "1D", start=when("2024-01-01"), end=when("2024-04-01"), as_of=when("2024-03-15"), service=service, clock=lambda: NOW)
    assert (down.status, down.source, down.reason) == (sentinels.DATA_UNAVAILABLE, "down", "provider_unreachable")
    missing = ohlcv.bars("nobody|ACME", "1D", start=when("2024-01-01"), end=when("2024-04-01"), as_of=when("2024-03-15"), service=service, clock=lambda: NOW)
    assert missing.status == sentinels.DATA_UNAVAILABLE and missing.reason == sentinels.REASON_KEY_MISSING


def test_unsupported_interval_is_bad_input_TA_61() -> None:
    from hokma.quant.errors import BadInput

    with pytest.raises(BadInput):
        ohlcv.bars("ACME", "3m", start=when("2024-01-01"), end=when("2024-02-01"), as_of=when("2024-02-01"))
    assert ohlcv.normalize_interval("1d") == "1D" and ohlcv.normalize_interval("1h") == "1h"


def test_test_bars_from_csv_are_labelled_test_only_FF_025() -> None:
    result = ohlcv.load_test_bars(FIXTURES / "bars_daily.csv")
    assert result.ok and result.source == "test_file" and result.meta["purpose"] == "test_only" and "test_data_only" in result.warnings
    assert list(result.value.columns) == list(ohlcv.COLUMNS) and result.value.index.tz is not None
    text = "date,open,high,low,close,volume\n2024-01-02,1,2,1,2,10\n2024-01-03,2,3,2,3,10\n"
    assert len(ohlcv.load_test_bars(text).value) == 2
    assert ohlcv.load_test_bars("date,open,high,low,close,volume\n").status == sentinels.NO_DATA
    from hokma.quant.errors import BadInput

    with pytest.raises(BadInput):
        ohlcv.load_test_bars("a,b\n1,2\n")
