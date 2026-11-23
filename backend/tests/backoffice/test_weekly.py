"""Weekly digest of closed theses (SPEC-018: CTS-025). Values by hand."""
from decimal import Decimal

import pytest

from hokma.backoffice import weekly
from hokma.backoffice.weekly import ClosedThesis, DigestError, OpenThesis

D = Decimal


def thesis(tid, day, side, entry, stop, qty, mult, gross, costs="0", mae=None, mfe=None, **tags):
    return weekly.thesis_from_row({"thesis_id": tid, "closed_on": day, "side": side, "entry_price": entry, "stop_price": stop, "qty": qty, "multiplier": mult,
                                   "gross_pnl": gross, "costs": costs, "mae_pct": mae, "mfe_pct": mfe, **tags})


WEEK = [
    thesis("A", "2026-03-03", "long", "5000", "4990", "2", "50", "1000", "10", mae="2", mfe="3", source="x", exit_reason="target"),      # futures, multiplier 50
    thesis("B", "2026-03-04", "long", "100", "95", "100", "1", "-400", mae="-6", mfe="1", source="x", exit_reason="stop"),
    thesis("C", "2026-03-05", "long", "100", "101", "10", "1", "300"),                    # the stop was raised above the entry: no positive risk
    thesis("D", "2026-03-06", "short", "50", "52", "10", "1", "-20", source="y", exit_reason="stop"),
    thesis("F", "2026-03-06", "long", "10", None, "1", "1", "5", "5", source="y"),
    thesis("G", "2026-03-09", "long", "10", "9", "1", "1", "100"),                        # exits after the week
]
OPEN = [OpenThesis("O1", (("2026-03-04", D(150)), ("2026-03-20", D(99)))), OpenThesis("O2", (("2026-03-05", D(-30)),))]


def digest():
    return weekly.build_digest(WEEK, OPEN, start="2026-03-02", end="2026-03-08")


def test_headline_numbers_are_net_of_costs_and_only_for_closed_theses_of_the_week():
    d = digest()
    # nets: A 1000-10=990, B -400, C 300, D -20, F 5-5=0
    assert (d.total, d.winners, d.losers, d.breakeven) == (5, 2, 2, 1)
    assert d.win_rate == D("0.4") and d.expectancy == D(174) and d.net_pnl == D(870) and d.costs == D(15)
    assert d.profit_factor == D(1290) / D(420)                            # (990 + 300) / (400 + 20)
    assert d.avg_winner == D(645) and d.avg_loser == D(-210)


def test_r_uses_the_multiplier_and_only_a_positive_stop_risk():
    d = digest()
    # A: risk (5000-4990)*2*50 = 1000, R = 990/1000. B: risk 500, R = -400/500. D short: risk (52-50)*10 = 20, R = -20/20. C and F have none.
    assert d.r_count == 3 and d.r_average == (D("0.99") + D("-0.8") + D(-1)) / 3 == D("-0.27")
    assert abs(d.r_stdev ** 2 - D("1.2007")) < D("1e-20")                 # sample standard deviation
    assert WEEK[0].initial_risk == D(1000) and WEEK[2].initial_risk is None and WEEK[2].r_multiple is None and WEEK[4].r_multiple is None
    without_multiplier = thesis("A2", "2026-03-03", "long", "5000", "4990", "2", "1", "1000")
    assert without_multiplier.initial_risk == D(20)                       # the multiplier is what makes a future's risk real


def test_mae_and_mfe_are_clamped_and_averaged():
    d = digest()
    assert WEEK[0].mae_pct == 0                                           # a positive MAE is clamped to zero
    assert d.avg_mae_pct == D(-3) and d.avg_mfe_pct == D(2)               # (0 + -6) / 2 and (3 + 1) / 2


def test_the_breakdowns_keep_every_thesis_with_unknown_for_a_missing_tag():
    p = digest().patterns
    assert p["source"] == {"unknown": {"wins": 1, "losses": 0, "total": 1}, "x": {"wins": 1, "losses": 1, "total": 2}, "y": {"wins": 0, "losses": 1, "total": 2}}
    assert p["exit_reason"]["stop"] == {"wins": 0, "losses": 2, "total": 2}


def test_partial_trims_of_open_theses_are_apart_and_never_in_the_total():
    d = digest()
    assert d.partial_trims["count"] == 2 and d.partial_trims["total_realized"] == "120"      # 150 and -30; the trim of 03-20 is outside the week
    assert d.net_pnl == D(870)                                           # unchanged by the trims
    assert [t["thesis_id"] for t in d.partial_trims["trims"]] == ["O1", "O2"]
    assert d.to_dict()["partial_trims"]["note"].startswith("partial trims")


def test_an_empty_week_is_zeroed_not_invented():
    d = weekly.build_digest([], [], start="2026-03-02", end="2026-03-08")
    assert d.total == 0 and d.win_rate is None and d.expectancy is None and d.profit_factor is None and d.net_pnl == 0 and d.r_average is None


def test_no_losers_leaves_the_profit_factor_undefined():
    d = weekly.build_digest([WEEK[0]], start="2026-03-02", end="2026-03-08")
    assert d.profit_factor is None and d.win_rate == 1


def test_bad_records_are_refused():
    with pytest.raises(DigestError):
        weekly.thesis_from_row({"thesis_id": "x"})
    with pytest.raises(DigestError):
        thesis("x", "2026-03-03", "sideways", "1", "1", "1", "1", "1")
    with pytest.raises(DigestError):
        weekly.build_digest([], start="2026-03-09", end="2026-03-01")
    assert isinstance(WEEK[0], ClosedThesis)
