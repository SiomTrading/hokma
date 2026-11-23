"""Mandate and analyst plan (SPEC-019: AH-25, AH-26, AH-29, TA-21, TA-28)."""
from pathlib import Path

import pytest

from hokma.desks.agents.manager import (
    ANALYSTS, AnalystPlanError, Mandate, analyst_plan, load_mandate, parse_mandate, select_analysts,
)
from hokma.desks.core.spec_loader import DeskSpec, SpecError
from hokma.desks.core.state import is_absent
from hokma.desks.plantel import seats_for

MANDATES = Path(__file__).resolve().parents[3] / "kit" / "template" / "desk" / "mandates"

BASE = {
    "name": "Options swing", "line": "SWO", "variant": "traditional",
    "strategies": [{"name": "quality", "horizon": "weeks", "models": [{"name": "A16", "weight": 2}, {"name": "A17"}]}],
}


def mandate(**extra):
    return {**BASE, **extra}


# ---- AH-25: typed mandate, extra keys rejected --------------------------------------------------------------------

def test_a_valid_mandate_loads_with_its_defaults_AH_25():
    m = parse_mandate(BASE)
    assert m.rebalance == "weekly" and m.risk.max_open_positions == 5 and m.asset_classes == ["stock", "etf", "option", "future"]
    assert m.strategies[0].blend.method == "conviction_weighted" and m.strategies[0].models[0].weight == 2.0


@pytest.mark.parametrize("patch,where", [
    ({"leverage": 3}, "leverage: unknown field"),
    ({"risk": {"max_open_positions": 3, "stop": 2}}, "risk.stop: unknown field"),
    ({"strategies": [{**BASE["strategies"][0], "stop": 1}]}, "strategies.0.stop: unknown field"),
])
def test_extra_keys_are_rejected_and_named_AH_25(patch, where):
    with pytest.raises(SpecError, match=where):
        parse_mandate(mandate(**patch))


@pytest.mark.parametrize("patch,word", [
    ({"line": "XX"}, "line"), ({"rebalance": "hourly"}, "rebalance"), ({"name": ""}, "name"),
    ({"risk": {"max_risk_per_trade_pct": 0}}, "max_risk_per_trade_pct"), ({"risk": {"max_open_positions": 0}}, "max_open_positions"),
    ({"risk": {"max_drawdown_pct": float("nan")}}, "max_drawdown_pct"), ({"variant": "other"}, "variant"),
    ({"strategies": []}, "strategies"), ({"asset_classes": []}, "asset_classes"), ({"asset_classes": ["stock", "stock"]}, "duplicate"),
    ({"notes": "x" * 501}, "notes"),
])
def test_out_of_domain_values_name_the_field_AH_25(patch, word):
    with pytest.raises(SpecError, match=word):
        parse_mandate(mandate(**patch))


def test_not_a_mapping_and_missing_files_fail_with_a_spec_error_AH_25(tmp_path):
    for bad in (None, [], "text", 3):
        with pytest.raises(SpecError):
            parse_mandate(bad)
    with pytest.raises(SpecError, match="cannot read"):
        load_mandate(tmp_path / "nope.yaml")
    (tmp_path / "list.yaml").write_text("- a\n- b\n")
    with pytest.raises(SpecError, match="mapping"):
        load_mandate(tmp_path / "list.yaml")


def test_models_must_be_seats_of_the_desk_and_not_repeat_AH_25():
    codes = {s.code for s in seats_for("SWO", "traditional")}
    assert "A23" not in codes
    with pytest.raises(SpecError, match="A23"):
        parse_mandate(mandate(strategies=[{"name": "s", "horizon": "weeks", "models": [{"name": "A23"}]}]))
    pead = parse_mandate(mandate(variant="pead", strategies=[{"name": "s", "horizon": "weeks", "models": [{"name": "A23"}]}]))
    assert pead.strategies[0].models[0].name == "A23"
    with pytest.raises(SpecError, match="duplicate model"):
        parse_mandate(mandate(strategies=[{"name": "s", "horizon": "weeks", "models": [{"name": "A16"}, {"name": "A16"}]}]))
    with pytest.raises(SpecError, match="duplicate strategy"):
        parse_mandate(mandate(strategies=BASE["strategies"] * 2))
    with pytest.raises(SpecError, match="weight"):
        parse_mandate(mandate(strategies=[{"name": "s", "horizon": "weeks", "models": [{"name": "A16", "weight": 0}]}]))


def test_the_mandate_round_trips_through_json_and_summarises_for_the_desk_row_AH_25():
    m = parse_mandate(BASE)
    assert Mandate.from_json(m.to_json()) == m
    text = m.summary()
    assert "SWO desk (traditional)" in text and "A16 x2" in text and len(text) <= 4000 and "\u2014" not in text


# ---- AH-26: no instruments in the mandate ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["universe", "symbols", "tickers", "assets", "underlying", "Universe"])
def test_a_mandate_with_instruments_fails_validation_AH_26(key):
    with pytest.raises(SpecError, match="names no instrument"):
        parse_mandate(mandate(**{key: ["AAPL", "MSFT"]}))
    strategy = {**BASE["strategies"][0], key: ["AAPL"]}
    with pytest.raises(SpecError, match="names no instrument"):
        parse_mandate(mandate(strategies=[strategy]))


def test_instruments_hidden_in_the_params_of_a_model_fail_too_AH_26():
    models = [{"name": "A16", "params": {"filter": {"symbols": ["AAPL"]}}}]
    with pytest.raises(SpecError, match="names no instrument"):
        parse_mandate(mandate(strategies=[{"name": "s", "horizon": "weeks", "models": models}]))


def test_the_universe_comes_in_as_an_argument_of_the_run_AH_26():
    m = parse_mandate(BASE)
    spec = m.to_desk_spec(["aapl", "msft", "AAPL"], capital=50_000)
    assert isinstance(spec, DeskSpec) and spec.strategies[0].universe == ["AAPL", "MSFT"]
    assert spec.capital == 50_000 and spec.name == "options-swing" and spec.strategies[0].line == "SWO"
    other = m.to_desk_spec(["ES", "NQ"], capital=1000)
    assert other.strategies[0].universe == ["ES", "NQ"]
    for bad in ([], ["bad symbol!"]):
        with pytest.raises(ValueError):
            m.to_desk_spec(bad, capital=1000)
    with pytest.raises(ValueError):
        m.to_desk_spec(["AAPL"], capital=0)


# ---- AH-29: example mandates ------------------------------------------------------------------------------------

@pytest.mark.parametrize("file,variant,drew", [("swo-traditional.yaml", "traditional", False), ("swo-pead.yaml", "pead", True)])
def test_the_example_mandates_load_in_english_and_name_no_instrument_AH_29(file, variant, drew):
    path = MANDATES / file
    text = path.read_text(encoding="utf-8")
    assert "\u2014" not in text and "\n" in text
    m = load_mandate(path)
    assert m.line == "SWO" and m.variant == variant and len(m.strategies) == 2
    used = {model.name for s in m.strategies for model in s.models}
    assert ("A23" in used) is drew
    assert used <= {s.code for s in seats_for("SWO", variant)}
    spec = m.to_desk_spec(["SPY"], capital=25_000)           # the example builds a valid core spec once a run gives the universe
    assert len(spec.strategies) == 2 and spec.rebalance == m.rebalance


def test_the_pead_example_declares_the_future_variant_AH_29():
    pead, traditional = load_mandate(MANDATES / "swo-pead.yaml"), load_mandate(MANDATES / "swo-traditional.yaml")
    assert "future" in pead.asset_classes and "future" not in traditional.asset_classes
    assert "weekly bar" in (pead.notes or "")


# ---- TA-21: plan table ------------------------------------------------------------------------------------------------

def test_the_plan_follows_the_order_of_the_mandate_TA_21():
    m = parse_mandate(mandate(analysts=[{"key": "options"}, {"key": "market"}, {"key": "news", "enabled": False}]))
    plan = analyst_plan(m)
    assert [(s.key, s.seat_code, s.enabled) for s in plan] == [("options", "A07", True), ("market", "A01", True), ("news", "A04", False)]


def test_a_mandate_without_a_list_runs_every_analyst_of_the_line_in_table_order_TA_21():
    assert [s.key for s in analyst_plan(parse_mandate(BASE))] == list(ANALYSTS)
    assert [s.key for s in analyst_plan(None, line="DT")] == ["market", "sentiment", "news"]
    assert [s.key for s in analyst_plan(["news", "market"], line="SWO")] == ["news", "market"]


def test_a_key_that_does_not_exist_fails_when_the_mandate_loads_TA_21():
    with pytest.raises(SpecError, match="unknown analyst 'macro'"):
        parse_mandate(mandate(analysts=[{"key": "macro"}]))
    with pytest.raises(AnalystPlanError, match="unknown analyst"):
        analyst_plan(["market", "macro"])
    with pytest.raises(AnalystPlanError, match="twice"):
        analyst_plan(["market", "market"])
    with pytest.raises(SpecError, match="duplicate keys"):
        parse_mandate(mandate(analysts=[{"key": "market"}, {"key": "market"}]))


def test_an_analyst_the_line_does_not_carry_fails_TA_21():
    with pytest.raises(AnalystPlanError, match="not a seat of a DT desk"):
        analyst_plan(["fundamentals"], line="DT")
    with pytest.raises(SpecError, match="not a seat"):
        parse_mandate({**BASE, "line": "DT", "variant": None, "analysts": [{"key": "options"}],
                       "strategies": [{"name": "s", "horizon": "days", "models": [{"name": "A20"}]}]})


# ---- TA-28: selection per run -----------------------------------------------------------------------------------------

def test_turning_an_analyst_off_takes_it_out_of_the_graph_and_leaves_its_report_absent_TA_28():
    plan = analyst_plan(None)
    chosen = select_analysts(plan, off=["sentiment", "news"])
    assert chosen.run == ("market", "fundamentals", "options") and chosen.off == ("sentiment", "news")
    assert set(chosen.reports) == {"sentiment", "news"} and all(is_absent(r) for r in chosen.reports.values())
    assert chosen.reports["news"]["source"] == "news" and "not an empty finding" in chosen.reports["news"]["text"]


def test_the_plan_of_the_mandate_decides_which_are_off_TA_28():
    m = parse_mandate(mandate(analysts=[{"key": "market"}, {"key": "options", "enabled": False}]))
    chosen = select_analysts(analyst_plan(m))
    assert chosen.run == ("market",) and chosen.off == ("options",) and is_absent(chosen.reports["options"])
    with pytest.raises(AnalystPlanError, match="at least one"):
        select_analysts(analyst_plan(m), off=["market"])


def test_nothing_off_runs_everything_and_bad_requests_fail_TA_28():
    plan = analyst_plan(None)
    chosen = select_analysts(plan)
    assert chosen.run == tuple(ANALYSTS) and chosen.off == () and dict(chosen.reports) == {}
    with pytest.raises(AnalystPlanError, match="not in the plan"):
        select_analysts(plan, off=["macro"])
    with pytest.raises(AnalystPlanError, match="at least one"):
        select_analysts(plan, off=list(ANALYSTS))
    with pytest.raises(AnalystPlanError, match="at least one"):
        select_analysts(())
    with pytest.raises(SpecError, match="at least one"):
        parse_mandate(mandate(analysts=[{"key": "market", "enabled": False}]))
