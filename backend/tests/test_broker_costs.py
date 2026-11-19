"""Pure cost functions (task 3294, C7-5b): adverse slippage and commission, with their edges."""
import pytest

from hokma.broker.costs import DEFAULT_COMMISSION, commission, fill_price
from hokma.db.errors import ValidationError


def test_slippage_goes_against_the_trader():
    assert fill_price("buy", 100.0, 2.0) == 100.02
    assert fill_price("sell", 100.0, 2.0) == 99.98


def test_slippage_edges():
    assert fill_price("buy", 100.0, 0) == fill_price("sell", 100.0, 0) == 100.0
    assert fill_price("buy", 0, 5.0) == 0.0
    assert fill_price("buy", 33.3333, 1.0) == 33.3366   # 4 decimals


@pytest.mark.parametrize("args", [("hold", 1, 1), ("buy", -1, 1), ("buy", 1, -1), ("buy", "x", 1), ("buy", True, 1),
                                   ("buy", float("nan"), 1)])
def test_slippage_rejects_bad_input(args):
    with pytest.raises(ValidationError):
        fill_price(*args)


def test_commission_bps_has_a_minimum_and_rounds_to_the_cent():
    assert commission(10_000, 100, DEFAULT_COMMISSION) == 1.0
    assert commission(1_234.56, 10, DEFAULT_COMMISSION) == 0.12
    assert commission(100, 1, {"type": "bps", "value": 1.0, "min": 1.0}) == 1.0


def test_commission_per_share():
    assert commission(0, 200, {"type": "per_share", "value": 0.005, "min": 1.0}) == 1.0
    assert commission(0, 1000, {"type": "per_share", "value": 0.005, "min": 1.0}) == 5.0


def test_commission_edges():
    assert commission(0, 100, DEFAULT_COMMISSION) == 0.0                                  # price 0
    assert commission(5000, 0, {"type": "bps", "value": 1.0, "min": 1.0}) == 0.0          # nothing traded: no minimum
    assert commission(10_000, 10, {"type": "bps", "value": 0, "min": 0}) == 0.0           # bps 0


@pytest.mark.parametrize("cfg", [{}, {"type": "flat", "value": 1}, {"type": "bps", "value": -1},
                                 {"type": "bps", "value": 1, "min": -1}, {"type": "bps"}, None])
def test_commission_rejects_bad_config(cfg):
    with pytest.raises(ValidationError):
        commission(100, 1, cfg)
