"""Snapshots: verified market data, as-of snapshot, fundamentals snapshot, combined hash (SPEC-017: TA-32, OA-60, AH-46)."""
import numpy as np
import pandas as pd
import pytest

from hokma.data import sentinels, snapshot as snapshot_module
from hokma.data.models import PeriodFundamentals
from hokma.data.snapshot import (FundamentalsSnapshot, as_of_snapshot, build_fundamentals_snapshot, compute_indicator, snapshot,
                                 verified_market_snapshot)

from .support import FakeMarketSource, fixture_json, market_source, when  # noqa: F401


def linear_frame(n: int = 40, last: str = "2024-03-08") -> pd.DataFrame:
    index = pd.bdate_range(end=last, periods=n, tz="UTC")
    close = np.arange(1.0, n + 1)
    return pd.DataFrame({"open": close - 0.25, "high": close + 0.5, "low": close - 0.5, "close": close, "volume": 100.0}, index=index)


# ------------------------------------------------------------------------------------------------ TA-32
def test_indicators_match_hand_written_values_TA_32() -> None:
    frame = linear_frame()
    assert compute_indicator(frame, "close_10_sma") == pytest.approx(35.5)                 # mean of 31..40
    assert compute_indicator(frame, "boll") == pytest.approx(30.5)                         # mean of 21..40
    assert compute_indicator(frame, "boll_ub") == pytest.approx(30.5 + 2 * np.sqrt((20 ** 2 - 1) / 12))
    assert compute_indicator(frame, "boll_lb") == pytest.approx(30.5 - 2 * np.sqrt((20 ** 2 - 1) / 12))
    assert compute_indicator(frame, "rsi") == pytest.approx(100.0)                         # only up moves
    assert compute_indicator(frame, "atr") == pytest.approx(1.5 - 0.5 * (13 / 14) ** 39)    # first range 1.0, then 1.5 each; Wilder smoothing of 1/14
    assert compute_indicator(frame, "macd") > 0 and compute_indicator(frame, "close_10_ema") == pytest.approx(compute_indicator(frame, "close_10_ema"))
    assert compute_indicator(frame, "close_50_sma") is None                                 # not enough rows: no value, no guess
    with pytest.raises(ValueError):
        compute_indicator(frame, "nonsense")


def test_verified_snapshot_excludes_rows_after_as_of_TA_32() -> None:
    frame = linear_frame(50, "2024-03-22")
    out = verified_market_snapshot(frame, when("2024-03-08"))
    assert out["latest_bar"].startswith("2024-03-08") and out["latest"]["close"] == float(len(frame[frame.index <= when("2024-03-08")]))
    assert out["rows_used"] == int((frame.index <= when("2024-03-08")).sum())
    assert all(c["time"] <= "2024-03-08T23:59:59+00:00" for c in out["recent_closes"]) and len(out["recent_closes"]) == 30
    assert set(out["indicators"]) == set(snapshot_module.DEFAULT_INDICATORS)
    with pytest.raises(ValueError):
        verified_market_snapshot(frame, when("2023-01-01"))


def test_snapshot_sections_carry_source_reception_and_sentinels_TA_32() -> None:
    frame = linear_frame()
    sources = {
        "market": lambda symbol, as_of: sentinels.ok(verified_market_snapshot(frame, as_of), source="recorded", as_of=as_of, received_at=when("2024-04-01")),
        "fundamentals": lambda symbol, as_of: sentinels.unavailable("prov", sentinels.REASON_KEY_MISSING, as_of=as_of, received_at=when("2024-04-01")),
    }
    snap = snapshot("ACME", as_of=when("2024-03-08"), categories=["market", "fundamentals"], sources=sources)
    assert snap.sections["market"].source == "recorded" and snap.sections["market"].received_at == when("2024-04-01")
    assert snap.sections["fundamentals"].status == sentinels.DATA_UNAVAILABLE and snap.sections["fundamentals"].value is None
    assert snap.ok is False and len(snap.content_hash) == 64
    with pytest.raises(ValueError):
        snapshot("ACME", as_of=when("2024-03-08"), categories=["nope"])


def test_snapshot_hash_ignores_reception_time_and_changes_with_a_number_TA_32() -> None:
    def build(close_shift: float, received: str) -> str:
        frame = linear_frame().assign(close=lambda f: f["close"] + close_shift)
        src = {"market": lambda s, a: sentinels.ok(verified_market_snapshot(frame, a), source="recorded", as_of=a, received_at=when(received))}
        return snapshot("ACME", as_of=when("2024-03-08"), categories=["market"], sources=src).content_hash

    assert build(0.0, "2024-04-01") == build(0.0, "2025-01-01")
    assert build(0.0, "2024-04-01") != build(0.01, "2024-04-01")


def test_default_market_section_reads_bars_from_hokma_market_TA_32(market_source) -> None:
    source = market_source(FakeMarketSource())
    snap = snapshot("ACME", as_of=when("2024-03-15"), categories=["market"])
    section = snap.sections["market"]
    assert section.ok and section.source == "hokma_market" and source.calls == [("ACME", "1D")]
    assert section.value["verified"]["latest_bar"].startswith("2024-03-14") and section.value["is_latest_actual"] is True
    missing = snapshot("ACME", as_of=when("2024-03-15"), categories=["market"])  # same source again: deterministic hash
    assert missing.content_hash == snap.content_hash


# ------------------------------------------------------------------------------------------------ OA-60
def test_as_of_snapshot_hand_values_OA_60() -> None:
    frame = linear_frame(40, "2024-03-08")
    out = as_of_snapshot(frame, when("2024-03-08"), bars_out=3)
    assert out["is_latest_actual"] is True and out["stale_trading_days"] == 0 and out["warnings"] == []
    assert out["latest"]["close"] == 40.0 and out["latest"]["previous_close"] == 39.0
    assert out["latest"]["change_pct"] == pytest.approx((40 / 39 - 1) * 100)
    assert out["latest"]["day_amplitude_pct"] == pytest.approx(1 / 39 * 100)
    assert out["levels"]["sma20"] == pytest.approx(30.5) and out["levels"]["sma50"] is None and out["levels"]["rsi14"] == pytest.approx(100.0)
    assert out["levels"]["period_high"] == 40.5 and out["levels"]["period_low"] == 0.5
    assert out["levels"]["dist_from_high_pct"] == pytest.approx((40 - 40.5) / 40.5 * 100)
    assert out["levels"]["dist_from_low_pct"] == pytest.approx((40 - 0.5) / 0.5 * 100)
    assert [b["close"] for b in out["bars"]] == [38.0, 39.0, 40.0]


def test_as_of_snapshot_warns_how_many_trading_days_behind_OA_60() -> None:
    frame = linear_frame(40, "2024-03-08")                 # a Friday
    out = as_of_snapshot(frame, when("2024-03-14T16:00:00"))
    assert out["is_latest_actual"] is False and out["stale_trading_days"] == 4
    assert len(out["warnings"]) == 1 and "4 trading day(s) behind" in out["warnings"][0] and "2024-03-08" in out["warnings"][0]
    midnight = as_of_snapshot(frame, when("2024-03-11"))                      # Monday 00:00: the Friday bar is the newest that can exist
    assert midnight["is_latest_actual"] is True and midnight["warnings"] == []
    empty = as_of_snapshot(frame, when("2020-01-01"))
    assert empty["latest"] is None and empty["bars"] == [] and empty["window_bars"] == 0


# ------------------------------------------------------------------------------------------------ AH-46
def metrics() -> list[dict]:
    return fixture_json("fd_financial_metrics.json")["financial_metrics"]


def test_fundamentals_snapshot_derived_numbers_AH_46() -> None:
    result = build_fundamentals_snapshot("ACME", "2024-04-30", metrics(), facts={"sector": "Technology", "industry": "Software"})
    snap = result.value
    assert result.ok and result.source == "financial_datasets" and result.received_at
    assert [p.filing_date for p in snap.periods] == ["2024-02-01", "2023-11-02", "2023-08-03", "2023-05-04", "2023-02-02"]   # newest first, nothing filed later
    assert snap.roe_avg == pytest.approx(0.32) and snap.net_margin_avg == pytest.approx(0.258)
    assert snap.gross_margin_trend == pytest.approx(0.03) and snap.bvps_cagr == pytest.approx(0.2667)
    assert snap.debt_to_equity_latest == 1.1 and snap.market_cap_latest == 1.4e11
    assert (snap.sector, snap.industry) == ("Technology", "Software") and "sector_industry_are_latest_not_vintage" in result.warnings
    assert isinstance(snap, FundamentalsSnapshot) and isinstance(snap.periods[0], PeriodFundamentals)


def test_fundamentals_snapshot_hash_and_render_do_not_depend_on_the_date_AH_46() -> None:
    a = build_fundamentals_snapshot("ACME", "2024-04-30", metrics()).value
    b = build_fundamentals_snapshot("ACME", "2024-04-15", metrics()).value          # no new filing in between
    c = build_fundamentals_snapshot("ACME", "2024-05-02", metrics()).value          # the 10-Q arrives
    assert a.content_hash == b.content_hash and a.render() == b.render() and a.content_hash != c.content_hash
    assert "2024-04-30" not in a.render() and "Company: ACME" in a.render()


def test_too_few_filed_periods_is_no_data_not_a_neutral_view_AH_46() -> None:
    result = build_fundamentals_snapshot("ACME", "2023-06-01", metrics())
    assert result.status == sentinels.NO_DATA and result.reason == "insufficient_periods" and result.value is None
    assert build_fundamentals_snapshot("ACME", "2024-04-30", metrics(), min_periods=6).status == sentinels.NO_DATA
    assert build_fundamentals_snapshot("ACME", "2024-04-30", [], min_periods=1).status == sentinels.NO_DATA
