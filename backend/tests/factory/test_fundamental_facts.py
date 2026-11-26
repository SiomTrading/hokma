"""The facts of the value and fundamental alphas come from H3 and are point in time (SPEC-020: AH-30, AH-31). The statements are the three companies
of the H3 fixture and the closes are the real daily bars of SPY of the H3 fixture."""
import json
from decimal import Decimal as D
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hokma.factory import book, fundamental_facts as ff
from hokma.quant.errors import BadInput

FIXTURES = Path(__file__).resolve().parents[1] / "quant" / "fixtures"
PIT = json.loads((FIXTURES / "statements_pit.json").read_text(encoding="utf-8"))
SPY = pd.read_csv(FIXTURES / "ohlcv_daily_spy.csv", parse_dates=["date"]).set_index("date")["close"]


def statements(company: str) -> pd.DataFrame:
    frame = pd.DataFrame(PIT["companies"][company]["rows"]).set_index("period_end")
    numeric = [c for c in frame.columns if c != "filed"]
    frame[numeric] = frame[numeric].astype(float)
    return frame


def test_the_yields_of_alfa_are_the_hand_computed_ones_of_its_last_filed_period():
    """Period 2023-12 (filed 2024-02-20): net income 237.6, 98 shares, price 60; cash flow 290 less capex 75 = 215; equity 1160."""
    got = ff.value_facts(statements("ALFA"), "2024-06-30")
    assert got["earnings_yield"] == pytest.approx(237.6 / 98 / 60) and got["fcf_yield"] == pytest.approx((290 - 75) / 98 / 60)
    assert got["book_to_price"] == pytest.approx(1160 / 98 / 60)


def test_a_period_filed_after_the_date_never_moves_a_fact_and_a_later_date_does():
    """The period 2024-06 is filed on 2024-08-05: the cut of 2024-08-04 does not see it, the cut of 2024-08-05 does (231.2, 97 shares, price 62)."""
    before, after = ff.value_facts(statements("ALFA"), "2024-08-04"), ff.value_facts(statements("ALFA"), "2024-08-05")
    assert before == ff.value_facts(statements("ALFA"), "2024-06-30")
    assert after["earnings_yield"] == pytest.approx(231.2 / 97 / 62) and after["fcf_yield"] == pytest.approx((150 - 40) / 97 / 62)
    assert after["book_to_price"] == pytest.approx(1205 / 97 / 62)


def test_the_price_of_the_bar_replaces_the_price_of_the_statement_and_halving_the_price_doubles_each_yield():
    base = ff.value_facts(statements("ALFA"), "2025-01-31")
    half = ff.value_facts(statements("ALFA"), "2025-01-31", price=31.0)
    assert {k: half[k] / base[k] for k in base} == pytest.approx({k: 2.0 for k in base})


def test_a_loss_keeps_its_sign_and_a_missing_line_gives_none_not_zero():
    """BRAVO, period 2023-12: net income -55, 60 shares, price 2; cash flow -15 less capex 10 = -25. Its equity is negative: no book to price.
    CHARLIE reports no capex: no free cash flow yield; earnings 72 over 40 shares at 30, equity 440."""
    bravo = ff.value_facts(statements("BRAVO"), "2024-06-30")
    assert bravo["earnings_yield"] == pytest.approx(-55 / 60 / 2) and bravo["fcf_yield"] == pytest.approx(-25 / 60 / 2) and bravo["book_to_price"] is None
    charlie = ff.value_facts(statements("CHARLIE"), "2024-06-30")
    assert charlie["fcf_yield"] is None and charlie["earnings_yield"] == pytest.approx(72 / 40 / 30) and charlie["book_to_price"] == pytest.approx(440 / 40 / 30)


def test_the_quality_score_is_the_f_score_of_h3_and_none_without_its_lines():
    assert ff.quality_score(statements("ALFA"), "2024-06-30") == 7.0 and ff.quality_score(statements("BRAVO"), "2024-06-30") == 1.0
    assert ff.quality_score(statements("CHARLIE"), "2024-06-30") is None


def test_percentile_ranks_by_hand():
    """3, 1, 2 give ranks 3, 1, 2 of 3: 1, 0 and 0.5. A name with no score has none. Two names share the rank 1.5 of 3: (1.5 - 1) / 2 = 0.25."""
    assert ff.percentile_ranks({"A": 3.0, "B": None, "C": 1.0, "D": 2.0}) == {"A": 1.0, "B": None, "C": 0.0, "D": 0.5}
    assert ff.percentile_ranks({"A": 1.0, "B": 1.0, "C": 3.0}) == {"A": 0.25, "B": 0.25, "C": 1.0}
    assert ff.percentile_ranks({"A": 5.0}) == {"A": None}                                     # a rank among one name is not a rank
    with pytest.raises(BadInput):
        ff.percentile_ranks({})


CUT = SPY.index[850]                       # a day of 2024 between the filing of 2024-02 and the one of 2024-08


def universe():
    """Each name gets a window of the real SPY closes that runs past the date: the three move differently, and only the closes up to the date are read."""
    return {"ALFA": ff.Name(statements("ALFA"), SPY.iloc[600:900]), "BRAVO": ff.Name(statements("BRAVO"), SPY.iloc[650:950]),
            "CHARLIE": ff.Name(statements("CHARLIE"), SPY.iloc[700:1000])}


def test_the_three_ranks_of_the_universe_by_hand():
    """Yield ranks: earnings yield CHARLIE 1, ALFA 0.5, BRAVO 0; free cash flow yield ALFA 1, BRAVO 0 (CHARLIE has none); book to price CHARLIE 1,
    ALFA 0 (BRAVO has none). Mean ranks ALFA 0.5, BRAVO 0, CHARLIE 1: ranked again they stay 0.5, 0, 1. Quality: ALFA 1, BRAVO 0, CHARLIE none.
    Momentum: the return from 3 closes ago to the last close of each window."""
    out = ff.rank_facts(universe(), CUT, periods=3, skip=0)
    assert [out[n]["value_rank"] for n in ("ALFA", "BRAVO", "CHARLIE")] == [0.5, 0.0, 1.0]
    assert [out[n]["quality_rank"] for n in ("ALFA", "BRAVO", "CHARLIE")] == [1.0, 0.0, None]
    raw = {}
    for n, u in universe().items():
        closes = u.closes[u.closes.index <= CUT].to_numpy()
        raw[n] = closes[-1] / closes[-4] - 1.0
    expected = ff.percentile_ranks(raw)
    assert [out[n]["momentum_rank"] for n in ("ALFA", "BRAVO", "CHARLIE")] == [expected[n] for n in ("ALFA", "BRAVO", "CHARLIE")]
    assert out["ALFA"]["momentum_rank"] is not None


def test_a_close_after_the_date_does_not_change_the_momentum_rank():
    names = universe()
    changed = {n: ff.Name(u.statements, u.closes.where(u.closes.index <= CUT, u.closes * 3.0)) for n, u in names.items()}
    assert ff.rank_facts(changed, CUT, periods=3, skip=0) == ff.rank_facts(names, CUT, periods=3, skip=0)


def test_step_feeds_hold_each_rebalance_until_the_next_and_are_nan_before_the_first():
    dates = pd.date_range("2024-01-01", periods=10)
    seen = []

    def compute(day):
        seen.append(day)
        return {"x": float(len(seen)), "y": None}

    out = ff.step_feeds(dates, [pd.Timestamp("2024-01-03"), pd.Timestamp("2024-01-07")], compute)
    assert [d.isoformat() for d in seen] == ["2024-01-03", "2024-01-07"]                    # computed on the rebalance dates only
    assert np.isnan(out["x"][:2]).all() and out["x"][2:6].tolist() == [1.0] * 4 and out["x"][6:].tolist() == [2.0] * 4
    assert np.isnan(out["y"]).all()


def test_the_book_is_neutral_to_the_cent_by_hand():
    """Views 0.6 and 0.2 long, -0.5 and -0.1 short, gross 100000: 50000 each side. Long 3 : 1 -> 37500.00 and 12500.00; short 5 : 1 -> 41666.67 and 8333.33."""
    made = book.dollar_neutral_book({"A": 0.6, "B": 0.2, "C": -0.5, "D": -0.1, "E": 0.0}, D("100000"))
    assert made == {"A": D("37500.00"), "B": D("12500.00"), "C": D("-41666.67"), "D": D("-8333.33")} and book.net_usd(made) == 0
    assert book.dollar_neutral_book({"A": 0.6, "B": 0.2}, D("100000")) == {}                  # nothing to short: no book, not a half book
    with pytest.raises(BadInput):
        book.dollar_neutral_book({"A": 1.0, "B": -1.0}, D("0"))


def test_the_check_names_the_rebalance_that_is_not_neutral_or_has_no_book():
    from datetime import date

    good = book.dollar_neutral_book({"A": 1.0, "B": -1.0}, D("1000"))
    books = {date(2024, 1, 2): good, date(2024, 2, 1): {"A": D("500"), "B": D("-499.98")}, date(2024, 3, 1): {}}
    assert book.neutral_gaps(books) == (date(2024, 2, 1), date(2024, 3, 1))
    assert book.neutral_gaps(books, tolerance=D("0.05")) == (date(2024, 3, 1),)
