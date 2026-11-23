"""Tim, the news and macro analyst (SPEC-019: B7): TA-03, the 8-K table DTS-02 and the sector rotation reading OA-03."""
from datetime import date, datetime, timezone

import pytest
from b7_support import AS_OF, ROOT, ScriptedChat, answer, call, load_registry

from hokma.data import filings as data_filings
from hokma.data import sentinels
from hokma.desks.agents import analyst_kit as kit
from hokma.desks.agents import news
from hokma.desks.core.signal import contains_execution_terms
from hokma.desks.core.tools import ToolNotAllowed
from tests.data.sec.support import DownTransport, make_client

MACRO = {"series_id": "CPIAUCSL", "vintage": "2024-06-12", "value": 313.225, "source": "fred"}
SNAPSHOT = {"symbol": "AAPL", "sections": {"news": {"status": "ok", "source": "yahoo", "value": [{"title": "Apple unveils new software", "published": "2024-06-10"}]},
                                            "prediction_markets": {"status": "no_data", "value": None}}}


@pytest.fixture(autouse=True)
def registry_ready():
    load_registry()


def macro_handler(**args):
    return {"value": [{"date": "2024-05-01", "value": 313.225}], "source": "fred", "as_of": args["as_of"], "series_id": args["series_id"]}


def calendar_handler(**args):
    return {"status": "OK", "events": [{"name": "FOMC rate decision", "when": "2024-06-19T18:00:00Z", "source": "calendar"}]}


def report(**patch):
    base = {
        "summary": "One software event and one macro print before the cut-off.",
        "events": [{"date": "2024-06-10", "source": "yahoo", "category": "news", "text": "Apple unveils new software."}],
        "macro": [MACRO],
        "calendar": [{"event": "FOMC rate decision", "when": "2024-06-19T18:00:00Z", "source": "calendar"}],
        "prediction_markets": {"status": "data_missing", "items": []},
        "sector_rotation": {"regime": "unclear", "note": "The returned figures do not fix a regime.", "ranking": []},
        "data_gaps": [],
    }
    return {**base, **patch}


def run_tim(body, snapshot=SNAPSHOT, **kwargs):
    model = ScriptedChat(call("data_macro", series_id="CPIAUCSL", as_of="2024-06-14"), call("calendar_events", start="2024-06-14", end="2024-06-30"),
                         answer(body))
    handlers = {"data_macro": macro_handler, "calendar_events": calendar_handler}
    return model, news.run(model, desk_id=1, symbol="AAPL", as_of=AS_OF, snapshot=snapshot, handlers=handlers, **kwargs)


def test_TA_03_tim_joins_news_macro_with_its_vintage_and_the_calendar_into_one_dated_report_with_sources():
    model, result = run_tim(report())
    assert result.valid, result.check.problems
    text = result.text
    assert "2024-06-10 (news, source yahoo)" in text and "as published on 2024-06-12" in text and "Calendar FOMC rate decision" in text
    assert result.calls == ("data_macro", "calendar_events")
    assert model.calls[0]["tools"] == ["data_snapshot", "data_macro", "data_filings", "calendar_events"]


def test_TA_03_without_prediction_markets_from_the_data_module_they_are_data_missing_and_carry_no_probability():
    _, result = run_tim(report())
    assert result.valid and "prediction_markets" in result.report.data_gaps and "Prediction markets: missing data." in result.text
    said = report(prediction_markets={"status": "available", "items": [{"question": "Rate cut in June?", "probability": 0.62, "source": "polymarket"}]})
    _, invented = run_tim(said)
    assert not invented.valid and any("data_missing" in p for p in invented.check.problems)
    served = {**SNAPSHOT, "sections": {**SNAPSHOT["sections"], "prediction_markets": {"status": "ok", "value": [{"question": "Rate cut in June?", "probability": 0.62}]}}}
    _, honest = run_tim(said, snapshot=served)
    assert honest.valid and "prediction_markets" not in honest.report.data_gaps and "0.62" in honest.text


def test_TA_03_a_macro_value_after_the_cut_off_or_without_a_date_is_refused():
    _, late = run_tim(report(macro=[{**MACRO, "vintage": "2024-07-01"}]))
    assert not late.valid and any("after the cut-off" in p for p in late.check.problems)
    _, undated = run_tim(report(events=[{"date": "recently", "source": "yahoo", "category": "news", "text": "Apple unveils new software."}]))
    assert not undated.valid and any("not a date" in p for p in undated.check.problems)
    assert set(news.SCHEMA["properties"]["events"]["items"]["required"]) >= {"date", "source"}


def test_TA_03_tim_gives_no_signal_and_no_window_to_trade():
    for field in ("signal", "window", "direction", "trade_window"):
        _, result = run_tim({**report(), field: "up"})
        assert not result.valid and any(field in p for p in result.check.problems), field
    _, said = run_tim(report(summary="Enter now before the decision."))
    assert not said.valid
    assert not {"signal", "window", "direction"} & set(news.SCHEMA["properties"])


def test_TA_03_a_number_that_no_tool_returned_is_unverified_and_does_not_validate():
    _, result = run_tim(report(macro=[{**MACRO, "value": 399.5}]))
    assert not result.valid and any("399.5" in p for p in result.check.problems)


def test_TA_03_tim_gets_only_the_tools_of_his_role():
    box = news.toolbox(kit.RunContext(1, "AAPL", AS_OF, "SWO"))
    assert box.names() == ("data_snapshot", "data_macro", "data_filings", "calendar_events")
    for other in ("quant_call", "data_bars", "data_fundamentals", "contract_get"):
        with pytest.raises(ToolNotAllowed):
            box.call(other, {})
    refused = box.call("data_snapshot", {"symbol": "AAPL", "as_of": "2024-06-14", "categories": ["market"]})
    assert not refused.ok and refused.error_kind == "bad_input"


# ---- DTS-02: the table of items of the 8-K -----------------------------------------------------------------------------

def filings(as_of=datetime(2024, 8, 1, tzinfo=timezone.utc), client=None):
    return data_filings("NWND", form="8-K", as_of=as_of, client=client or make_client())


def test_DTS_02_the_table_classifies_the_8k_filings_read_by_the_data_module():
    table = news.eight_k_items(filings())
    by_accession = {r.accession: r for r in table.rows}
    results = by_accession["0001000001-24-000021"]
    assert results.filing_date == date(2024, 5, 2) and results.kinds == ("results", "exhibits")
    officer = by_accession["0001000001-24-000030"]
    assert officer.kinds == ("officer_or_director_change",) and officer.items[0].number == "5.02"
    assert [r.accession for r in table.rows] == ["0001000001-24-000030", "0001000001-24-000021"] and table.data_gaps == ()
    assert table.to_dict()["filings"][1]["items"][0] == {"number": "2.02", "kind": "results", "meaning": "Results of operations."}


def test_DTS_02_when_headline_and_filing_disagree_the_filing_rules_and_the_headline_is_a_hint():
    table = news.eight_k_items(filings(), [
        {"text": "Northwind CEO steps down after a decade", "date": "2024-05-02"},        # the filing of that day carries results, not an officer change
        {"text": "Northwind CEO steps down after a decade", "date": "2024-07-15"},        # the filing of that day carries 5.02
        {"text": "Northwind announces acquisition of a rival", "date": "2024-07-20"},     # no filing carries an acquisition
        {"text": "Northwind shares drift on a quiet day", "date": "2024-07-20"},          # no kind of event named
    ])
    conflict, confirmed, missing, vague = table.headlines
    assert conflict.status == "conflict" and conflict.accession == "0001000001-24-000021" and "The filing rules" in conflict.note
    assert conflict.headline_kind == "officer_or_director_change" and "results" in conflict.note
    assert confirmed.status == "confirmed" and confirmed.accession == "0001000001-24-000030"
    assert missing.status == "not_filed" and missing.accession is None
    assert vague.status == "unclassified" and vague.headline_kind is None


def test_DTS_02_a_filing_after_the_cut_off_is_hidden_and_a_failed_read_is_a_gap_not_an_empty_table():
    early = news.eight_k_items(filings(as_of=datetime(2024, 5, 10, tzinfo=timezone.utc)))
    assert [r.accession for r in early.rows] == ["0001000001-24-000021"]
    down = news.eight_k_items(filings(client=make_client(DownTransport())))
    assert down.rows == () and down.data_gaps and down.data_gaps[0].startswith("filings_")
    assert news.eight_k_items([]).data_gaps == ("no_8k_visible_at_cut_off",)


def test_DTS_02_items_outside_the_table_are_unknown_and_exhibits_never_confirm_a_headline_by_themselves():
    row = news.eight_k_items([{"accession": "a-1", "form": "8-K/A", "filing_date": "2024-06-01", "items": ["9.99", "9.01", "7.01"]}])
    kinds = row.rows[0].kinds
    assert kinds == ("unknown", "exhibits", "disclosure") and row.rows[0].amends is True
    assert news.classify_item("2.02").kind == "results" and news.classify_item("12.3").kind == "unknown"
    read = news.eight_k_items([{"accession": "a-1", "form": "8-K", "filing_date": "2024-06-01", "items": ["9.01"]}],
                              [{"text": "Company reports quarterly results", "accession": "a-1"}])
    assert read.headlines[0].status == "conflict"                     # the exhibits item carries material of another item, it is not the results item
    assert news.eight_k_items([{"accession": "x", "form": "10-K", "filing_date": "2024-06-01", "items": []}]).rows == ()


def test_DTS_02_the_skill_is_in_the_prompt_of_tim_and_the_filing_tool_is_his():
    assert "filing-events" in [s.name for s in kit.load_seat_file("A04").skills]
    assert "## Skill: filing-events\n" in news.system_prompt(line="SWO", desk_id=1, as_of=AS_OF)
    box = news.toolbox(kit.RunContext(1, "NWND", AS_OF, "SWO"), handlers={"data_filings": lambda **a: {"value": [], "args": a}})
    assert box.call("data_filings", {"symbol": "NWND", "form": "8-K", "as_of": "2024-06-14"}).ok
    assert "## 1. Item table" in kit.load_seat_file("A04").skills[0].body


def test_DTS_02_tim_gets_only_the_tools_of_his_role_when_reading_filings():
    box = news.toolbox(kit.RunContext(1, "NWND", AS_OF, "SWO"))
    with pytest.raises(ToolNotAllowed):
        box.call("data_fundamentals", {"symbol": "NWND", "as_of": "2024-06-14", "statement": "income", "period": "annual"})
    assert not box.call("data_filings", {"symbol": "NWND", "form": "not a form!", "as_of": "2024-06-14"}).ok


# ---- OA-03: sector rotation as a reading instruction -------------------------------------------------------------------

def test_OA_03_the_sector_rotation_section_is_in_the_prompt_of_tim_and_only_in_his_prompt():
    prompt = news.system_prompt(line="SWO", desk_id=1, as_of=AS_OF)
    assert "## Sector rotation" in prompt and "Sector rotation is a way of reading, not a signal and not a calculation." in prompt
    from hokma.desks.agents import fundamentals, market, options, sentiment

    for other in (market, sentiment, fundamentals, options):
        assert "## Sector rotation" not in other.system_prompt(line="SWO", desk_id=1, as_of=AS_OF)
    with pytest.raises(kit.AnalystKitError):
        news.system_prompt(line="DT", desk_id=1, as_of=AS_OF)                       # a line the file does not carry never gets the piece


def test_OA_03_the_rotation_is_an_instruction_with_the_macro_regime_and_has_no_code_and_no_signal():
    body = kit.load_seat_file("A04").body
    section = body.split("## Sector rotation")[1].split("\n## ")[0]
    for needle in ("short window", "macro backdrop", "regime", "carries no direction"):
        assert needle in section
    assert contains_execution_terms(section) is None and section.isascii()
    assert "sector_rotation" in news.SCHEMA["properties"] and set(news.SCHEMA["properties"]["sector_rotation"]["properties"]) == {"regime", "note", "ranking"}
    assert not (ROOT / "backend" / "hokma" / "desks" / "agents" / "sector_rotation.py").exists()
