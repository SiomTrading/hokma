"""Signal contract (SPEC-017 section 4.8): AH-01, AH-02, AH-04 and the eight invariants."""
import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest

from hokma.desks.core.signal import (
    AlphaModel, Criterion, QuantModel, Signal, SignalContext, contains_execution_terms, count_sentences,
)

T0 = datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc)
REASON = "Returns on equity stayed above the threshold for ten years. Debt is falling."


def make(**over):
    base = dict(source="A16", desk_id=7, line="LP", symbol="AAPL", as_of=T0, horizon="years", value=0.4, confidence=60,
                abstained=False, criteria=(Criterion("roe_10y_min", 0.21, 0.15, True, "metrics.mean"),), data_missing=(),
                reason=REASON, snapshot_hash="abc123")
    base.update(over)
    return Signal(**base)


def test_protocols_are_runtime_checkable_AH_01():
    class Good:
        def signal(self, ctx):
            return None

    class Bad:
        pass

    assert isinstance(Good(), AlphaModel) and isinstance(Good(), QuantModel)
    assert not isinstance(Bad(), AlphaModel) and not isinstance(Bad(), QuantModel)


def test_invariant_1_abstained_iff_value_none_AH_02():
    assert make().abstained is False
    with pytest.raises(ValueError):
        make(value=None, abstained=False)
    with pytest.raises(ValueError):
        make(value=0.2, abstained=True)
    s = make(value=None, abstained=True, confidence=0)
    assert s.value is None and s.abstained


@pytest.mark.parametrize("value", [1.0001, -1.5, float("nan"), float("inf")])
def test_invariant_2_value_in_range_AH_02(value):
    with pytest.raises(ValueError):
        make(value=value)


def test_invariant_2_edges_are_valid_AH_02():
    assert make(value=1).value == 1.0 and make(value=-1.0).value == -1.0 and make(value=0.0).value == 0.0


def test_invariant_3_confidence_range_and_profile_cap_AH_02():
    for bad in (-1, 101, 55.5, True):
        with pytest.raises(ValueError):
            make(confidence=bad)
    assert make(confidence=69, confidence_cap=69).confidence == 69
    with pytest.raises(ValueError, match="cap"):
        make(confidence=70, confidence_cap=69)


def test_invariant_4_required_criterion_without_value_abstains_AH_04():
    gap = Criterion("fcf_yield", None, 0.04, None, "data.fundamentals")
    s = make(criteria=(gap,), required=("fcf_yield",))
    assert s.abstained and s.value is None and s.data_missing == ("fcf_yield",)
    absent = make(criteria=(), required=("roe_10y_min",))
    assert absent.abstained and "roe_10y_min" in absent.data_missing
    ok = make(required=("roe_10y_min",))
    assert not ok.abstained


def test_abstain_propagates_the_missing_input_AH_04():
    s = Signal.abstain("A16", 7, "LP", "AAPL", T0, "years", ["fcf_yield"], "The cash flow series is not available. No view is formed.", "h1")
    assert s.abstained is True and s.value is None and s.confidence == 0
    assert s.data_missing == ("fcf_yield",)


def test_context_abstain_lists_required_gaps_AH_04():
    ctx = SignalContext("A16", 7, "LP", "AAPL", T0, "years", "h1", required=("roe_10y_min", "fcf_yield"),
                        criteria=(Criterion("roe_10y_min", 0.2, 0.15, True, "metrics.mean"),))
    assert ctx.missing_required() == ("fcf_yield",)
    s = ctx.abstain("An input is missing. No view is formed.", ctx.missing_required())
    assert s.abstained and s.data_missing == ("fcf_yield",)


@pytest.mark.parametrize("reason,count", [("One sentence only.", 1), ("", 0), ("A. B. C. D. E.", 5)])
def test_invariant_5_reason_needs_two_to_four_sentences(reason, count):
    assert count_sentences(reason) == count
    with pytest.raises(ValueError, match="sentences"):
        make(reason=reason)


def test_invariant_5_edges_are_valid():
    assert make(reason="Two ideas. Here.").reason
    assert make(reason="One. Two. Three. Four.").reason
    assert count_sentences("The ratio is 0.65 today. It was lower last year.") == 2


@pytest.mark.parametrize("text", [
    "Use a position size of two percent. The view is positive.",
    "Place a stop below the last low. The view is positive.",
    "The price target is high. The view is positive.",
    "Enter now while it is cheap. The view is positive.",
    "Take profit near the highs. The view is positive.",
    "El tama\u00f1o de posici\u00f3n debe ser bajo. La vista es positiva.",
])
def test_invariant_6_no_sizing_exit_or_timing_text(text):
    assert contains_execution_terms(text)
    with pytest.raises(ValueError, match="view only"):
        make(reason=text)


def test_invariant_6_contract_has_no_size_stop_target_fields_and_rejects_extra_keys():
    names = {f.name for f in dataclasses.fields(Signal)}
    assert not names & {"size", "position_size", "stop", "stop_loss", "target", "take_profit", "entry", "entry_time"}
    data = make().to_dict()
    for key in ("size", "stop", "target", "entry"):
        with pytest.raises(ValueError, match="unknown"):
            Signal.from_dict({**data, key: 1})
    with pytest.raises(TypeError):
        Signal(size=1, **dataclasses.asdict(make()))   # not a field of the contract


def test_invariant_6_criterion_names_are_checked_too():
    with pytest.raises(ValueError):
        Criterion("stop_loss pct", 1.0, None, None, "x")


def test_invariant_7_as_of_not_after_the_snapshot_AH_02():
    assert make(snapshot_at=T0).as_of == T0
    assert make(snapshot_at=T0 + timedelta(hours=1))
    with pytest.raises(ValueError, match="lookahead"):
        make(snapshot_at=T0 - timedelta(seconds=1))
    with pytest.raises(ValueError, match="timezone"):
        make(as_of=datetime(2026, 1, 2, 15, 0))


def test_as_of_is_normalised_to_utc():
    tz = timezone(timedelta(hours=-5))
    s = make(as_of=datetime(2026, 1, 2, 10, 0, tzinfo=tz))
    assert s.as_of == T0 and s.as_of.utcoffset() == timedelta(0)


def test_invariant_8_json_round_trip_AH_02():
    for s in (make(), Signal.abstain("A22", 3, "SWO", "MSFT", T0, "days", ["iv_rank"], "No volatility data arrived. No view is formed.", "h2")):
        assert Signal.from_json(s.to_json()) == s
        json.loads(s.to_json())
    assert Signal.from_dict(make().to_dict()) == make()


def test_from_json_rejects_bad_payloads():
    with pytest.raises(ValueError):
        Signal.from_json("[1]")
    data = make().to_dict()
    del data["reason"]
    with pytest.raises(ValueError, match="missing"):
        Signal.from_dict(data)
    data = make().to_dict()
    data["value"] = 2.0
    with pytest.raises(ValueError):
        Signal.from_dict(data)


def test_criterion_rules():
    with pytest.raises(ValueError):
        Criterion("x", None, 1.0, True, "s")
    with pytest.raises(ValueError):
        Criterion("x", float("nan"), None, None, "s")
