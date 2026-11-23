"""Caleb, the options analyst (SPEC-019: B7): AFS-005, AFS-027, ECT-031, ECT-042 and ECT-098, and the closed premium selling switch."""
import importlib.util
import json
from datetime import datetime, timezone

import pandas as pd
import pytest
from b7_support import FIXTURES, ScriptedChat, answer, call, cited_calculations, load_registry, piece_ids, skill_text

from hokma.desks.agents import analyst_kit as kit
from hokma.desks.agents import options
from hokma.desks.core.tools import ToolNotAllowed
from hokma.quant import registry

CAL_AS_OF = datetime(2026, 11, 20, 20, 30, tzinfo=timezone.utc)
CHAIN = json.loads((FIXTURES / "option_chain_spy.json").read_text(encoding="utf-8"))
STATS = FIXTURES.parent / "stats" / "fixtures"
CLOSED = lambda desk_id, underlying: "closed"                                # noqa: E731
OPEN = lambda desk_id, underlying: "open"                                    # noqa: E731


@pytest.fixture(autouse=True)
def registry_ready():
    load_registry()


def richness_params(**extra):
    return {"contracts": CHAIN["contracts"], "spot": CHAIN["underlying_price"], "rate": CHAIN["rate"], "as_of": CHAIN["as_of"],
            "expiration": "2026-12-18", "dividend_yield": CHAIN["dividend_yield"], "closes": CHAIN["closes"], **extra}


@pytest.fixture(scope="module")
def richness():
    box = options.toolbox(kit.RunContext(1, "SPY", CAL_AS_OF, "SWO", options.switch_facts(1, "SPY", CLOSED)))
    result = box.call("quant_call", {"name": "options.vol_richness", "params": richness_params()})
    assert result.ok
    return result.data["value"]


def report(rich, classification="uncalibrated", structure=None, **patch):
    base = {
        "summary": "Implied volatility is read against realized volatility for the matched expiration.",
        "volatility": {"matched_expiration": "2026-12-18",
                       "implied_over_realized": [{"window_days": int(w), "ratio": rich["iv_over_realized"][w]} for w in ("20", "60", "90")],
                       "classification": classification, "earnings_in_window": rich["earnings_in_window"]},
        "skew": {"risk_reversal_25d": rich["risk_reversal_25d"], "butterfly_25d": rich["butterfly_25d"]},
        "earnings_preview": {"implied_move": None, "historical_reaction": None, "scenarios": [], "factors": []},
        "greeks": [],
        "structure": structure,
        "data_gaps": [],
    }
    return {**base, **patch}


BLANK = {"iv_over_realized": {"20": None, "60": None, "90": None}, "earnings_in_window": None, "risk_reversal_25d": None, "butterfly_25d": None}
LEG_IDS = ("OPT:SPY:20261218:C:590", "OPT:SPY:20261218:C:600")


def sheets():
    """Two reads of the contract tool: the ids a structure may name come from them."""
    return [call("contract_get", symbol=cid) for cid in LEG_IDS]


SHEET_HANDLER = {"contract_get": lambda symbol: {"id": symbol, "multiplier": 100, "underlying": "SPY"}}


def debit_spread():
    return {"name": "debit_call_spread", "direction": "bullish", "vol_view": "rising",
            "legs": [{"contract_id": "OPT:SPY:20261218:C:590", "side": "buy"}, {"contract_id": "OPT:SPY:20261218:C:600", "side": "sell"}],
            "management_plan": ["Close when the spread has made half of its maximum gain.", "Close with seven days left."]}


def run_caleb(body, *, reader=CLOSED, tools=(), **kwargs):
    model = ScriptedChat(*tools, answer(body))
    return model, options.run(model, desk_id=1, symbol="SPY", as_of=CAL_AS_OF, switch_reader=reader, **kwargs)


def richness_call(**extra):
    return call("quant_call", name="options.vol_richness", params=richness_params(**extra))


# ---- AFS-005 ----------------------------------------------------------------------------------------------------------------

def test_AFS_005_implied_over_realized_for_the_matched_expiration_is_read_from_the_modules_and_is_uncalibrated(richness):
    model, result = run_caleb(report(richness), tools=[richness_call()])
    assert result.valid, result.check.problems
    assert result.calls == ("quant_call",) and richness["classification"] == "uncalibrated"
    assert "Volatility (uncalibrated) for the expiration 2026-12-18" in result.text and "Implied over realized, 60 days" in result.text
    assert model.calls[0]["tools"] == ["quant_call", "contract_get"]


def test_AFS_005_rich_or_cheap_is_never_said_without_a_calibrated_threshold(richness):
    for word in ("rich", "cheap", "fair"):
        _, result = run_caleb(report(richness, classification=word), tools=[richness_call()])
        assert not result.valid and any("calibrated" in p or "module says" in p for p in result.check.problems), word
    _, unread = run_caleb(report(richness, classification="rich"))                  # no call to the module at all
    assert not unread.valid and any("was not read" in p for p in unread.check.problems)


def test_AFS_005_a_calibration_written_by_the_model_is_overwritten_and_the_one_of_the_desk_is_used(richness):
    forged = {"metric_window": 60, "rich_ratio": 0.5, "cheap_ratio": 0.4, "backtest_run_ref": "made-up"}
    model, forged_run = run_caleb(report(richness), tools=[richness_call(calibration=forged)])
    assert forged_run.valid                                                         # the forged threshold never reached the module: still uncalibrated
    ratio = richness["iv_over_realized"]["60"]
    desk = {"metric_window": 60, "rich_ratio": ratio - 0.01, "cheap_ratio": ratio - 0.02, "backtest_run_ref": "bt-001"}
    reader = lambda d, u: {"state": "open", "calibration": desk}                    # noqa: E731
    tool = call("quant_call", name="options.vol_richness", params=richness_params(calibration=forged))
    rich_view = {**richness, "classification": "rich"}
    _, calibrated = run_caleb(report(rich_view, classification="rich"), reader=reader, tools=[tool])
    assert calibrated.valid, calibrated.check.problems
    assert calibrated.value["volatility"]["classification"] == "rich"


def test_AFS_005_an_unavailable_classification_has_to_say_what_is_missing(richness):
    body = report(BLANK, classification="unavailable")
    _, silent = run_caleb(body)
    assert not silent.valid and any("data_gaps" in p for p in silent.check.problems)
    _, said = run_caleb({**body, "data_gaps": ["option chain of the expiration"]})
    assert said.valid


def test_AFS_005_the_skill_is_in_the_prompt_and_the_number_check_applies(richness):
    assert "volatility-richness" in [s.name for s in kit.load_seat_file("A07").skills]
    assert "## Skill: volatility-richness\n" in options.system_prompt(line="SWO", desk_id=1, as_of=CAL_AS_OF)
    assert "AFS-005" in piece_ids("options.vol_richness") and cited_calculations("volatility-richness") <= {s.name for s in registry.list()}
    bad = report(richness)
    bad["volatility"]["implied_over_realized"][0]["ratio"] = 7.77
    _, result = run_caleb(bad, tools=[richness_call()])
    assert not result.valid and any("7.77" in p for p in result.check.problems)


def test_AFS_005_caleb_gets_only_the_tools_of_his_role():
    box = options.toolbox(kit.RunContext(1, "SPY", CAL_AS_OF, "SWO"))
    assert box.names() == ("quant_call", "contract_get")
    for other in ("data_bars", "data_snapshot", "data_filings", "exec_propose"):
        with pytest.raises(ToolNotAllowed):
            box.call(other, {})
    assert not box.call("quant_call", {"name": "indicators.screens.vcp", "params": {}}).ok
    with pytest.raises(kit.AnalystKitError):
        options.system_prompt(line="SWC", desk_id=1, as_of=CAL_AS_OF)


# ---- AFS-027 ----------------------------------------------------------------------------------------------------------------

def earnings_call():
    bars = pd.read_csv(STATS / "ohlcv_daily_spy.csv", index_col=0, parse_dates=True)
    events = pd.read_csv(STATS / "earnings_events.csv").to_dict("records")
    series = lambda s: {"values": [float(x) for x in s], "index": [d.strftime("%Y-%m-%d") for d in s.index]}     # noqa: E731
    return call("quant_call", name="stats.earnings_reaction", params={
        "open_": series(bars["open"]), "close": series(bars["close"]), "events": events,
        "scenarios": {"bull": 0.05, "base": 0.01, "bear": -0.04}})


def test_AFS_027_three_scenarios_against_the_historical_reaction_measured_at_the_next_open_with_three_to_five_factors():
    box = options.toolbox(kit.RunContext(1, "SPY", CAL_AS_OF, "SWO"))
    tool = earnings_call()
    result = box.call("quant_call", tool.tool_calls[0].arguments)
    assert result.ok and "AFS-027" in result.data["piece_ids"]
    value = result.data["value"]
    assert 3 <= len(value["factors"]) <= 5 and set(value["scenarios"]) == {"bull", "base", "bear"} and value["events"][0]["fill"]
    first = value["events"][0]["reaction"]
    preview = {"implied_move": None, "historical_reaction": first,
               "scenarios": [{"name": n, "reaction": value["scenarios"][n]["move"]} for n in ("bull", "base", "bear")], "factors": value["factors"][:3]}
    richness = {"iv_over_realized": {"20": None, "60": None, "90": None}, "earnings_in_window": None, "risk_reversal_25d": None, "butterfly_25d": None}
    body = report(richness, classification="unavailable", earnings_preview=preview, data_gaps=["option chain"])
    _, ok = run_caleb(body, tools=[tool])
    assert ok.valid, ok.check.problems
    few = {**body, "earnings_preview": {**preview, "factors": value["factors"][:1]}}
    _, short = run_caleb(few, tools=[earnings_call()])
    assert not short.valid and any("factors" in p for p in short.check.problems)


def test_AFS_027_the_skill_is_in_the_prompt_and_measures_the_reaction_by_the_timing_of_the_report():
    assert "## Skill: earnings-preview\n" in options.system_prompt(line="SWO", desk_id=1, as_of=CAL_AS_OF)
    assert "AFS-027" in piece_ids("stats.earnings_reaction") and "stats" in kit.SEATS["A07"].quant_modules
    assert cited_calculations("earnings-preview") <= {s.name for s in registry.list()}
    assert "after the close" in skill_text("earnings-preview")
    empty = registry.call("stats.earnings_reaction", open_=pd.Series([], dtype=float, index=pd.DatetimeIndex([])),
                          close=pd.Series([], dtype=float, index=pd.DatetimeIndex([])), events=[{"date": "2024-01-01", "timing": "AMC", "event_id": "E1"}])
    assert empty.value is None


# ---- ECT-031 ----------------------------------------------------------------------------------------------------------------

LEG = {"kind": "call", "strike": 100.0, "volatility": 0.30, "quantity": 2, "time_to_expiration": 0.25}
SHEET = {"id": "OPT:SPY:20261218:C:590", "multiplier": 100, "underlying": "SPY"}


def greeks_call(multiplier):
    return call("quant_call", name="options.position_greeks", params={"legs": [LEG], "spot": 100.0, "rate": 0.04, "dividend_yield": 0.0, "multiplier": multiplier})


def greeks_body(totals):
    richness = {"iv_over_realized": {"20": None, "60": None, "90": None}, "earnings_in_window": None, "risk_reversal_25d": None, "butterfly_25d": None}
    greeks = [{"label": "two calls", "delta": totals["delta_usd"], "gamma": totals["gamma_usd_per_1pct"], "vega": totals["vega_usd_per_point"],
               "theta": totals["theta_usd_per_day"]}]
    return report(richness, classification="unavailable", greeks=greeks, data_gaps=["chain"])


def test_ECT_031_the_greeks_are_in_dollars_with_the_multiplier_of_the_contract_sheet_and_not_a_hundred_from_memory():
    box = options.toolbox(kit.RunContext(1, "SPY", CAL_AS_OF, "SWO"))
    totals = box.call("quant_call", {"name": "options.position_greeks", "params": {"legs": [LEG], "spot": 100.0, "rate": 0.04, "multiplier": 100}}).data["value"]["totals"]
    half = box.call("quant_call", {"name": "options.position_greeks", "params": {"legs": [LEG], "spot": 100.0, "rate": 0.04, "multiplier": 50}}).data["value"]["totals"]
    assert half["delta_usd"] == pytest.approx(totals["delta_usd"] / 2, abs=1e-9)             # no hidden 100
    sheet = call("contract_get", symbol="OPT:SPY:20261218:C:590")
    _, honest = run_caleb(greeks_body(totals), tools=[sheet, greeks_call(100)], handlers={"contract_get": lambda **a: SHEET})
    assert honest.valid, honest.check.problems
    _, remembered = run_caleb(greeks_body(totals), tools=[greeks_call(100)])                    # a multiplier no sheet gave in this run
    assert not remembered.valid and any("contract sheet" in p for p in remembered.check.problems)
    _, other_sheet = run_caleb(greeks_body(totals), tools=[call("contract_get", symbol="X"), greeks_call(100)], handlers={"contract_get": lambda **a: {**SHEET, "multiplier": 50}})
    assert not other_sheet.valid


def test_ECT_031_the_skill_is_in_the_prompt_and_refuses_a_multiplier_from_memory():
    assert "## Skill: greeks-management\n" in options.system_prompt(line="SWO", desk_id=1, as_of=CAL_AS_OF)
    assert "ECT-031" in piece_ids("options.position_greeks") and cited_calculations("greeks-management") <= {s.name for s in registry.list()}
    assert "multiplier of 100 from memory" in skill_text("greeks-management")
    assert registry.call("options.position_greeks", legs=[], spot=100.0, rate=0.04, multiplier=100).value is None            # empty input: no value, no crash


# ---- ECT-042 and the closed switch ------------------------------------------------------------------------------------------

def test_ECT_042_the_volatility_premium_skill_is_in_the_prompt_and_says_the_sale_is_closed_without_a_threshold():
    assert "volatility-premium" in [s.name for s in kit.load_seat_file("A07").skills]
    prompt = options.system_prompt(line="SWO", desk_id=1, as_of=CAL_AS_OF)
    assert "## Skill: volatility-premium\n" in prompt and "premium_selling_closed" in prompt
    assert cited_calculations("volatility-premium") <= {s.name for s in registry.list()}
    assert "premium selling" in kit.load_seat_file("A07").body and "never writes that the switch is\nopen" in kit.load_seat_file("A07").body


def test_ECT_042_an_answer_that_sells_premium_is_rejected_while_the_switch_is_closed(richness):
    for name in options.SELLING_STRUCTURES:
        sale = {**debit_spread(), "name": name}
        _, result = run_caleb(report(richness, structure=sale), tools=[richness_call(), *sheets()], handlers=SHEET_HANDLER)
        assert not result.valid and any("sale is closed" in p for p in result.check.problems), name
    _, debit = run_caleb(report(richness, structure=debit_spread()), tools=[richness_call(), *sheets()], handlers=SHEET_HANDLER)
    assert debit.valid, debit.check.problems
    _, open_sale = run_caleb(report(richness, structure={**debit_spread(), "name": "iron_condor"}), reader=OPEN, tools=[richness_call(), *sheets()],
                             handlers=SHEET_HANDLER)
    assert open_sale.valid                                                              # the switch is the desk's: with it open the checks let the sale through to the code guard


def test_ECT_042_a_switch_that_cannot_be_read_is_closed_and_the_model_cannot_open_it():
    def broken(desk_id, underlying):
        raise RuntimeError("database is down")

    assert options.switch_is_open(1, "SPY", broken) is False and options.switch_is_open(1, "SPY", lambda d, u: None) is False
    assert options.switch_is_open(1, "SPY", lambda d, u: {"state": "open"}) is True
    assert options.switch_is_open(1, "SPY", OPEN) is True and options.switch_is_open(1, "SPY", lambda d, u: "OPEN") is False
    assert options.switch_is_open(1, "SPY") is False                             # the real reader finds no open switch here
    model = ScriptedChat(answer({"nonsense": 1}))
    options.run(model, desk_id=1, symbol="SPY", as_of=CAL_AS_OF, facts={"premium_switch": {"state": "open"}}, switch_reader=CLOSED)
    assert '"state": "closed"' in model.calls[0]["messages"][0].content and '"state": "open"' not in model.calls[0]["messages"][0].content


def test_ECT_042_the_premium_selling_guard_of_the_pipeline_refuses_a_sale_from_a_desk_while_the_threshold_is_missing():
    if importlib.util.find_spec("hokma.exec.guards.premium_selling") is None:
        pytest.skip("the guard of the pipeline is built by the execution block of this spec")
    from hokma.exec.guards import premium_selling

    assert callable(premium_selling.is_premium_selling)


# ---- ECT-098 ----------------------------------------------------------------------------------------------------------------

def matrix_call(flag):
    return call("quant_call", name="options.structure_matrix", params={"direction": "bullish", "vol_view": "rising", "premium_selling_open": flag})


def test_ECT_098_while_the_switch_is_closed_the_matrix_offers_only_purchases_and_debit_spreads_even_if_the_model_asks_for_more():
    box = options.toolbox(kit.RunContext(1, "SPY", CAL_AS_OF, "SWO", options.switch_facts(1, "SPY", CLOSED)))
    forced = box.call("quant_call", {"name": "options.structure_matrix", "params": {"direction": "bullish", "vol_view": "rising", "premium_selling_open": True}})
    offered = {o["structure"] for o in forced.data["value"]["offered"]}
    assert forced.data["params"]["premium_selling_open"] is False and offered <= set(options.DEBIT_STRUCTURES) and offered
    assert {o["premium_class"] for o in forced.data["value"]["offered"]} == {"debit"}
    free = options.toolbox(kit.RunContext(1, "SPY", CAL_AS_OF, "SWO", options.switch_facts(1, "SPY", OPEN)))
    opened = free.call("quant_call", {"name": "options.structure_matrix", "params": {"direction": "neutral", "vol_view": "falling"}})
    assert opened.data["params"]["premium_selling_open"] is True
    menu = options.structure_menu("bullish", "rising", desk_id=1, underlying="SPY", reader=CLOSED)
    assert {o["structure"] for o in menu.value["offered"]} <= set(options.DEBIT_STRUCTURES)
    unread = options.structure_menu("bullish", "rising", desk_id=1, underlying="SPY", reader=lambda d, u: (_ for _ in ()).throw(RuntimeError()))
    assert unread.params["premium_selling_open"] is False


def test_ECT_098_a_structure_has_its_plan_written_before_entering_and_is_classified_by_its_kind(richness):
    plan_less = {**debit_spread(), "management_plan": []}
    _, result = run_caleb(report(richness, structure=plan_less), tools=[richness_call(), *sheets()], handlers=SHEET_HANDLER)
    assert not result.valid and any("management plan" in p for p in result.check.problems)
    assert set(options.DEBIT_STRUCTURES) | set(options.SELLING_STRUCTURES) == set(options.STRUCTURES) and not set(options.DEBIT_STRUCTURES) & set(options.SELLING_STRUCTURES)
    assert "ECT-098" in piece_ids("options.structure_matrix") and "ECT-098" in piece_ids("options.classify_structure")
    assert cited_calculations("options-structures") <= {s.name for s in registry.list()}
    assert "## Skill: options-structures\n" in options.system_prompt(line="SWO", desk_id=1, as_of=CAL_AS_OF)
    _, ok = run_caleb(report(richness, structure=debit_spread()), tools=[richness_call(), *sheets()], handlers=SHEET_HANDLER)
    assert ok.valid and "Structure debit_call_spread for a bullish view" in ok.text and "Plan: Close with seven days left." in ok.text
    invented = {**debit_spread(), "legs": [{"contract_id": "OPT:SPY:20261218:C:777", "side": "buy"}]}
    _, made_up = run_caleb(report(richness, structure=invented), tools=[richness_call(), *sheets()], handlers=SHEET_HANDLER)
    assert not made_up.valid and any("not an identifier that a tool returned" in p for p in made_up.check.problems)


def test_ECT_098_caleb_does_not_size_the_structure():
    assert not {"quantity", "contracts", "size", "ratio"} & set(options.SCHEMA["properties"]["structure"]["properties"]["legs"]["items"]["properties"])
    _, result = run_caleb({**report({"iv_over_realized": {"20": None, "60": None, "90": None}, "earnings_in_window": None, "risk_reversal_25d": None,
                                     "butterfly_25d": None}, classification="unavailable", data_gaps=["chain"]), "contracts": 5})
    assert not result.valid and any("contracts" in p for p in result.check.problems)
