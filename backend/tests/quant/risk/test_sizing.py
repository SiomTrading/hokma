"""Position sizing caps. Row FM-06."""
import pytest

from hokma.quant import risk
from hokma.quant.errors import BadInput

from .conftest import call, piece_ids


def test_kelly_payoff_exact_value_FM_06():
    res = call("risk.kelly_payoff", p_win=0.55, payoff=1.5)
    # f = (p*b - (1-p)) / b = (0.825 - 0.45) / 1.5 = 0.25
    assert res.value == pytest.approx(0.25, abs=1e-12)
    assert res.unit == "fraction" and res.method == "kelly_payoff" and res.warnings == ()
    assert res.params == {"p_win": 0.55, "payoff": 1.5}
    assert "FM-06" in piece_ids("risk.kelly_payoff")


@pytest.mark.parametrize("p_win, payoff", [(0.40, 1.0), (0.50, 1.0), (0.30, 2.0), (0.0, 3.0)])
def test_kelly_without_edge_is_zero_with_a_warning_FM_06(p_win, payoff):
    res = call("risk.kelly_payoff", p_win=p_win, payoff=payoff)
    assert res.value == 0.0
    assert res.warnings and "no edge" in res.warnings[0]


def test_kelly_other_values_FM_06():
    assert call("risk.kelly_payoff", p_win=0.6, payoff=1.0).value == pytest.approx(0.2, abs=1e-12)
    assert call("risk.kelly_payoff", p_win=1.0, payoff=0.5).value == pytest.approx(1.0, abs=1e-12)
    assert risk.kelly_payoff(0.55, 1.5).value == pytest.approx(0.25, abs=1e-12)


def test_kelly_is_never_negative_FM_06():
    for p in [i / 20 for i in range(21)]:
        for b in (0.1, 0.5, 1.0, 2.0, 10.0):
            assert call("risk.kelly_payoff", p_win=p, payoff=b).value >= 0.0


def test_kelly_bad_input_FM_06():
    for p_win, payoff in ((1.2, 1.0), (-0.1, 1.0), (0.5, 0.0), (0.5, -1.0), (float("nan"), 1.0), (0.5, float("inf"))):
        res = call("risk.kelly_payoff", p_win=p_win, payoff=payoff)
        assert res.value is None and res.warnings
    with pytest.raises(BadInput):
        call("risk.kelly_payoff", p_win="0.5", payoff=1.0)
    with pytest.raises(BadInput):
        call("risk.kelly_payoff", p_win=True, payoff=1.0)
