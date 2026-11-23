"""Registry, types, helpers and hashing (SPEC-017: ED-042, AH-11, AH-47, AH-48)."""
import math

import pandas as pd
import pytest

from hokma.desks.core.hashing import content_hash, render
from hokma.quant import helpers, registry
from hokma.quant.errors import BadInput, BlockedFunction, UnknownFunction
from hokma.quant.types import QuantResult


@pytest.fixture(autouse=True)
def _isolated_registry():
    saved = dict(registry._FUNCTIONS)
    registry._reset_for_tests()
    yield
    kept = {n: s for n, s in registry._FUNCTIONS.items() if str(getattr(s.fn, "__module__", "")).startswith("hokma.quant.")}
    registry._reset_for_tests()
    registry._FUNCTIONS.update(saved)
    registry._FUNCTIONS.update(kept)   # modules imported during the test stay registered: Python will not import them again


def _register_double():
    @registry.register("metrics.double", module="metrics", summary="doubles x", inputs={"x": {"type": "float", "required": True}}, piece_ids=("T-1",))
    def double(x: float) -> QuantResult:
        return QuantResult(name="metrics.double", value=2 * x, unit="ratio", method="x*2", piece_ids=("T-1",))


def test_register_list_describe_call():
    _register_double()
    assert [s.name for s in registry.list("metrics")] == ["metrics.double"]
    assert registry.describe("metrics.double").piece_ids == ("T-1",)
    assert registry.call("metrics.double", x=2.5).value == 5.0


def test_unregistered_name_does_not_exist():
    with pytest.raises(UnknownFunction):
        registry.call("metrics.nope")


def test_bad_arguments_are_bad_input():
    _register_double()
    with pytest.raises(BadInput):
        registry.call("metrics.double")
    with pytest.raises(BadInput):
        registry.call("metrics.double", x=1, y=2)


def test_blocked_function_names_its_replacement():
    registry.register_blocked("metrics.probabilistic_sharpe_ratio", module="metrics", summary="blocked", replacement="validation.psr")
    assert registry.list("metrics") == []
    assert [s.name for s in registry.list("metrics", include_blocked=True)] == ["metrics.probabilistic_sharpe_ratio"]
    with pytest.raises(BlockedFunction) as err:
        registry.call("metrics.probabilistic_sharpe_ratio")
    assert err.value.replacement == "validation.psr"


def test_result_roundtrip_and_empty():
    empty = QuantResult.empty("metrics.x", "ratio", "m", "no data")
    assert empty.value is None and not empty.ok and empty.warnings == ("no data",)
    assert empty.to_dict()["as_of"] is None


def test_helpers_known_values():
    assert helpers.safe_div(1, 0) is None and helpers.safe_div(6, 3) == 2
    assert helpers.periods_per_year("daily") == 252
    assert math.isclose(helpers.annualize_rate(0.01, 12), 1.01 ** 12 - 1)
    assert math.isclose(helpers.deannualize_rate(helpers.annualize_rate(0.01, 12), 12), 0.01)
    assert math.isclose(helpers.total_return(pd.Series([0.1, 0.1])), 0.21)
    dd = helpers.drawdown_series(pd.Series([0.1, -0.5, 0.0]))
    assert math.isclose(dd.min(), -0.5)
    assert helpers.finite_or_none(float("nan")) is None
    s, w = helpers.validate_returns([0.01, float("nan")])
    assert len(s) == 1 and w
    with pytest.raises(BadInput):
        helpers.as_series("abc")


def test_hash_ignores_dates_but_not_numbers():
    a = {"close": 10.0, "generated_at": "2026-01-01T00:00:00"}
    b = {"generated_at": "2027-05-05T10:00:00", "close": 10.0}
    assert content_hash(a) == content_hash(b)
    assert content_hash(a) != content_hash({"close": 10.5, "generated_at": "2026-01-01"})
    assert render(a) == render(b)
    assert helpers.hash_inputs(pd.Series([1.0, 2.0])) == helpers.hash_inputs(pd.Series([1.0, 2.0]))


def test_describe_dict_lists_piece_ids():
    _register_double()
    assert registry.describe("metrics.double").describe()["piece_ids"] == ["T-1"]
