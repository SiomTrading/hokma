"""Contract between the copies of `hokma.factory.user_api` and the real types they stand for (task 3414, finding 5). It runs in the parent process, where both exist.

The rule: a copy may be STRICTER than the real type, never looser. What a rule builds with a copy must be accepted by the real type the parent builds from its data,
so a rule never passes its own test and then fails in the parent. The few things the parent adds or decides itself are listed in KNOWN_DIFFERENCES, and
that list is asserted too: if the real type changes, this test says so.
"""
import dataclasses
import math
from decimal import Decimal

import numpy as np
import pytest

from hokma.desks.core import signal as real_signal
from hokma.factory import indicator_library, orders, strategy, user_api
from hokma.factory.alphas import _base as real_base

KNOWN_DIFFERENCES = {
    # the parent supplies these when it builds the real Signal from the data of the rule (proxies, session 3)
    "Signal fields only in the real type": {"desk_id", "line", "symbol", "as_of", "horizon", "confidence", "snapshot_hash", "contract_version"},
    # the real Signal needs 2 to 4 sentences in the reason; a rule gives one reason of any length, and the parent decides how it composes the real one
    "reason sentences": (real_signal.MIN_SENTENCES, real_signal.MAX_SENTENCES),
}

NUMBERS = [1, 1.5, 0.5, 99.99, 100, 150, 0, -1, 1e308, float("nan"), float("inf"), float("-inf"), True, False, None, "5", "abc", Decimal("2"), np.float64(2.5),
           np.int64(3), [], {}]
TEXTS = ["ok", "", " ", "x" * 80, "x" * 81, "stop loss", "position sizing", "enter now", "stop_hunt", "the close broke the high", "tama\u00f1o de posici\u00f3n",
         None, 5, "buy now at the open"]


def outcome(build):
    try:
        return build(), None
    except Exception as exc:          # noqa: BLE001  (any refusal counts)
        return None, exc


def field_shape(cls):
    return [(f.name, f.default if f.default is not dataclasses.MISSING else "<required>") for f in dataclasses.fields(cls)]


def annotations(cls):
    return {f.name: str(f.type).replace("typing.", "") for f in dataclasses.fields(cls)}


# ---------------------------------------------------------------- the four units

@pytest.mark.parametrize("name,copy,real", [("Price", user_api.Price, orders.Price), ("Ticks", user_api.Ticks, orders.Ticks), ("ATR", user_api.ATR, orders.ATR),
                                            ("Pct", user_api.Pct, orders.Pct)])
def test_a_unit_of_the_copy_is_never_looser_than_the_real_one(name, copy, real):
    assert copy.TYPE == name.lower()
    for value in NUMBERS:
        made, copy_error = outcome(lambda: copy(value))
        if made is None:
            continue                                                  # stricter is allowed
        data = made.to_data()
        assert data["type"] == name.lower()
        built, real_error = outcome(lambda: real(data["value"]))
        assert real_error is None, f"{name}({value!r}) is accepted by the copy and refused by the real one: {real_error}"
        assert float(built.value) == data["value"]


def test_pct_stops_at_a_hundred_like_the_real_one():
    for value in (100, 100.0, 150):
        with pytest.raises(ValueError):
            user_api.Pct(value)
        with pytest.raises(orders.BadUnit):
            orders.Pct(value)
    assert user_api.Pct(99.99).to_data()["value"] == float(orders.Pct(99.99).value) == 99.99


def test_the_units_are_the_four_the_real_module_has():
    assert {"Price", "Ticks", "ATR", "Pct"} == {n for n in user_api.__all__ if n in ("Price", "Ticks", "ATR", "Pct")}
    assert {c.__name__ for c in orders.Unit.__args__} == {"Price", "Ticks", "ATR", "Pct"}


# ---------------------------------------------------------------- Criterion

def test_criterion_has_the_same_fields_in_the_same_order_with_the_same_types():
    assert field_shape(user_api.Criterion) == field_shape(real_signal.Criterion)
    assert annotations(user_api.Criterion) == annotations(real_signal.Criterion)


@pytest.mark.parametrize("name", TEXTS)
@pytest.mark.parametrize("source", ["s", "stop", "registry.name", "", None])
@pytest.mark.parametrize("value,threshold,passed", [(1.0, None, None), (None, None, True), (None, None, None), (1, 2, True), (float("nan"), None, None),
                                                    (True, None, None), (1.0, float("inf"), None), (np.float64(1.5), np.float64(1), False),
                                                    ("x", None, None)])
def test_a_criterion_of_the_copy_is_never_looser_than_the_real_one(name, source, value, threshold, passed):
    made, _ = outcome(lambda: user_api.Criterion(name, value, threshold, passed, source))
    if made is None:
        return
    built, error = outcome(lambda: real_signal.Criterion(**made.to_data()))
    assert error is None, f"accepted by the copy, refused by the real Criterion: {error}"
    assert built.to_dict() == made.to_data()


def test_the_execution_terms_are_the_same_pattern_in_both():
    assert user_api._EXECUTION_TERMS.pattern == real_signal.EXECUTION_TERMS.pattern
    assert user_api._EXECUTION_TERMS.flags == real_signal.EXECUTION_TERMS.flags


# ---------------------------------------------------------------- Signal and view

def test_signal_shares_the_data_the_parent_needs_and_the_known_differences_are_the_listed_ones():
    real_fields = {f.name for f in dataclasses.fields(real_signal.Signal)}
    shared = {"source", "value", "abstained", "criteria", "reason"}
    assert shared <= real_fields and shared <= {f.name for f in dataclasses.fields(user_api.Signal)}
    assert {"missing"} <= {f.name for f in dataclasses.fields(user_api.Signal)} and "data_missing" in real_fields      # same thing under two names
    assert real_fields - {f.name for f in dataclasses.fields(user_api.Signal)} - {"data_missing"} == KNOWN_DIFFERENCES["Signal fields only in the real type"]
    assert (real_signal.MIN_SENTENCES, real_signal.MAX_SENTENCES) == KNOWN_DIFFERENCES["reason sentences"]


@pytest.mark.parametrize("reason", ["The close broke the high of the inside bar. The range was tight.", "one sentence only", "stop at the low. Then exit.",
                                    "position sizing is large. Take it.", ""])
@pytest.mark.parametrize("source", ["u_inside_bar", "u_stop_run", "u_x"])
def test_a_view_of_the_copy_that_speaks_of_execution_is_refused_where_the_real_one_would(reason, source):
    ctx = user_api.SignalContext(source="rule", symbol="NQ", as_of="2026-01-02T15:00:00+00:00", facts={})
    made, _ = outcome(lambda: user_api.view(ctx, source, 0.5, (), reason))
    if made is None:
        return
    sentences = real_signal.count_sentences(reason)
    if real_signal.MIN_SENTENCES <= sentences <= real_signal.MAX_SENTENCES:          # the sentence count is the parent's to arrange
        from datetime import datetime, timezone

        built, error = outcome(lambda: real_signal.Signal(source=made.source, desk_id=1, line=real_signal.LINES[0], symbol="NQ", as_of=datetime(2026, 1, 2, tzinfo=timezone.utc),
                                                          horizon=real_signal.HORIZONS[0], value=made.value, confidence=50, abstained=False, criteria=(), data_missing=(),
                                                          reason=made.reason, snapshot_hash="h"))
        assert error is None, f"accepted by the copy, refused by the real Signal: {error}"


# ---------------------------------------------------------------- IndicatorRequest and the helpers

def test_indicator_request_has_the_same_fields_defaults_and_the_names_of_the_library():
    assert field_shape(user_api.IndicatorRequest) == field_shape(strategy.IndicatorRequest)
    assert annotations(user_api.IndicatorRequest) == annotations(strategy.IndicatorRequest)
    assert set(user_api.INDICATORS) == set(indicator_library.LIBRARY)


@pytest.mark.parametrize("kind", ["ema", "sma", "atr", "rsi", "os", "", "EMA"])
@pytest.mark.parametrize("params", [(), (("length", 9),), (("length", True),), (("length", 1.5),), tuple((f"p{i}", i) for i in range(9))])
@pytest.mark.parametrize("warmup", [1, 5000, 5001, 0, True, "3"])
def test_an_indicator_request_of_the_copy_builds_the_real_one_and_the_library_has_it(kind, params, warmup):
    made, _ = outcome(lambda: user_api.IndicatorRequest("fast", kind, "close", params, warmup))
    if made is None:
        return
    data = made.to_data()
    real = strategy.IndicatorRequest(name=data["name"], kind=data["kind"], source=data["source"], params=tuple((k, v) for k, v in data["params"]),
                                     warmup=data["warmup"])
    assert dataclasses.asdict(real) == dataclasses.asdict(made) and data["kind"] in indicator_library.LIBRARY


def test_fact_and_clip_give_what_the_factory_helpers_give():
    for value in (-5, -1, 0, 0.3, 1, 5, 1e300):
        assert user_api.clip(value) == real_base.clip(value)
    facts = {"a": [1.0, 2.0, 3.0], "b": [1.0, float("nan"), 3.0], "c": [float("nan"), 2.0, 3.0], "d": [[1.0]], "e": ["x"], "f": [], "g": None}
    ctx_copy = user_api.SignalContext(source="s", symbol="NQ", as_of="x", facts=facts)
    ctx_real = type("C", (), {"facts": facts})()
    for name in ("a", "b", "c", "d", "e", "f", "g", "missing"):
        for minimum in (1, 2, 3, 4):
            got, want = user_api.fact(ctx_copy, name, minimum), real_base.fact(ctx_real, name, minimum)
            assert (got is None) == (want is None) and (got is None or np.array_equal(got, want, equal_nan=True)), (name, minimum)
