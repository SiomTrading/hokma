"""Parameter validation (SPEC-020: B-03): the error text names the parameter, the range and the value."""
import pytest

from hokma.factory.params import Param, ParamError, ParamSchema, validate

SCHEMA = ParamSchema((Param("fast", "int", 12, 2, 200), Param("scale", "float", 0.5, 0.0, 1.0), Param("mode", "choice", "long", choices=("long", "short")),
                      Param("window", "int", 20, 1, 1_000_000), Param("on", "bool", True)))


def test_defaults_fill_and_valid_values_pass():
    assert validate(SCHEMA, {"fast": 20}) == {"fast": 20, "scale": 0.5, "mode": "long", "window": 20, "on": True}


@pytest.mark.parametrize("values, text", [
    ({"fats": 3}, "parameter 'fats' is unknown; allowed: fast, scale, mode, window, on"),
    ({"fast": "abc"}, "parameter 'fast' must be a whole number, got 'abc' (str)"),
    ({"fast": 500}, "parameter 'fast' must be between 2 and 200, got 500"),
    ({"fast": 1}, "parameter 'fast' must be between 2 and 200, got 1"),
    ({"scale": 1.5}, "parameter 'scale' must be between 0.0 and 1.0, got 1.5"),
    ({"scale": True}, "parameter 'scale' must be a finite number, got True (bool)"),
    ({"mode": "flat"}, "parameter 'mode' must be one of long, short, got 'flat'"),
    ({"on": 1}, "parameter 'on' must be true or false, got 1 (int)"),
    ({"window": 2_000_000}, "parameter 'window' must be between 1 and 1000000, got 2000000"),
])
def test_bad_values_give_the_exact_sentence(values, text):
    with pytest.raises(ParamError) as caught:
        validate(SCHEMA, values)
    assert str(caught.value) == text
    assert caught.value.code == "bad_param"


def test_the_size_cap_of_quant_limits_applies_inside_the_range():
    schema = ParamSchema((Param("window", "int", 10, 1, 10_000_000),))
    with pytest.raises(ParamError, match="window=200000 is above the limit 100000"):
        validate(schema, {"window": 200_000})


def test_required_parameter_without_default_and_duplicate_names():
    with pytest.raises(ParamError, match="parameter 'x' is required"):
        validate(ParamSchema((Param("x", "int"),)), {})
    with pytest.raises(ValueError):
        ParamSchema((Param("x", "int", 1), Param("x", "int", 2)))
