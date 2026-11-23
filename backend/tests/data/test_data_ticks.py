"""Tick cleaning, bars, direction, storage (SPEC-017: ECT-013)."""
import numpy as np
import pandas as pd
import pytest

from hokma.data import sentinels, ticks as tk
from hokma.quant.errors import BadInput

from .support import FIXTURES, no_network, when  # noqa: F401


def load(name: str) -> pd.DataFrame:
    frame = pd.read_csv(FIXTURES / name, index_col="time")
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index, utc=True, format="ISO8601"), name="time")
    return frame


def small(prices, sizes=None, start="2024-03-28 23:00:00") -> pd.DataFrame:
    index = pd.date_range(start, periods=len(prices), freq="250ms", tz="UTC")
    return pd.DataFrame({"price": prices, "size": sizes if sizes is not None else [1.0] * len(prices)}, index=index)


# ------------------------------------------------------------------------------------------------ cleaning
def test_cleaning_removes_each_known_problem_and_counts_it_ECT_013() -> None:
    ticks = load("ticks.csv")
    clean, report = tk.clean_ticks(ticks)
    assert report.received == len(ticks) == 242
    assert (report.non_positive, report.duplicates) == (2, 2)                       # one zero size, one negative price; two repeated rows
    assert report.outliers >= 1 and report.excluded_condition == 0 and report.outside_session == 0
    assert clean["price"].max() < 18000 + 100 and (clean["price"] > 0).all() and (clean["size"] > 0).all()   # the 400 point print is gone
    assert report.kept == len(clean)


def test_conditions_and_session_are_explicit_and_default_to_none_ECT_013() -> None:
    ticks = load("ticks.csv")
    with_condition, report = tk.clean_ticks(ticks, excluded_conditions={"X"})
    assert report.excluded_condition == 1 and "X" not in set(with_condition["condition"].fillna(""))
    nothing_dropped_by_hours, plain = tk.clean_ticks(ticks)
    assert plain.outside_session == 0                                               # no equity hours imposed on a market that trades at night
    session, report = tk.clean_ticks(ticks, session=("23:00", "23:00:20"))
    assert report.outside_session > 0 and session.index.max() <= pd.Timestamp("2024-03-28 23:00:20", tz="UTC")


def test_exact_duplicates_only_not_every_repeated_timestamp_ECT_013() -> None:
    index = pd.DatetimeIndex(["2024-03-28 23:00:00"] * 3, tz="UTC")
    frame = pd.DataFrame({"price": [100.0, 100.25, 100.25], "size": [1.0, 2.0, 2.0]}, index=index)
    clean, report = tk.clean_ticks(frame)
    assert report.duplicates == 1 and list(clean["price"]) == [100.0, 100.25]       # two real trades share a timestamp and both stay


def test_outlier_filter_is_trailing_and_centered_needs_offline_ECT_013() -> None:
    prices = [100.0 + 0.25 * (i % 4) for i in range(80)]
    prices[60] = 140.0
    frame = small(prices)
    clean, report = tk.clean_ticks(frame)
    assert report.outliers == 1 and 140.0 not in set(clean["price"])
    early = [100.0 + 0.25 * (i % 4) for i in range(80)]
    early[1] = 140.0                                                                  # no history yet to judge it by: kept, not guessed
    kept, early_report = tk.clean_ticks(small(early))
    assert early_report.outliers == 0 and 140.0 in set(kept["price"])
    with pytest.raises(BadInput):
        tk.clean_ticks(frame, centered=True)                                          # uses future ticks: live use is refused
    _, centered = tk.clean_ticks(frame, centered=True, offline=True)
    assert centered.outliers >= 1
    with pytest.raises(BadInput):
        tk.clean_ticks(pd.DataFrame({"price": [1.0]}))


# ------------------------------------------------------------------------------------------------ bars
def test_volume_tick_and_dollar_bars_hand_values_ECT_013() -> None:
    frame = small([10.0, 11.0, 9.0, 12.0, 10.0, 10.0, 13.0], sizes=[4.0, 6.0, 5.0, 5.0, 2.0, 8.0, 1.0])
    volume = tk.volume_bars(frame, 10)
    assert list(volume["volume"]) == [10.0, 10.0, 10.0, 1.0] and list(volume["open"]) == [10.0, 9.0, 10.0, 13.0]
    assert list(volume["high"]) == [11.0, 12.0, 10.0, 13.0] and list(volume["low"]) == [10.0, 9.0, 10.0, 13.0] and list(volume["close"]) == [11.0, 12.0, 10.0, 13.0]
    assert list(volume["complete"]) == [True, True, True, False]                      # only the last bar can still be forming
    ticks = tk.tick_bars(frame, 3)
    assert list(ticks["n_ticks"]) == [3, 3, 1] and list(ticks["complete"]) == [True, True, False] and ticks.index[1] == frame.index[3]
    dollars = tk.dollar_bars(frame, 100.0)
    assert dollars["dollar_volume"].iloc[0] == 40.0 + 66.0 and dollars["n_ticks"].iloc[0] == 2          # 10*4 + 11*6 crosses 100 on the second trade
    assert dollars["dollar_volume"].sum() == pytest.approx(float((frame["price"] * frame["size"]).sum())) and bool(dollars["complete"].iloc[:-1].all())
    for bad in (lambda: tk.volume_bars(frame, 0), lambda: tk.tick_bars(frame, 0), lambda: tk.dollar_bars(frame, -1)):
        with pytest.raises(BadInput):
            bad()


def test_time_bars_skip_empty_intervals_and_mark_the_forming_bar_ECT_013() -> None:
    index = pd.DatetimeIndex(["2024-03-28 23:00:05", "2024-03-28 23:00:40", "2024-03-28 23:02:10"], tz="UTC")     # the 23:01 minute has no ticks
    frame = pd.DataFrame({"price": [100.0, 101.0, 99.0], "size": [1.0, 2.0, 3.0]}, index=index)
    bars = tk.time_bars(frame, "1min")
    assert list(bars.index.minute) == [0, 2] and list(bars["open"]) == [100.0, 99.0] and list(bars["high"]) == [101.0, 99.0]    # no bar was invented for 23:01
    assert list(bars["volume"]) == [3.0, 3.0] and list(bars["complete"]) == [True, False]
    closed = tk.time_bars(frame, "1min", as_of=when("2024-03-28T23:05:00"))
    assert list(closed["complete"]) == [True, True]
    assert tk.time_bars(frame.iloc[0:0], "1min").empty


def test_imbalance_bars_close_on_one_sided_flow_ECT_013() -> None:
    frame = small([100 + 0.25 * i for i in range(60)])                                # every trade up: pure buying
    bars = tk.imbalance_bars(frame, kind="tick", initial_ticks=10, initial_imbalance=0.5)
    assert len(bars) >= 5 and bars["n_ticks"].sum() == 60 and bars["n_ticks"].iloc[0] == 6   # threshold 10 * 0.5 = 5; the first trade has no direction, so 6 trades
    assert bool(bars["complete"].iloc[:-1].all())
    mixed = small([100, 100.25, 100, 100.25, 100] * 12)
    assert len(tk.imbalance_bars(mixed, kind="volume", initial_ticks=10, initial_imbalance=0.5)) < len(bars)       # balanced flow: bars take longer
    with pytest.raises(BadInput):
        tk.imbalance_bars(frame, kind="dollar")


# ------------------------------------------------------------------------------------------------ direction and spread
def test_tick_rule_carries_direction_and_assumes_nothing_at_the_start_ECT_013() -> None:
    sign = tk.tick_rule(pd.Series([10.0, 10.0, 11.0, 11.0, 10.5, 10.5]))
    assert list(sign) == [0, 0, 1, 1, -1, -1]


def test_lee_ready_quote_rule_tick_rule_and_crossed_quotes_ECT_013() -> None:
    t = pd.date_range("2024-03-28 23:00:00", periods=4, freq="1s", tz="UTC")
    trades = pd.DataFrame({"price": [100.30, 99.70, 100.00, 100.50], "size": [1.0, 2.0, 3.0, 4.0]}, index=t)
    quotes = pd.DataFrame({"bid": [99.75, 99.75, 101.0], "ask": [100.25, 100.25, 100.0]},
                          index=pd.DatetimeIndex([t[0] - pd.Timedelta("1s"), t[0] - pd.Timedelta("500ms"), t[3] - pd.Timedelta("100ms")]))
    out = tk.lee_ready(trades, quotes)
    # trade 1: above the mid 100.00 -> buy; trade 2: below -> sell; trade 3: at the mid -> tick rule (price rose from 99.70) -> buy;
    # trade 4: the latest quote is crossed (bid above ask) -> tick rule (price rose) -> buy
    assert list(out["side"]) == [1, -1, 1, 1]
    assert list(out["buy_volume"]) == [1.0, 0.0, 3.0, 4.0] and list(out["sell_volume"]) == [0.0, 2.0, 0.0, 0.0]
    delayed = tk.lee_ready(trades, quotes, delay="5s")                                  # a 5 s delay: no quote is known yet, everything falls to the tick rule
    assert list(delayed["side"]) == [0, -1, 1, 1] and delayed["bid"].isna().all()


def test_quote_rule_on_the_recorded_ticks_ECT_013() -> None:
    trades, quotes = load("ticks.csv").iloc[:100], load("quotes.csv").iloc[:100]
    out = tk.lee_ready(trades, quotes)
    assert set(out["side"].unique()) <= {-1, 0, 1} and len(out) == len(trades)
    assert (out["buy_volume"] + out["sell_volume"] <= out["size"]).all()


def test_bid_ask_bounce_roll_estimator_ECT_013() -> None:
    bouncing = pd.Series([100.0, 100.5] * 20)                                           # pure bounce of 0.5: changes alternate +0.5, -0.5
    out = tk.roll_spread(bouncing)
    assert out["autocorrelation"] == pytest.approx(-1.0, abs=0.05) and out["spread"] == pytest.approx(1.0, abs=0.05)
    trending = tk.roll_spread(pd.Series(np.arange(30.0)))
    assert trending["spread"] is None or trending["autocovariance"] >= 0               # no bounce: the estimator does not exist
    assert tk.roll_spread(pd.Series([1.0, 2.0])) == {"autocovariance": None, "autocorrelation": None, "spread": None}


# ------------------------------------------------------------------------------------------------ storage and the call
def test_ticks_round_trip_through_compressed_storage_ECT_013(tmp_path) -> None:
    ticks = load("ticks.csv")
    path = tk.write_ticks(ticks, tmp_path / "a" / "ticks.csv.gz")
    back = tk.read_ticks(path)
    assert back.index.equals(ticks.index) and back["price"].tolist() == ticks["price"].tolist() and back.index.tz is not None


def test_bars_from_ticks_cut_at_as_of_and_declare_source_ECT_013() -> None:
    ticks = load("ticks.csv")
    cut = when("2024-03-28T23:00:30")
    result = tk.bars_from_ticks(ticks, "volume", 50, as_of=cut, source="recorded", clock=lambda: when("2024-04-01"))
    assert result.ok and result.source == "recorded" and result.received_at == when("2024-04-01") and result.meta["cleaning"]["received"] > 0
    assert result.value.index.max() <= pd.Timestamp(cut)
    early = tk.bars_from_ticks(ticks, "tick", 5, as_of=when("2024-03-01"), clock=lambda: when("2024-04-01"))
    assert (early.status, early.reason, early.value) == (sentinels.NO_DATA, "no_ticks_by_as_of", None)
    junk = small([-1.0, -2.0, -3.0, -4.0])
    assert tk.bars_from_ticks(junk, "tick", 2, as_of=when("2024-04-01")).reason == "no_ticks_after_cleaning"
    with pytest.raises(BadInput):
        tk.bars_from_ticks(ticks, "renko", 5, as_of=when("2024-04-01"))
    for kind, size in (("time", "10s"), ("tick", 10), ("dollar", 1_000_000), ("imbalance", "tick")):
        assert tk.bars_from_ticks(ticks, kind, size, as_of=when("2024-04-01")).ok
