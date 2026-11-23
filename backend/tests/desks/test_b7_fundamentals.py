"""Ezra, the fundamentals analyst (SPEC-019: B7): TA-04, the thesis log AFS-004, DTS-10, congress context DTS-11, value chain OA-04
and ratios with a discounted cash flow SKL-013."""
import json
from datetime import datetime, timezone

import pandas as pd
import pytest
from b7_support import AS_OF, FIXTURES, KIT, ScriptedChat, answer, call, cited_calculations, load_registry, piece_ids, skill_text
from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db, make_desk  # noqa: F401  (fixtures)

from hokma.data import sentinels
from hokma.desks.agents import analyst_kit as kit
from hokma.desks.agents import fundamentals as fund
from hokma.desks.core.signal import contains_execution_terms
from hokma.desks.core.tools import ToolNotAllowed
from hokma.lessons import thesis
from hokma.quant import registry

GORDON = {"dividends_per_share": 2, "rate_of_return": 0.08, "growth_rate": 0.03}
STATEMENTS = {"value": [{"period_end": "2024-03-31", "filed": "2024-05-02", "revenue": 1000, "net_income": 152}], "source": "recorded", "as_of": "2024-06-14"}


@pytest.fixture(autouse=True)
def registry_ready():
    load_registry()


def report(value=41.199999999999996, **patch):
    base = {
        "summary": f"Gordon value {value} from the quarter read around the event; one pillar is at risk.",
        "thesis": {
            "statement": "Recurring revenue keeps the margin stable through the next release.",
            "pillars": [{"name": "recurring revenue", "expected": "revenue keeps growing", "status": "on_track", "trend": "flat"},
                        {"name": "margin", "expected": "margin holds", "status": "at_risk", "trend": "deteriorating"}],
            "risks": ["A late filing would delay the read."],
            "catalysts": [{"name": "quarterly release", "date": "2024-06-20", "source": "calendar"}],
            "counter_evidence": ["Cost of revenue grew faster than revenue in the last quarter filed."]},
        "quarter": {"fiscal_quarter": None, "notes": ["Statements filed on 2024-05-02 were read."]},
        "congress": {"criterion": "NO_DATA", "note": "No provider is connected; trades arrive up to 45 days after the trade."},
        "data_gaps": [],
    }
    return {**base, **patch}


def run_ezra(body, **kwargs):
    model = ScriptedChat(call("data_fundamentals", symbol="AAPL", as_of="2024-06-14", statement="income", period="quarterly"),
                         call("quant_call", name="fundamentals.intrinsic", params={"method": "gordon", "params": GORDON}), answer(body))
    handlers = {"data_fundamentals": lambda **a: {**STATEMENTS, "args": a}}
    return model, fund.run(model, desk_id=1, symbol="AAPL", as_of=AS_OF, handlers=handlers, **kwargs)


# ---- TA-04 ---------------------------------------------------------------------------------------------------------------

def test_TA_04_in_swo_ezra_reads_point_in_time_statements_of_one_operation_and_asks_the_module_for_the_value():
    model, result = run_ezra(report())
    assert result.valid, result.check.problems
    assert result.calls == ("data_fundamentals", "quant_call")
    sent = model.calls[0]
    assert "Line SWO" in sent["system"] and sent["tools"] == ["data_fundamentals", "data_filings", "quant_call"]
    # the statements were requested at the cut-off of the run: the tool call is the one of the script, read from the evidence
    evidence_calls = [m for m in model.calls[1]["messages"] if m.role == "tool"]
    assert json.loads(evidence_calls[0].content)["data"]["args"]["as_of"] == "2024-06-14"
    assert "Pillar margin" in result.text and "Against:" in result.text


def test_TA_04_ezra_does_not_calculate_ratios_a_figure_the_module_did_not_return_is_unverified():
    _, result = run_ezra(report(summary="Return on equity is 18.5 percent and the value is 41.199999999999996."))
    assert not result.valid and result.check.unverified == ("18.5",)
    _, own_value = run_ezra(report(value=47.5))
    assert not own_value.valid and any("47.5" in p for p in own_value.check.problems)


def test_TA_04_a_thesis_without_the_evidence_against_it_does_not_validate():
    body = report()
    body["thesis"] = {**body["thesis"], "counter_evidence": []}
    _, result = run_ezra(body)
    assert not result.valid and any("evidence against" in p for p in result.check.problems)


def test_TA_04_ezra_gets_only_the_tools_of_his_role():
    box = fund.toolbox(kit.RunContext(1, "AAPL", AS_OF, "SWO"))
    assert box.names() == ("data_fundamentals", "data_filings", "quant_call")
    for other in ("data_bars", "data_macro", "calendar_events", "contract_get"):
        with pytest.raises(ToolNotAllowed):
            box.call(other, {})
    blocked = box.call("quant_call", {"name": "options.vol_richness", "params": {}})
    assert not blocked.ok and blocked.error_kind == "blocked"
    with pytest.raises(kit.AnalystKitError):
        fund.system_prompt(line="SWC", desk_id=1, as_of=AS_OF)


# ---- AFS-004 -------------------------------------------------------------------------------------------------------------

PILLARS = [fund.Pillar("recurring revenue", "revenue keeps growing"), fund.Pillar("margin", "margin holds")]


@pytest.fixture
def board(desks_db):
    desk_id = make_desk(desks_db)["id"]
    return fund.ThesisBoard(desk_id, "aapl", db=desks_db), desks_db


def test_AFS_004_the_thesis_is_opened_once_and_every_new_fact_is_a_dated_row_with_its_effect_on_a_pillar(board):
    b, db = board
    opened = b.open("Recurring revenue keeps the margin stable.", PILLARS, risks=["late filing"], catalysts=[{"name": "release", "date": "2024-06-20", "source": "calendar"}],
                    now="2024-06-01 12:00:00")
    assert opened.state == "open" and json.loads(opened.note)["pillars"][1]["name"] == "margin"
    one = b.log_datum(pillar="margin", effect="weakens", source="10-Q 2024-05-02", reading="Cost of revenue grew faster than revenue.",
                      action="read_again", fact_date="2024-05-02", as_of=AS_OF, now="2024-06-02 09:00:00")
    two = b.log_datum(pillar="recurring revenue", effect="strengthens", source="10-Q 2024-05-02", reading="Subscription revenue grew.",
                      action="keep_reading", fact_date="2024-05-02", now="2024-06-03 09:00:00")
    assert two > one
    rows = thesis.history(b.desk_id, "AAPL", db=db)
    assert [r.state for r in rows] == ["open", "open", "open"] and [r.created_at for r in rows][1:] == ["2024-06-02 09:00:00", "2024-06-03 09:00:00"]
    datum = json.loads(rows[1].note)
    assert datum["kind"] == "datum" and datum["pillar"] == "margin" and datum["effect"] == "weakens" and datum["fact_date"] == "2024-05-02"
    assert thesis.current_state(b.desk_id, "AAPL", db=db) == "open"
    entries = b.entries()
    assert [e["kind"] for e in entries] == ["thesis", "datum", "datum"]


def test_AFS_004_the_board_has_one_row_per_pillar_with_what_was_expected_its_status_and_its_trend(board):
    b, _ = board
    b.open("Recurring revenue keeps the margin stable.", PILLARS)
    assert [(r.name, r.status, r.trend) for r in b.board()] == [("recurring revenue", "unknown", "unknown"), ("margin", "unknown", "unknown")]
    b.log_datum(pillar="margin", effect="weakens", source="10-Q", reading="Margin fell.", action="read_again", fact_date="2024-05-02")
    b.log_datum(pillar="margin", effect="neutral", source="call", reading="Management repeated the plan.", action="keep_reading", fact_date="2024-05-03")
    b.log_datum(pillar="recurring revenue", effect="strengthens", source="10-Q", reading="Revenue grew.", action="keep_reading", fact_date="2024-05-02")
    rows = {r.name: r for r in b.board()}
    assert rows["margin"].expected == "margin holds" and (rows["margin"].status, rows["margin"].trend) == ("at_risk", "deteriorating")
    assert rows["margin"].last_effect == "neutral" and rows["margin"].last_fact_date == "2024-05-03"
    assert (rows["recurring revenue"].status, rows["recurring revenue"].trend) == ("on_track", "improving")
    b.log_datum(pillar="margin", effect="weakens", breaks=True, source="8-K item 4.02", reading="The statements were withdrawn.", action="hand_to_judge",
                fact_date="2024-05-10")
    assert {r.name: r.status for r in b.board()}["margin"] == "broken"


def test_AFS_004_a_bad_datum_writes_nothing(board):
    b, db = board
    with pytest.raises(thesis.InvalidTransition):
        b.log_datum(pillar="margin", effect="weakens", source="10-Q", reading="x", action="read_again", fact_date="2024-05-02")      # no thesis yet
    b.open("Recurring revenue keeps the margin stable.", PILLARS)
    good = dict(pillar="margin", effect="weakens", source="10-Q", reading="Margin fell.", action="read_again", fact_date="2024-05-02")
    for patch in ({"pillar": "growth"}, {"effect": "great"}, {"action": "sell"}, {"fact_date": "yesterday"}, {"source": " "}, {"breaks": True, "effect": "neutral"},
                  {"fact_date": "2024-06-20", "as_of": AS_OF}):
        with pytest.raises(thesis.ThesisError):
            b.log_datum(**{**good, **patch})
    assert len(thesis.history(b.desk_id, "AAPL", db=db)) == 1
    thesis.record(b.desk_id, "AAPL", "closed", db=db)
    with pytest.raises(thesis.InvalidTransition):
        b.log_datum(**good)                                                     # a closed thesis takes no more data
    with pytest.raises(thesis.ThesisError):
        b.open("", PILLARS)
    with pytest.raises(thesis.ThesisError):
        b.open("Same names", [fund.Pillar("a", "x"), fund.Pillar("a", "y")])


def test_AFS_004_the_answer_has_no_exit_condition_with_a_number_because_the_committee_fixes_it():
    for extra in ({"exit_trigger": 120.5}, {"stop": 100.0}, {"price_target": 150.0}):
        body = report()
        body["thesis"] = {**body["thesis"], **extra}
        _, result = run_ezra(body)
        assert not result.valid and any(next(iter(extra)) in p for p in result.check.problems), extra
    _, said = run_ezra(report(summary="Leave if the price target of 150 is reached; the gordon value is 41.199999999999996."))
    assert not said.valid
    assert "exit_trigger" not in json.dumps(fund.SCHEMA)


def test_AFS_004_the_skill_is_in_the_prompt_of_ezra_and_the_log_rules_are_in_it():
    assert "thesis-tracker" in [s.name for s in kit.load_seat_file("A06").skills]
    prompt = fund.system_prompt(line="SWO", desk_id=1, as_of=AS_OF)
    assert "## Skill: thesis-tracker\n" in prompt and "strengthens, weakens or neutral" in prompt
    text = skill_text("thesis-tracker")
    assert "risk committee" in text and "evidence against" in text


# ---- DTS-10 ---------------------------------------------------------------------------------------------------------------

def test_DTS_10_ezra_reaches_the_quarter_facts_of_the_module_with_the_fourth_quarter_derived():
    assert "DTS-10" in piece_ids("fundamentals.quarter_facts")
    assert registry.describe("fundamentals.quarter_facts").module in kit.SEATS["A06"].quant_modules
    facts = pd.DataFrame(json.loads((FIXTURES / "statements_pit.json").read_text(encoding="utf-8"))["quarterly_revenue_ALFA"]["facts"])
    value = registry.call("fundamentals.quarter_facts", facts=facts, as_of="2024-06-30").value
    q4 = next(q for q in value["quarters"] if q["period_end"] == "2023-12-31")
    assert q4["derived"] is True and q4["value"] == pytest.approx(1300 - (310 + 320 + 335), abs=1e-9)         # the year minus three quarters
    assert "quarter of the event" in fund.system_prompt(line="SWO", desk_id=1, as_of=AS_OF)


def test_DTS_10_an_empty_or_nan_input_is_no_value_and_ezra_is_told_to_read_the_quarter_of_the_event():
    columns = ["period_start", "period_end", "value", "filed", "fiscal_year", "fiscal_period"]
    empty = registry.call("fundamentals.quarter_facts", facts=pd.DataFrame(columns=columns), as_of="2024-06-30")
    assert empty.value is None and empty.warnings
    assert "the fourth quarter derived as the year minus three quarters" in kit.load_seat_file("A06").body


# ---- DTS-11 ---------------------------------------------------------------------------------------------------------------

def congress_result(rows, ok=True):
    cut = sentinels.parse_time("2024-06-14")
    if ok:
        return sentinels.ok(rows, source="provider", as_of=cut, received_at=cut)
    return sentinels.unavailable("provider", "key_missing", as_of=cut, received_at=cut)


def test_DTS_11_without_a_provider_the_criterion_is_no_data_and_no_edge_is_promised():
    for given in (None, congress_result([], ok=False), congress_result([])):
        context = fund.congress_context(given, AS_OF)
        assert context.criterion == "NO_DATA" and context.trades == ()
        assert "up to 45 days" in context.note and "not an edge" in context.note
    model = ScriptedChat(answer(report()))
    result = fund.run(model, desk_id=1, symbol="AAPL", as_of=AS_OF, snapshot=STATEMENTS)
    assert "\"criterion\": \"NO_DATA\"" in model.calls[0]["messages"][0].content


def test_DTS_11_the_trades_come_with_their_delay_and_a_disclosure_after_the_cut_off_is_not_visible():
    rows = [{"member": "A. Member", "transaction_date": "2024-05-01", "disclosure_date": "2024-05-20", "type": "purchase"},
            {"member": "B. Member", "transaction_date": "2024-03-01", "disclosure_date": "2024-05-10", "type": "sale"},
            {"member": "C. Member", "transaction_date": "2024-06-10", "disclosure_date": "2024-06-20", "type": "purchase"}]
    context = fund.congress_context(congress_result(rows), AS_OF)
    assert context.criterion == "CONTEXT" and [t.member for t in context.trades] == ["A. Member", "B. Member"]
    assert [t.lag_days for t in context.trades] == [19, 70] and [t.beyond_declared_lag for t in context.trades] == [False, True]
    assert context.to_dict()["max_lag_days"] == 45


def test_DTS_11_the_report_has_to_match_the_data_module_and_declare_the_delay():
    _, lying = run_ezra(report(congress={"criterion": "CONTEXT", "note": "Trades arrive up to 45 days late."}))
    assert not lying.valid and any("data module gives NO_DATA" in p for p in lying.check.problems)
    _, silent = run_ezra(report(congress={"criterion": "NO_DATA", "note": "Nothing connected."}))
    assert not silent.valid and any("delay" in p for p in silent.check.problems)
    _, honest = run_ezra(report())
    assert honest.valid
    assert "Congress context NO_DATA" in honest.text


def test_DTS_11_ezra_gets_only_the_tools_of_his_role_when_reading_context_data():
    box = fund.toolbox(kit.RunContext(1, "AAPL", AS_OF, "SWO"))
    assert not {"data_snapshot", "data_universe"} & set(box.names())
    with pytest.raises(ToolNotAllowed):
        box.call("data_snapshot", {"symbol": "AAPL", "as_of": "2024-06-14", "categories": ["fundamentals"]})


# ---- OA-04 ----------------------------------------------------------------------------------------------------------------

def test_OA_04_the_value_chain_section_is_in_the_prompt_of_ezra_and_only_in_mesas_of_his_lines():
    prompt = fund.system_prompt(line="SWO", desk_id=1, as_of=AS_OF)
    assert "## Value chain" in prompt
    from hokma.desks.agents import market, news, options, sentiment

    for other in (market, sentiment, news, options):
        assert "## Value chain" not in other.system_prompt(line="SWO", desk_id=1, as_of=AS_OF)
    assert kit.load_seat_file("A06").lines == ("SWO", "LP")
    assert "## Value chain" in fund.system_prompt(line="LP", desk_id=1, as_of=AS_OF)
    with pytest.raises(kit.AnalystKitError):
        fund.system_prompt(line="DT", desk_id=1, as_of=AS_OF)


def test_OA_04_the_value_chain_is_an_instruction_of_the_analyst_without_code_or_execution_words():
    body = kit.load_seat_file("A06").body
    section = body.split("## Value chain")[1].split("\n## ")[0]
    for needle in ("suppliers", "customers", "competitors", "margin", "gives no signal by itself"):
        assert needle in section
    assert contains_execution_terms(section) is None and section.isascii()


# ---- SKL-013 --------------------------------------------------------------------------------------------------------------

def test_SKL_013_the_ratios_and_dcf_skill_is_in_the_prompt_of_ezra_and_every_call_it_names_exists():
    assert "financial-ratios-dcf" in [s.name for s in kit.load_seat_file("A06").skills]
    assert "## Skill: financial-ratios-dcf\n" in fund.system_prompt(line="SWO", desk_id=1, as_of=AS_OF)
    names = cited_calculations("financial-ratios-dcf")
    assert {"fundamentals.profitability", "fundamentals.intrinsic", "fundamentals.wacc"} <= names
    assert names <= {s.name for s in registry.list()}
    assert "FT-016" in piece_ids("fundamentals.intrinsic")
    text = skill_text("financial-ratios-dcf")
    assert "budget variance" in text and "forecast of the company" in text           # named as left out, not as part of the skill


def test_SKL_013_a_dcf_with_terminal_growth_at_or_above_the_discount_rate_is_rejected():
    box = fund.toolbox(kit.RunContext(1, "AAPL", AS_OF, "SWO"))
    dcf = {"cash_flow": 100.0, "growth_rate": 0.05, "perpetual_growth_rate": 0.08, "wacc": 0.08, "cash": 10.0, "total_debt": 20.0, "shares_outstanding": 10.0}
    for method, params in (("dcf", dcf), ("dcf", {**dcf, "perpetual_growth_rate": 0.09}), ("gordon", {**GORDON, "growth_rate": 0.08}),
                           ("ddm_two_stage", {"dividends_per_share": 2, "rate_of_return": 0.06, "high_growth_rate": 0.1, "stable_growth_rate": 0.07})):
        rejected = box.call("quant_call", {"name": "fundamentals.intrinsic", "params": {"method": method, "params": params}})
        assert not rejected.ok and rejected.error_kind == "bad_input" and "rejected" in rejected.message, (method, params)
    sound = box.call("quant_call", {"name": "fundamentals.intrinsic", "params": {"method": "dcf", "params": {**dcf, "perpetual_growth_rate": 0.02}}})
    assert sound.ok and sound.data["value"]["value"] is not None
    assert fund.dcf_problems("dcf", {"perpetual_growth_rate": 0.02, "wacc": 0.08}) == [] and fund.dcf_problems("other", {}) == []
    assert registry.call("fundamentals.intrinsic", method="gordon", params={**GORDON, "growth_rate": 0.08}).value is None      # the module agrees


def test_SKL_013_the_skill_asks_for_the_reasonableness_check_before_the_value():
    text = skill_text("financial-ratios-dcf")
    assert "terminal growth is at or above the discount rate" in text and "## 3. Check before you value" in text
    assert text.index("## 3. Check before you value") < text.index("## 4. Value")
