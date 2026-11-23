"""Adam, the market analyst (SPEC-019: B7): TA-01 and the screens he reads, CTS-004, CTS-005, CTS-020 and CTS-028."""
import json

import pytest
from b7_support import AS_OF, ScriptedChat, answer, call, cited_calculations, frame_payload, load_registry, piece_ids, skill_text, spy_bars

from hokma.desks.agents import analyst_kit as kit
from hokma.desks.agents import market
from hokma.desks.core.tools import ToolNotAllowed
from hokma.quant import registry

WEEKLY = "indicators.screens.weekly_structure"


@pytest.fixture(autouse=True)
def registry_ready():
    load_registry()


@pytest.fixture(scope="module")
def bars():
    return spy_bars().loc[:"2024-06-14"].tail(400)


@pytest.fixture
def weekly(bars):
    """The real weekly structure of the fixture bars, read through Adam's own toolbox."""
    result = market.toolbox(kit.RunContext(1, "SPY", AS_OF, "SWO")).call(
        "quant_call", {"name": WEEKLY, "params": {"bars": frame_payload(bars), "as_of": "2024-06-14"}})
    assert result.ok
    return result.data["value"]


def fake_bars(bars):
    rows = [{"date": d.strftime("%Y-%m-%d"), "close": float(r.close)} for d, r in bars.tail(5).iterrows()]
    return lambda **args: {"value": rows, "source": "recorded", "as_of": args["as_of"]}


def report(weekly):
    scenarios = weekly["scenarios"]
    return {
        "summary": "Weekly structure read on closed bars.",
        "trend": {"daily": "range", "weekly": "range"},
        "levels": [{"kind": "support", "name": "weekly support", "value": weekly["supports"][0], "source": WEEKLY},
                   {"kind": "resistance", "name": "weekly resistance", "value": weekly["resistances"][0], "source": WEEKLY}],
        "scenarios": [{"name": s["name"], "reading": "Read from the weekly structure.", "invalidation_level": s["invalidation"],
                       "invalidation_text": "the weekly close goes through the level"} for s in scenarios[:3]],
        "screens": [{"name": "weekly structure", "state": weekly["confirmation"]["verdict"]}],
        "data_gaps": [],
    }


def run_adam(bars, weekly, body, **kwargs):
    model = ScriptedChat(
        call("data_bars", symbol="SPY", interval="1D", start="2023-11-01", end="2024-06-14", as_of="2024-06-14"),
        call("quant_call", name=WEEKLY, params={"bars": frame_payload(bars), "as_of": "2024-06-14"}),
        answer(body))
    result = market.run(model, desk_id=1, symbol="spy", as_of=AS_OF, handlers={"data_bars": fake_bars(bars)}, **kwargs)
    return model, result


def test_TA_01_the_report_has_levels_and_scenarios_with_invalidation_anchored_to_the_evidence(bars, weekly):
    model, result = run_adam(bars, weekly, report(weekly))
    assert result.valid, result.check.problems
    assert result.calls == ("data_bars", "quant_call")
    texts = " ".join(result.report.findings)
    assert str(weekly["supports"][0]) in texts and str(weekly["scenarios"][0]["invalidation"]) in texts and "[unverified]" not in texts
    assert 2 <= len(result.value["scenarios"]) <= 4 and all("invalidation_level" in s for s in result.value["scenarios"])
    first = model.calls[0]
    assert first["tools"] == ["data_bars", "data_snapshot", "quant_call"] and "Line SWO" in first["system"]
    assert "Symbol: SPY" in first["messages"][0].content and "2024-06-14T20:00:00" in first["messages"][0].content


def test_TA_01_the_answer_has_no_order_size_or_stop_field_and_the_bar_price_is_never_an_entry(bars, weekly):
    for extra in ({"stop": 1.0}, {"entry_price": weekly["close"]}, {"order": {"side": "buy"}}):
        _, result = run_adam(bars, weekly, {**report(weekly), **extra})
        key = next(iter(extra))
        assert not result.valid and any(key in p for p in result.check.problems), key
    entry_level = {**report(weekly), "levels": [{"kind": "entry", "name": "bar close", "value": weekly["close"], "source": "bar"}]}
    _, result = run_adam(bars, weekly, entry_level)
    assert result.degraded and not result.valid and "is not one of" in result.check.problems[0]        # the schema has no level kind for an entry
    said = {**report(weekly), "summary": "Use the entry price at the close of the signal bar."}
    _, result = run_adam(bars, weekly, said)
    assert not result.valid and any("sizing, exits or timing" in p for p in result.check.problems)
    assert set(market.SCHEMA["properties"]) == {"summary", "trend", "levels", "scenarios", "screens", "data_gaps"}


def test_TA_01_in_swo_adam_reads_daily_and_weekly_bars_and_says_so_when_one_is_missing(bars, weekly):
    model = ScriptedChat(answer(report(weekly)))
    result = market.run(model, desk_id=1, symbol="SPY", as_of=AS_OF)           # no bars read, no weekly call, no snapshot
    assert not result.valid
    problems = " ".join(result.check.problems)
    assert WEEKLY in problems and "no daily bar or snapshot" in problems
    _, ok = run_adam(bars, weekly, report(weekly))
    assert ok.valid and "quant_call" in ok.calls and "data_bars" in ok.calls
    with pytest.raises(kit.AnalystKitError):
        market.system_prompt(line="DT", desk_id=1, as_of=AS_OF)


def test_TA_01_a_number_that_no_module_or_snapshot_returned_is_unverified_and_the_answer_does_not_validate(bars, weekly):
    invented = {**report(weekly), "summary": "The stock sits at 12345.67 now."}
    _, result = run_adam(bars, weekly, invented)
    assert not result.valid and result.check.unverified == ("12345.67",) and "12345.67 [unverified]" in result.report.summary
    wrong_level = report(weekly)
    wrong_level["levels"][0]["value"] = weekly["supports"][0] + 1.5
    _, result = run_adam(bars, weekly, wrong_level)
    assert not result.valid and any("not in the evidence" in p for p in result.check.problems)


def test_TA_01_adam_gets_only_the_tools_of_his_role(bars):
    box = market.toolbox(kit.RunContext(1, "SPY", AS_OF, "SWO"))
    assert box.names() == ("data_bars", "data_snapshot", "quant_call")
    for other in ("data_macro", "data_filings", "contract_get", "calendar_events", "exec_propose"):
        with pytest.raises(ToolNotAllowed):
            box.call(other, {})
    blocked = box.call("quant_call", {"name": "fundamentals.profitability", "params": {}})
    assert not blocked.ok and blocked.error_kind == "blocked"


@pytest.mark.parametrize("piece,skill,function", [("CTS-004", "momentum-burst", "indicators.screens.momentum_burst"),
                                                  ("CTS-005", "weekly-structure", WEEKLY),
                                                  ("CTS-020", "vcp", "indicators.screens.vcp"),
                                                  ("CTS-028", "distribution-days", "indicators.screens.distribution_days")])
def test_the_screens_of_adam_are_skills_in_his_prompt_and_calculations_in_the_registry(piece, skill, function):
    assert skill in [s.name for s in kit.load_seat_file("A01").skills]
    assert f"## Skill: {skill}\n" in market.system_prompt(line="SWO", desk_id=1, as_of=AS_OF)
    assert piece in piece_ids(function) and function in [s.name for s in registry.list("indicators")]
    assert cited_calculations(skill) <= {s.name for s in registry.list()}


def test_CTS_004_momentum_burst_is_reachable_by_adam_and_the_skill_refuses_a_fill_at_the_trigger_close(bars):
    box = market.toolbox(kit.RunContext(1, "SPY", AS_OF, "SWO"))
    result = box.call("quant_call", {"name": "indicators.screens.momentum_burst", "params": {"bars": frame_payload(bars), "as_of": "2024-06-14"}})
    assert result.ok and result.data["piece_ids"] == ["CTS-004"]
    assert "close of the trigger bar" in skill_text("momentum-burst") and "still open" in skill_text("momentum-burst")


def test_CTS_005_the_weekly_structure_has_scenarios_with_invalidation_and_no_probability(weekly):
    assert 2 <= len(weekly["scenarios"]) <= 4 and all(s["invalidation"] is not None for s in weekly["scenarios"])
    assert all(s["probability"] is None for s in weekly["scenarios"]) and weekly["probabilities_calibrated"] is False
    assert "probability" in skill_text("weekly-structure").lower()


def test_CTS_020_vcp_is_reachable_by_adam_and_the_skill_says_the_stage_is_judged_by_closes_not_wicks(bars):
    box = market.toolbox(kit.RunContext(1, "SPY", AS_OF, "SWO"))
    result = box.call("quant_call", {"name": "indicators.screens.vcp", "params": {"bars": frame_payload(bars), "as_of": "2024-06-14"}})
    assert result.ok and result.data["piece_ids"] == ["CTS-020"]
    text = skill_text("vcp")
    assert "wick" in text and "next open plus one tick" in text


def test_CTS_028_distribution_days_are_reachable_by_adam_and_a_missing_volume_is_never_a_distribution_day(bars):
    box = market.toolbox(kit.RunContext(1, "SPY", AS_OF, "SWO"))
    result = box.call("quant_call", {"name": "indicators.screens.distribution_days", "params": {"bars": frame_payload(bars), "as_of": "2024-06-14"}})
    assert result.ok and result.data["piece_ids"] == ["CTS-028"]
    assert "without volume" in skill_text("distribution-days")
