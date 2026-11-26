"""IC against horizon and the cost of turnover (SPEC-020: ECT-036). The H3 correlation is injected; the aligned data is checked by hand."""
import json
import math
from pathlib import Path

import numpy as np
import pytest

from hokma.factory.studies import _h3, ic_decay
from hokma.quant.errors import BadInput

FIXTURE = Path(__file__).resolve().parents[1] / "backtest" / "fixtures" / "nq_session.json"


def ranks(values):
    """Average ranks, written for the test (the module takes the correlation from H3)."""
    order = np.argsort(values, kind="stable")
    out = np.empty(len(values))
    i = 0
    while i < len(values):
        j = i
        while j + 1 < len(values) and values[order[j + 1]] == values[order[i]]:
            j += 1
        out[order[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return out


def spearman(x, y):
    return float(np.corrcoef(ranks(x), ranks(y))[0, 1])


def nq_minutes():
    bars = json.loads(FIXTURE.read_text(encoding="utf-8"))["day"]["bars"]
    return np.array([float(b["open"]) for b in bars]), np.array([float(b["close"]) for b in bars])


def test_forward_returns_start_at_the_next_open_by_hand():
    opens = [100, 101, 102, 100, 103, 104, 102, 105]
    y = ic_decay.forward_returns(opens, 1)                      # y[t] = open[t + 2] / open[t + 1] - 1
    assert y[:6] == pytest.approx([102 / 101 - 1, 100 / 102 - 1, 103 / 100 - 1, 104 / 103 - 1, 102 / 104 - 1, 105 / 102 - 1])
    assert np.isnan(y[6:]).all()
    y3 = ic_decay.forward_returns(opens, 3)                     # y[t] = open[t + 4] / open[t + 1] - 1: four rows exist at most
    assert y3[:4] == pytest.approx([103 / 101 - 1, 104 / 102 - 1, 102 / 100 - 1, 105 / 103 - 1]) and np.isnan(y3[4:]).all()
    assert np.isnan(ic_decay.forward_returns([100, 101], 1)).all()


def test_the_curve_hands_the_aligned_rows_to_the_h3_function_and_computes_the_gross_gain():
    opens = [100, 101, 102, 100, 103, 104, 102, 105]
    signal = [1, -1, 1, 1, -1, 1, 1, 1]
    calls = []

    def fake(x, y):
        calls.append((x.tolist(), y.tolist()))
        return 0.25

    curve = ic_decay.ic_curve(signal, opens, [1], bars_per_year=100, bet_correlation=0.0, min_obs=3, ic_fn=fake)
    assert calls[0][0] == [1, -1, 1, 1, -1, 1]
    assert calls[0][1] == pytest.approx([102 / 101 - 1, 100 / 102 - 1, 103 / 100 - 1, 104 / 103 - 1, 102 / 104 - 1, 105 / 102 - 1])
    point = curve.points[0]
    gross = ((102 / 101 - 1) - (100 / 102 - 1) + (103 / 100 - 1) + (104 / 103 - 1) - (102 / 104 - 1) + (105 / 102 - 1)) / 6
    assert point.n_obs == 6 and point.ic == 0.25 and point.gross_per_bet == pytest.approx(gross)
    assert point.breakeven_bps == pytest.approx(gross * 10_000) and point.ir_per_year == pytest.approx(0.25 * math.sqrt(100))
    assert curve.best_horizon == 1


def test_curve_on_real_nq_minutes_at_1_5_15_30():
    opens, closes = nq_minutes()
    signal = np.r_[np.nan, closes[1:] / closes[:-1] - 1.0]     # the last one minute return, known at the close of each bar
    curve = ic_decay.ic_curve(signal, opens, [30, 1, 15, 5], bars_per_year=390 * 252, bet_correlation=0.5, min_obs=30, ic_fn=spearman)
    assert [p.horizon for p in curve.points] == [1, 5, 15, 30]
    assert [p.n_obs for p in curve.points] == [387, 383, 373, 358]       # 390 rows, minus the NaN signal, minus 1 + horizon without a future
    for p in curve.points:
        y = ic_decay.forward_returns(opens, p.horizon)
        keep = np.isfinite(signal) & np.isfinite(y)
        assert p.ic == pytest.approx(spearman(signal[keep], y[keep]))
        assert -1.0 <= p.ic <= 1.0
    assert curve.warnings == ()


def test_a_short_horizon_without_enough_rows_has_no_ic_and_says_so():
    curve = ic_decay.ic_curve([1, -1, 1, 1], [100, 101, 102, 100], [1, 2], bars_per_year=252, bet_correlation=0.0, min_obs=3, ic_fn=lambda x, y: 0.1)
    assert [p.ic for p in curve.points] == [None, None] and len(curve.warnings) == 2 and curve.best_horizon is None


def test_h3_function_that_gives_nothing_is_reported_not_filled():
    curve = ic_decay.ic_curve(list(range(1, 11)), list(range(100, 110)), [1], bars_per_year=252, bet_correlation=0.0, min_obs=3, ic_fn=lambda x, y: None)
    assert curve.points[0].ic is None and curve.points[0].ir_per_year is None and "not defined" in curve.warnings[0]


def test_without_h3_and_without_an_injected_function_the_error_is_stable(monkeypatch):
    monkeypatch.setattr(_h3, "ALLOWED_PREFIX", "nowhere.")          # H3 is installed: the module is made unreachable instead
    with pytest.raises(_h3.H3Unavailable, match="h3_unavailable: hokma.quant.portfolio.ic is not a quant module"):
        ic_decay.ic_curve(list(range(1, 11)), list(range(100, 110)), [1], bars_per_year=252, bet_correlation=0.0, min_obs=3)
    assert _h3.H3Unavailable.code == "h3_unavailable"


def test_effective_breadth_by_hand():
    assert ic_decay.effective_breadth(100, 0.1) == pytest.approx(100 / (1 + 99 * 0.1))      # 9.174...
    assert ic_decay.effective_breadth(20, 0.1) == pytest.approx(20 / 2.9)                    # 6.9
    assert ic_decay.effective_breadth(50, 0.0) == 50
    with pytest.raises(BadInput):
        ic_decay.effective_breadth(50, 1.0)


def test_break_even_and_cost_by_hand():
    """Gross alpha 9.84 percent, one way turnover 35 percent a month: 4.2 a year. Break-even 9.84 / 4.2 = 234.3 bp; cost at 20 bp 0.84 percent."""
    assert ic_decay.annual_cost(0.35, 12, 20) == pytest.approx(0.0084)
    assert ic_decay.annual_cost(0.35, 12, 40) == pytest.approx(0.0168)
    assert ic_decay.breakeven_cost_bps(0.0984, 0.35, 12) == pytest.approx(234.2857142857, rel=1e-9)
    assert ic_decay.breakeven_cost_bps(0.0984, 0.0) is None


def test_bad_inputs_raise_bad_input():
    good = dict(bars_per_year=252, bet_correlation=0.0, min_obs=3, ic_fn=lambda x, y: 0.0)
    for horizons in ([], [0], [1.5], [2000], list(range(1, 40))):
        with pytest.raises(BadInput):
            ic_decay.ic_curve([1, 2, 3, 4, 5], [1, 2, 3, 4, 5], horizons, **good)
    with pytest.raises(BadInput):
        ic_decay.ic_curve([1, 2, 3], [1, 2, 3, 4], [1], **good)
    with pytest.raises(BadInput):
        ic_decay.ic_curve([1, 2, 3, 4, 5], [1, 2, 3, 4, 5], [1], **{**good, "min_obs": 2})
    with pytest.raises(BadInput):
        ic_decay.ic_curve([1, 2, 3, 4, 5], [1, 2, 3, 4, 5], [1], **{**good, "bet_correlation": 1.0})


def test_no_invented_sharpe_haircut_ECT_036():
    assert not [n for n in dir(ic_decay) if "haircut" in n.lower()]
