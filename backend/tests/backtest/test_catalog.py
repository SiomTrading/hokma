"""The local catalog of loaded bars (SPEC-020: N-31): the second run reads from it, never calls the data module and has the same hash."""
from datetime import datetime, timezone

import pandas as pd
import pytest

from hokma.backtest import data_feed
from hokma.backtest.catalog import Catalog, ParquetStore
from hokma.data import ohlcv
from hokma.data.providers import BarService, RawBars

UTC = timezone.utc
START, END, AS_OF = datetime(2025, 11, 10, 14, 30, tzinfo=UTC), datetime(2025, 11, 10, 14, 50, tzinfo=UTC), datetime(2025, 11, 10, 21, 0, tzinfo=UTC)


def source():
    index = pd.DatetimeIndex([pd.Timestamp(f"2025-11-10 14:{m}", tz="UTC") for m in (30, 31, 32, 35)], name="time")
    close = pd.Series([25000.0, 25001.5, 25002.25, 25003.0], index=index)
    frame = pd.DataFrame({"open": close - 1, "high": close + 2, "low": close - 2, "close": close, "volume": [10.0, 11.0, 12.0, 13.0]})
    calls = []

    def fetcher(symbol, interval):
        calls.append(symbol)
        return RawBars(frame, datetime(2025, 11, 10, 21, 0, 5, tzinfo=UTC))

    service = BarService({"fixture": fetcher})
    return calls, lambda s, i, **kw: ohlcv.bars(s, i, service=service, **kw)


def run(fetch, catalog, symbol="fixture|NQ", **kw):
    return data_feed.load(symbol, "1m", start=kw.pop("start", START), end=END, as_of=AS_OF, fetch=fetch, catalog=catalog)


def test_the_second_run_reads_the_catalog_and_does_not_call_the_data_module(tmp_path):
    calls, fetch = source()
    catalog = Catalog(ParquetStore(tmp_path / "catalog"))
    first = run(fetch, catalog)
    second = run(fetch, catalog)
    assert calls == ["NQ"] and not first.from_catalog and second.from_catalog and second.source == "catalog"       # the counter did not move
    assert first.data_hash == second.data_hash
    pd.testing.assert_frame_equal(first.frame, second.frame, check_freq=False)
    assert len(list((tmp_path / "catalog").glob("*.parquet"))) == 1 and not list((tmp_path / "catalog").glob("*.tmp"))


def test_another_request_is_another_entry(tmp_path):
    calls, fetch = source()
    catalog = Catalog(ParquetStore(tmp_path))
    run(fetch, catalog)
    run(fetch, catalog, start=datetime(2025, 11, 10, 14, 31, tzinfo=UTC))
    assert calls == ["NQ", "NQ"]
    assert Catalog.key("a", "1m", START, END, AS_OF) != Catalog.key("a", "5m", START, END, AS_OF)
    assert Catalog.key("a", "1m", START, END, AS_OF) == Catalog.key("a", "1m", START, END, AS_OF)


def test_a_failed_load_is_not_cached(tmp_path):
    calls, fetch = source()
    catalog = Catalog(ParquetStore(tmp_path))
    with pytest.raises(data_feed.DataFeedError):
        data_feed.load("fixture|NQ", "1m", start=START, end=END, as_of=AS_OF, fetch=lambda *a, **k: (_ for _ in ()).throw(data_feed.DataFeedError("no_data", "x")), catalog=catalog)
    assert not list(tmp_path.glob("*"))


def test_the_store_takes_only_hashes_as_names(tmp_path):
    store = ParquetStore(tmp_path)
    for bad in ("../outside", "NQ", "a" * 63, "A" * 64):
        with pytest.raises(ValueError):
            store.read(bad)
    with pytest.raises(ValueError):
        store.write("../outside", pd.DataFrame())
    assert store.read("0" * 64) is None
