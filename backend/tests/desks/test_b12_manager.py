"""Grant, the sub-manager of the desk (SPEC-019: B12): AFS-034, CTS-024, TA-13, TA-43, SKL-009 and the mandate of AH-25. Fake engine, recorded
fixtures, no network."""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from b7_support import ScriptedChat, answer, call
from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db, make_desk  # noqa: F401  (fixtures)
from exec_support import AGENT, proposal, ready  # noqa: F401  (fixtures)
from lessons.support import fill, make_decision, make_order
from swo_kit_support import build_kit

from hokma import exec as hexec
from hokma.backoffice import runs
from hokma.desks.agents import analyst_kit as kit
from hokma.desks.agents import manager as mg
from hokma.desks.core.graph import ModelReply
from hokma.desks.core.skills import parse_skill, verify_manifest, read_manifest
from hokma.desks.core.tools import ToolNotAllowed
from hokma.desks.plantel import SEATS
from hokma.exec import levels
from hokma.lessons.store import NotExecuted
import hokma.lessons as lessons

ROOT = Path(__file__).resolve().parents[3]
KIT = ROOT / "kit" / "template" / "desk"
AS_OF = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
CUT = AS_OF.isoformat()
EVENT_AT = "2026-11-20T13:30:00+00:00"


# ---- the morning note, AFS-034 ----------------------------------------------------------------------------------------------------

def facts(**kw):
    base = dict(positions=[{"symbol": "AAPL", "overnight_move_pct": 1.2}], events=[{"event": "CPI release", "at": EVENT_AT, "touches": "AAPL"}],
                reports={"market": "The trend is up and the weekly level holds."})
    base.update(kw)
    return mg.note_facts(AS_OF, **base)


def note(**patch):
    body = {"headline": "AAPL is the position to watch before the open.", "material": True,
            "opinions": [{"text": "AAPL moved 1.2 percent overnight and the trend report stays positive.", "at": CUT}],
            "overnight": [{"position": "AAPL", "text": "AAPL rose 1.2 percent overnight.", "at": CUT}],
            "events_today": [{"event": "CPI release", "at": EVENT_AT, "text": "It can move AAPL."}],
            "ideas": [{"idea": "Keep reading the trend.", "risk": "A gap through the weekly level would prove it wrong.", "report": "market"}],
            "previous_error": None, "data_gaps": []}
    body.update(patch)
    return body


def grant_note(body, *, desk_id=1, f=None, **kw):
    model = ScriptedChat(ModelReply(json.dumps(body)))
    return model, mg.morning_note(model, desk_id=desk_id, as_of=AS_OF, facts=f or facts(), **kw)


def test_AFS_034_the_note_starts_with_the_main_call_and_every_opinion_carries_its_time():
    model, result = grant_note(note())
    assert result.valid, result.check.problems
    assert result.text.splitlines()[0] == "AAPL is the position to watch before the open."
    assert all(line.startswith("- [") and CUT in line for line in result.text.splitlines()[1:2])
    assert f"[{CUT}] AAPL moved" in result.text and "Idea: Keep reading the trend." in result.text
    first = model.calls[0]
    assert first["tools"] == ["book_pnl", "lessons_propose", "schedule_run"] and "Line SWO" in first["system"]
    assert "## Skill: morning-note" in first["system"] and "signal-postmortem" not in first["system"] and "## Role" in first["system"]


def test_AFS_034_nothing_material_is_a_valid_note():
    quiet = note(headline="Nothing material before the open: no position moved and no event is due.", material=False, overnight=[], events_today=[], ideas=[],
                 opinions=[{"text": "The desk watches the trend report only.", "at": CUT}])
    _, result = grant_note(quiet, f=facts(positions=[], events=[]))
    assert result.valid, result.check.problems and result.text.startswith("Nothing material")
    for bad in ({"headline": "A quiet day."}, {"ideas": [note()["ideas"][0]]}, {"overnight": note()["overnight"]}):
        _, rejected = grant_note({**quiet, **bad})
        assert not rejected.valid and any("quiet" in p or "nothing is material" in p for p in rejected.check.problems), bad


def test_AFS_034_an_opinion_without_a_time_or_with_a_time_after_the_cutoff_or_made_up_does_not_validate():
    _, missing = grant_note(note(opinions=[{"text": "The trend is up."}]))
    assert not missing.valid and missing.degraded
    later = (AS_OF + timedelta(hours=2)).isoformat()
    _, after = grant_note(note(opinions=[{"text": "The trend is up.", "at": later}]), f=facts(extra={"later": later}))
    assert not after.valid and any("later than the cut-off" in p for p in after.check.problems)
    _, invented = grant_note(note(opinions=[{"text": "The trend is up.", "at": "2026-11-20T14:45:00+00:00"}]))
    assert not invented.valid and any("not an identifier that a tool returned" in p for p in invented.check.problems)
    _, none = grant_note(note(opinions=[]))
    assert not none.valid and any("at least one opinion" in p for p in none.check.problems)


def test_AFS_034_the_error_of_the_last_note_is_named_in_the_next_one():
    owed = facts(last_note_error="The last note gave the wrong time for the CPI release.")
    _, silent = grant_note(note(), f=owed)
    assert not silent.valid and any("previous_error" in p for p in silent.check.problems)
    _, named = grant_note(note(previous_error="The last note gave the wrong time for the CPI release."), f=owed)
    assert named.valid and "Correction of the last note: The last note gave the wrong time" in named.text
    _, invented = grant_note(note(previous_error="Something went wrong."))
    assert not invented.valid


def test_AFS_034_the_note_is_one_page_and_a_number_outside_the_evidence_is_unverified():
    _, long = grant_note(note(overnight=[{"position": "AAPL", "text": "x" * 590, "at": CUT}] * 6))
    assert not long.valid and any("one page" in p for p in long.check.problems)
    _, made_up = grant_note(note(opinions=[{"text": "AAPL rose 7.3 percent overnight.", "at": CUT}]))
    assert not made_up.valid and "7.3" in made_up.check.unverified and "7.3 [unverified]" in made_up.text
    _, extra = grant_note({**note(), "size": 3})
    assert not extra.valid


def test_AFS_034_grant_gets_only_the_tools_of_his_seat_and_asking_for_another_fails():
    box = mg.grant_toolbox(7)
    assert box.names() == ("book_pnl", "lessons_propose", "schedule_run") == SEATS["A15"].tools
    for other in ("order_place", "exec_propose", "quant_call", "desk_level_set", "data_bars", "desk_configure"):
        with pytest.raises(ToolNotAllowed):
            box.call(other, {})
    with pytest.raises(Exception, match="no tool"):
        mg.grant_toolbox(7, handlers={"order_place": lambda **kw: None})
    asked = ScriptedChat(call("order_place", symbol="AAPL"), ModelReply(json.dumps(note())))
    result = mg.morning_note(asked, desk_id=7, as_of=AS_OF, facts=facts())
    assert not result.valid and any("order_place" in p and "not a tool of this seat" in p for p in result.check.problems)
    assert box.call("book_pnl", {"desk_id": 8, "start": "2026-11-01", "end": "2026-11-20"}).error_kind == "blocked"        # another desk is never his


def test_AFS_034_a_figure_from_his_own_tool_is_evidence_and_book_pnl_reads_only_his_desk():
    seen = []

    def pnl(desk_id, start, end):
        seen.append((desk_id, start, end))
        return {"value": {"realized": 321.5}}

    model = ScriptedChat(call("book_pnl", start="2026-11-01", end="2026-11-20"),
                         ModelReply(json.dumps(note(opinions=[{"text": "The desk realized 321.5 this month.", "at": CUT}]))))
    result = mg.morning_note(model, desk_id=7, as_of=AS_OF, facts=facts(), handlers={"book_pnl": pnl})
    assert result.valid, result.check.problems and seen == [(7, "2026-11-01", "2026-11-20")] and result.calls == ("book_pnl",)


def test_AFS_034_the_skill_is_english_matches_its_folder_and_is_in_the_manifest():
    for name in ("morning-note", "signal-postmortem"):
        skill = parse_skill((KIT / "skills" / name / "SKILL.md").read_text(encoding="utf-8"), name)
        assert skill.name == name and read_manifest(KIT / "skills")["skills"][name]["digest"] == skill.digest()
    problems = [p for p in verify_manifest(KIT / "skills") if p.startswith(("morning-note", "signal-postmortem"))]
    assert problems == []


def test_AFS_034_a_seat_file_that_does_not_carry_the_line_is_refused_so_no_skill_of_another_line_reaches_the_prompt(tmp_path):
    root = build_kit(tmp_path / "kit", skills={"A15": ["morning-note"]}, lines={"A15": "SWC"})
    with pytest.raises(kit.AnalystKitError):
        mg.system_prompt("note", line="SWO", desk_id=1, as_of=AS_OF, kit_root=root)
    ok = mg.system_prompt("note", line="SWC", desk_id=1, as_of=AS_OF, kit_root=root)
    assert "## Skill: morning-note" in ok and "Line SWC" in ok


# ---- the final rating, TA-13 ------------------------------------------------------------------------------------------------------

def final_facts(**kw):
    base = dict(desk_level=2, judge_rating="APPROVE", symbol="AAPL", filter_summary="The company passes the filter.", judge_rationale="The catalyst is dated.",
                committee={"risk_level": "moderate", "summary": "The risk is acceptable."}, proposal={"structure": "long_call", "underlying": "AAPL"})
    base.update(kw)
    return mg.final_facts(**base)


def final(**patch):
    body = {"rating": "APPROVE", "grade": "B", "thesis": "A dated catalyst and a liquid contract carry a purchase of calls.",
            "would_change": ["The catalyst date moves out of the life of the option."], "horizon": "weeks", "desk_level": 2,
            "summary": "The tribunal approved and the committee accepted the risk.", "data_gaps": []}
    body.update(patch)
    return body


def grant_final(body, f=None, **kw):
    model = ScriptedChat(ModelReply(json.dumps(body)))
    return model, mg.final_rating(model, desk_id=1, as_of=AS_OF, symbol="aapl", facts=f or final_facts(), **kw)


def test_TA_13_grant_gives_the_final_rating_with_thesis_what_would_change_it_and_the_horizon():
    model, result = grant_final(final())
    assert result.valid, result.check.problems
    assert (result.final.rating, result.final.grade) == ("APPROVE", "B") and result.horizon == "weeks" and result.level == 2
    assert result.thesis.startswith("A dated catalyst") and result.would_change and "Would change it:" in result.text and "Horizon: weeks." in result.text
    assert "## Task\nGive the final rating" in model.calls[0]["system"] and "morning-note" not in model.calls[0]["system"]


def test_TA_13_the_level_of_the_desk_is_reported_as_it_is_and_never_changed():
    for patch in ({"desk_level": 3}, {"desk_level": 1}):
        _, result = grant_final(final(**patch))
        assert not result.valid and result.final is None and any("level of the desk" in p for p in result.check.problems), patch
    for key in ("level_change", "set_level", "new_level"):
        _, result = grant_final({**final(), key: 3})
        assert not result.valid and any(key in p for p in result.check.problems), key
    _, ok = grant_final(final(desk_level=3), f=final_facts(desk_level=3))
    assert ok.valid and ok.level == 3


def test_TA_13_grant_confirms_or_lowers_the_rating_of_the_judge_and_never_raises_it():
    _, up = grant_final(final(rating="APPROVE"), f=final_facts(judge_rating="APPROVE_WITH_CONDITIONS"))
    assert not up.valid and any("above the rating of the judge" in p for p in up.check.problems)
    _, down = grant_final(final(rating="REJECT"), f=final_facts(judge_rating="APPROVE"))
    assert down.valid and down.final.rating == "REJECT"
    _, none = grant_final(final(), f=final_facts(judge_rating="REVIEW"))
    assert not none.valid
    _, empty = grant_final(final(would_change=[]))
    assert not empty.valid and any("would_change" in p for p in empty.check.problems)


def test_TA_13_grant_has_only_his_tools_and_every_number_is_in_the_evidence():
    asked = ScriptedChat(call("exec_propose", desk_id=1, proposal={}), ModelReply(json.dumps(final())))
    refused = mg.final_rating(asked, desk_id=1, as_of=AS_OF, symbol="AAPL", facts=final_facts())
    assert not refused.valid and any("exec_propose" in p for p in refused.check.problems)
    _, made_up = grant_final(final(summary="The case rests on a 63 percent chance."))
    assert not made_up.valid and "63" in made_up.check.unverified and "[unverified]" in made_up.text
    _, bad = grant_final(final(rating="BUY"))
    assert not bad.valid and bad.degraded


# ---- lessons only from executed orders, TA-43 --------------------------------------------------------------------------------------

@pytest.fixture
def world(desks_db):
    desk = make_desk(desks_db)["id"]
    decision = make_decision(desks_db)
    return desks_db, desk, decision


def executed_lesson(world, order_ref="ORD-1"):
    db, desk, decision = world
    order = make_order(db, desk, decision_id=decision, broker_order_id=order_ref, price=100.0)
    seed = lessons.propose(desk, decision, order, "Executed order.", "desk-manager", db=db)
    fills = [fill(order_ref, "BUY", 2, 101.0, "2026-11-02 15:00:00", 1.0, symbol="AAPL")]
    settled = lessons.settle(seed.id, fills=fills, mark_price=110.0, mark_at="2026-11-10 15:00:00", horizon_days=5, multiplier=100.0, db=db)
    return order, seed, settled, fills


def reflection_reply(order_id, text="The entry came from a dated catalyst and the first measure says the read was right."):
    return ModelReply(json.dumps({"reflections": [{"order_id": order_id, "lesson": text}], "data_gaps": []}))


def settlement(settled, fills):
    return mg.Settlement(settled, tuple(fills), 110.0, "2026-11-10 15:00:00", 5, 100.0)


def test_TA_43_the_reflection_is_written_only_on_lessons_settled_from_orders_that_executed(world):
    db, desk, decision = world
    order, seed, settled, fills = executed_lesson(world)
    model = ScriptedChat(reflection_reply(order))
    out = mg.reflect(model, desk_id=desk, as_of=AS_OF, settlements=[settlement(settled, fills)], db=db)
    assert out.result.valid, out.result.check.problems and len(out.written) == 1 and out.rejected == ()
    new = out.written[0]
    assert new.status == "settled" and new.order_id == order and new.decision_id == decision and new.proposed_by == "desk-manager"
    assert new.outcome["entry_price"] == 101.0 and new.outcome["entry_price"] != 100.0                    # from the real entry fill, not the signal
    assert new.measured_from == "2026-11-02 15:00:00" and "dated catalyst" in new.text
    facts_seen = model.calls[0]["messages"][0].content
    assert '"measured_from"' in facts_seen and str(order) in facts_seen and "Task\nWrite one short lesson" in model.calls[0]["system"]


def test_TA_43_a_lesson_that_lessons_settle_has_not_measured_is_not_reflected_on(world):
    db, desk, decision = world
    order = make_order(db, desk, decision_id=decision, broker_order_id="ORD-2")
    seed = lessons.propose(desk, decision, order, "Executed order.", "desk-manager", db=db)
    with pytest.raises(mg.NotSettled):
        mg.reflect(ScriptedChat(reflection_reply(order)), desk_id=desk, as_of=AS_OF, settlements=[mg.Settlement(seed, ())], db=db)
    nothing = mg.reflect(ScriptedChat(reflection_reply(order)), desk_id=desk, as_of=AS_OF, settlements=[], db=db)
    assert nothing.result.valid and nothing.written == ()


def test_TA_43_a_decision_without_an_executed_order_produces_no_lesson(world):
    db, desk, decision = world
    refused = make_order(db, desk, decision_id=decision, broker_order_id=None, rule_result="refused", http_status=None)
    waiting = make_order(db, desk, decision_id=decision, broker_order_id=None, rule_result="needs_owner", http_status=None)
    batch = mg.propose_lessons(desk, [(decision, refused, "A lesson."), (decision, waiting, "Another."), (decision, 9999, "Missing.")], db=db)
    assert batch.written == () and [o for o, _ in batch.rejected] == [refused, waiting, 9999] and all(w.startswith("NotExecuted") for _, w in batch.rejected)
    with pytest.raises(NotExecuted):
        lessons.propose(desk, decision, refused, "A lesson.", "desk-manager", db=db)
    learning = mg.learn_from_order(ScriptedChat(reflection_reply(refused)), desk_id=desk, decision_id=decision, order_id=refused, as_of=AS_OF, thesis="x", fills=[], db=db)
    assert learning.status == "not_executed" and learning.seed is None
    with db.read() as u:
        assert u.session.query("SELECT * FROM lessons") == []


def test_TA_43_a_window_that_has_not_traded_leaves_the_lesson_pending_and_no_reflection_is_asked(world):
    db, desk, decision = world
    order = make_order(db, desk, decision_id=decision, broker_order_id="ORD-3")
    model = ScriptedChat(reflection_reply(order))
    learning = mg.learn_from_order(model, desk_id=desk, decision_id=decision, order_id=order, as_of=AS_OF, thesis="A dated catalyst.", horizon_days=5,
                                   fills=[fill("ORD-3", "BUY", 1, 50.0, "2026-11-02 15:00:00", 0.0)], mark_price=55.0, mark_at="2026-11-03 15:00:00", db=db)
    assert learning.status == "pending" and learning.seed.status == "proposed" and model.calls == []


def test_TA_43_the_full_step_measures_from_the_entry_reflects_and_stores_both_lessons(world):
    db, desk, decision = world
    order = make_order(db, desk, decision_id=decision, broker_order_id="ORD-4", price=49.0)
    model = ScriptedChat(reflection_reply(order))
    learning = mg.learn_from_order(model, desk_id=desk, decision_id=decision, order_id=order, as_of=AS_OF, thesis="A dated catalyst.", horizon_days=5, multiplier=100.0,
                                   fills=[fill("ORD-4", "BUY", 1, 50.0, "2026-11-02 15:00:00", 0.0)], mark_price=55.0, mark_at="2026-11-09 15:00:00", db=db)
    assert learning.status == "reflected" and learning.settled.outcome["entry_price"] == 50.0 and learning.settled.outcome["return"] == pytest.approx(0.1)
    assert len(learning.reflection.written) == 1
    again = mg.learn_from_order(ScriptedChat(reflection_reply(order)), desk_id=desk, decision_id=decision, order_id=order, as_of=AS_OF, thesis="A dated catalyst.",
                                horizon_days=5, multiplier=100.0, fills=[fill("ORD-4", "BUY", 1, 50.0, "2026-11-02 15:00:00", 0.0)], mark_price=55.0,
                                mark_at="2026-11-09 15:00:00", db=db)
    assert again.seed.id == learning.seed.id                                                                # the same order is the same seed


def test_TA_43_a_reflection_about_an_order_that_is_not_in_the_facts_is_invalid_and_writes_nothing(world):
    db, desk, _ = world
    order, seed, settled, fills = executed_lesson(world, "ORD-5")
    out = mg.reflect(ScriptedChat(reflection_reply(order + 50)), desk_id=desk, as_of=AS_OF, settlements=[settlement(settled, fills)], db=db)
    assert not out.result.valid and out.written == ()
    twice = ModelReply(json.dumps({"reflections": [{"order_id": order, "lesson": "One."}, {"order_id": order, "lesson": "Two."}], "data_gaps": []}))
    out = mg.reflect(ScriptedChat(twice), desk_id=desk, as_of=AS_OF, settlements=[settlement(settled, fills)], db=db)
    assert not out.result.valid and any("two reflections" in p for p in out.result.check.problems)
    made_up = mg.reflect(ScriptedChat(reflection_reply(order, "The order made 87 percent.")), desk_id=desk, as_of=AS_OF, settlements=[settlement(settled, fills)], db=db)
    assert not made_up.result.valid and "87" in made_up.result.check.unverified


def test_TA_43_grant_has_only_his_tools_when_he_reflects(world):
    db, desk, _ = world
    order, seed, settled, fills = executed_lesson(world, "ORD-6")
    asked = ScriptedChat(call("order_place", symbol="AAPL"), reflection_reply(order))
    out = mg.reflect(asked, desk_id=desk, as_of=AS_OF, settlements=[settlement(settled, fills)], write=False, db=db)
    assert not out.result.valid and any("order_place" in p for p in out.result.check.problems)
    box = mg.grant_toolbox(desk, db=db)
    done = box.call("lessons_propose", {"decision_id": 1, "order_id": 999, "text": "A lesson."})
    assert not done.ok and done.error_kind == "blocked" and "not_executed" in done.message              # the tool of the seat checks the order too


# ---- the postmortem of the views, CTS-024 -----------------------------------------------------------------------------------------

def closes(symbol_prices, start="2026-11-02"):
    index = pd.bdate_range(start, periods=len(symbol_prices))
    return pd.Series(symbol_prices, index=index, dtype=float)


def view(source="A16", value=0.7, regime="neutral", order=1, entry="2026-11-02", price=100.0, bias=1, symbol="AAPL", minutes=120):
    at = datetime.fromisoformat(entry).replace(tzinfo=timezone.utc)
    return mg.ViewRecord(source, symbol, value, 70, at - timedelta(minutes=minutes), at, price, bias, order, "quote", regime)


def regimes(by_day):
    return lambda stamp: by_day.get((stamp - timedelta(days=1)).date().isoformat(), "neutral")      # the cut-off is the start of the day after the exit session


def test_CTS_024_every_view_is_classified_at_five_and_twenty_days_with_the_regime_at_the_signal_and_at_the_exit():
    series = closes([100 + i for i in range(40)])                        # a steady rise: +5 at day 5 and +20 at day 20 (calendar days)
    views = [view("A16", 0.7), view("A17", -0.6, regime="hostile"), view("A18", 0.0, bias=1), view("A19", -0.5, regime="favorable")]
    exit_regime = regimes({d.date().isoformat(): "hostile" for d in series.index})
    table = mg.classify_views(views, closes={"AAPL": series}, as_of=datetime(2026, 12, 31, tzinfo=timezone.utc), regime_at=exit_regime)
    by = {(r["source"], r["horizon_days"]): r for r in table.rows}
    assert by[("A16", 5)]["class"] == "true_positive" and by[("A16", 20)]["class"] == "true_positive"
    assert by[("A17", 5)]["class"] == "false_positive" and by[("A17", 5)]["regime_at_exit"] == "hostile"
    assert by[("A18", 5)]["class"] == "missed_opportunity"
    assert by[("A19", 5)]["class"] == "regime_mismatch" and by[("A19", 5)]["regime_at_signal"] == "favorable"
    assert by[("A16", 5)]["raw_price_return"] > 0 and by[("A16", 20)]["raw_price_return"] > by[("A16", 5)]["raw_price_return"]
    assert "raw price returns" in table.facts()["notice"] and set(table.facts()["classes"]) == set(mg.CLASSES)


def test_CTS_024_a_horizon_that_has_not_traded_is_pending_and_the_price_never_looks_ahead():
    series = closes([100 + i for i in range(40)])
    early = datetime(2026, 11, 10, tzinfo=timezone.utc)                                          # day 8 after the entry
    table = mg.classify_views([view()], closes={"AAPL": series}, as_of=early)
    assert [(r["horizon_days"], r["status"]) for r in table.rows] == [(5, "measured"), (20, "pending")]
    five = next(r for r in table.rows if r["horizon_days"] == 5)
    assert five["exit_session"] == "2026-11-06"                                                    # the last close on or before day 5, never one after it
    assert five["raw_price_return"] == pytest.approx(0.04)                                         # 104 over 100: not the 106 of day 9 that a forward search would find
    gap = closes([100, 101, 102]).copy()                                                           # the only price after the horizon is not used
    far = mg.classify_views([view()], closes={"AAPL": gap}, as_of=datetime(2026, 12, 31, tzinfo=timezone.utc))
    assert far.sources == () or all(r["status"] != "measured" for r in far.rows if r["horizon_days"] == 20)
    assert any("no close near day 20" in w or "no close near day 5" in w for w in far.left_out)


def test_CTS_024_a_record_without_a_usable_price_is_left_out_and_never_repaired():
    series = closes([100 + i for i in range(40)])
    broken = view("A20", 0.5, price=0.0)
    unknown = view("A21", 0.5, symbol="MSFT")
    table = mg.classify_views([broken, unknown, view()], closes={"AAPL": series}, as_of=datetime(2026, 12, 31, tzinfo=timezone.utc))
    assert [s["source"] for s in table.sources] == ["A16"]
    assert any("A20" in w and "no usable price" in w for w in table.left_out) and any("A21" in w for w in table.left_out)


def test_CTS_024_only_executed_orders_enter_and_the_ones_that_did_not_are_listed(world):
    db, desk, decision = world
    good = make_order(db, desk, decision_id=decision, broker_order_id="X-1")
    refused = make_order(db, desk, decision_id=decision, broker_order_id=None, rule_result="refused", http_status=None)
    series = closes([100 + i for i in range(40)])
    model = ScriptedChat(ModelReply(json.dumps(postmortem_body(views=1))))
    table, result = mg.postmortem(model, desk_id=desk, as_of=datetime(2026, 12, 31, tzinfo=timezone.utc), views=[view("A16", order=good), view("A17", order=refused)],
                                  closes={"AAPL": series}, db=db)
    assert [s["source"] for s in table.sources] == ["A16"] and any("A17" in w and "did not execute" in w for w in table.left_out)
    assert result.valid, result.check.problems
    assert "A17" not in model.calls[0]["messages"][0].content.split("sources")[1].split("rows")[0]


def postmortem_body(*, views, source="A16", change="none", dominant="true_positive", **patch):
    body = {"summary": "One source was right at both horizons.", "raw_return_notice": "These are raw price returns from the real entry, not profit in money.",
            "sources": [{"source": source, "views": views, "dominant_class": dominant, "weight_change": change, "text": "The view held at both horizons."}],
            "improvements": [{"source": source, "text": "Keep reading the criteria before the view."}], "left_out": [], "data_gaps": []}
    body.update(patch)
    return body


def test_CTS_024_with_fewer_than_twenty_views_no_change_of_weight_is_suggested(world):
    db, desk, decision = world
    order = make_order(db, desk, decision_id=decision, broker_order_id="X-2")
    series = closes([100 + i for i in range(40)])
    when = datetime(2026, 12, 31, tzinfo=timezone.utc)
    for change, ok in (("none", True), ("raise", False)):
        model = ScriptedChat(ModelReply(json.dumps(postmortem_body(views=1, change=change))))
        _, result = mg.postmortem(model, desk_id=desk, as_of=when, views=[view(order=order)], closes={"AAPL": series}, db=db)
        assert result.valid is ok, result.check.problems
    _, text = mg.postmortem(ScriptedChat(ModelReply(json.dumps(postmortem_body(views=1, improvements=[{"source": "A16", "text": "Raise the weight of this source."}])))),
                            desk_id=desk, as_of=when, views=[view(order=order)], closes={"AAPL": series}, db=db)
    assert not text.valid and any("no change of weight" in p for p in text.check.problems)


def test_CTS_024_with_twenty_views_of_a_source_a_change_of_weight_may_be_suggested(world):
    db, desk, decision = world
    order = make_order(db, desk, decision_id=decision, broker_order_id="X-3")
    series = closes([100 + i for i in range(40)])
    views = [view("A16", 0.7, order=order, minutes=10 + n) for n in range(mg.MIN_VIEWS)]
    model = ScriptedChat(ModelReply(json.dumps(postmortem_body(views=20, change="raise"))))
    table, result = mg.postmortem(model, desk_id=desk, as_of=datetime(2026, 12, 31, tzinfo=timezone.utc), views=views, closes={"AAPL": series}, db=db)
    assert table.sources[0]["views"] == 20 and result.valid, result.check.problems


def test_CTS_024_the_answer_must_match_the_table_and_say_that_the_returns_are_raw(world):
    db, desk, decision = world
    order = make_order(db, desk, decision_id=decision, broker_order_id="X-4")
    series = closes([100 + i for i in range(40)])
    when = datetime(2026, 12, 31, tzinfo=timezone.utc)

    def ask(body):
        return mg.postmortem(ScriptedChat(ModelReply(json.dumps(body))), desk_id=desk, as_of=when, views=[view(order=order)], closes={"AAPL": series}, db=db)[1]

    assert not ask(postmortem_body(views=3)).valid
    assert not ask(postmortem_body(views=1, dominant="false_positive")).valid
    assert not ask(postmortem_body(views=1, raw_return_notice="These are profits.")).valid
    assert not ask(postmortem_body(views=1, source="A99")).valid
    assert not ask(postmortem_body(views=1, summary="The gain was 99 percent.")).valid
    assert not ask(postmortem_body(views=1, change="raise")).valid


def test_CTS_024_grant_has_only_his_tools_when_he_writes_the_postmortem(world):
    db, desk, decision = world
    order = make_order(db, desk, decision_id=decision, broker_order_id="X-5")
    series = closes([100 + i for i in range(40)])
    asked = ScriptedChat(call("quant_call", name="risk.var"), ModelReply(json.dumps(postmortem_body(views=1))))
    _, result = mg.postmortem(asked, desk_id=desk, as_of=datetime(2026, 12, 31, tzinfo=timezone.utc), views=[view(order=order)], closes={"AAPL": series}, db=db)
    assert not result.valid and any("quant_call" in p for p in result.check.problems)


def test_CTS_024_the_prompt_has_the_postmortem_skill_only_and_the_skill_says_the_traps_in_words():
    system = mg.system_prompt("postmortem", line="SWO", desk_id=1, as_of=AS_OF)
    assert "## Skill: signal-postmortem" in system and "morning-note" not in system
    text = (KIT / "skills" / "signal-postmortem" / "SKILL.md").read_text(encoding="utf-8")
    for phrase in ("raw price return", "Never read a later price", "fewer than twenty views", "Do not repair"):
        assert phrase.lower() in text.lower() or phrase.lower().replace("never read", "never look") in text.lower(), phrase


# ---- the memory of the desk in two layers, SKL-009 ---------------------------------------------------------------------------------

def test_SKL_009_the_raw_debate_lives_in_run_records_and_only_approved_decisions_feed_later_cycles(ready):
    log = mg.decision_log(ready.desk_id, db=ready.db)
    raw_id = log.record_raw(symbol="AAPL", as_of=AS_OF, debate={"rounds": 2, "summary": "bull and bear"}, judge={"rating": "APPROVE"})
    assert runs.get(raw_id, db=ready.db)["kind"] == "desk_debate" and log.raw()[0]["record"]["outputs"]["debate"]["rounds"] == 2
    assert log.approved() == [] and log.rejected_cases() == []                          # the raw debate never reaches layer 2
    levels.set_level(ready.desk_id, 2, who=ready.owner, db=ready.db)
    a = hexec.submit(proposal(ready, qty="1"), who=AGENT, db=ready.db)
    b = hexec.submit(proposal(ready, qty="2"), who=AGENT, db=ready.db)
    hexec.approve(a.proposal_id, who=ready.owner, db=ready.db)
    hexec.reject(b.proposal_id, who=ready.owner, db=ready.db)
    assert [d["decision_id"] for d in log.approved()] == [a.decision_id]
    assert [d["decision_id"] for d in log._decided("rejected", 10)] == [b.decision_id] and b.decision_id not in [d["decision_id"] for d in log.approved()]


def test_SKL_009_the_case_of_a_proposal_is_read_from_its_legs_and_a_rejected_one_is_not_reopened_without_the_owner(ready):
    body = {"legs": [{"contract_id": "OPT:AAPL:2026-12-18:C:200", "side": "buy", "effect": "open", "qty": "1"},
                     {"contract_id": "OPT:AAPL:2027-01-15:C:210", "side": "sell", "effect": "open", "qty": "1"}]}
    assert mg.case_of(json.dumps(body)) == ("AAPL", "2026-12-18") and mg.case_of("not json") is None and mg.case_of({"legs": []}) is None
    log = mg.decision_log(ready.desk_id, db=ready.db)
    log._decided = lambda status, limit: [{"underlying": "AAPL", "expiration": "2026-12-18", "decision_id": 1, "topic": "x", "proposal": "x", "proposal_id": 1,
                                           "decided_at": None}] if status == "rejected" else []
    assert log.reopen_blocked("aapl", date(2026, 12, 18)) and log.reopen_blocked("AAPL", "2026-12-18")
    assert not log.reopen_blocked("AAPL", "2026-12-18", owner_order=True) and not log.reopen_blocked("AAPL", "2027-01-15") and not log.reopen_blocked("MSFT", "2026-12-18")


# ---- the mandate of the desk, AH-25 and AH-26 ---------------------------------------------------------------------------------------

def swo_desk_with_kit(db, tmp_path):
    from hokma.desks.swo.create import create_swo_desk

    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Test Fund")
    return create_swo_desk("Swing", 100_000, db=db)["id"]


def test_AH_25_the_mandate_is_edited_through_one_door_that_keeps_the_line_and_marks_who_approved_it(db, tmp_path):
    desk = swo_desk_with_kit(db, tmp_path)
    mandate = mg.load_desk_mandate(desk, db=db)
    data = mandate.model_dump(mode="json")
    data["risk"]["max_open_positions"] = 3
    data["notes"] = "Debit structures only."
    saved = mg.save_mandate(desk, data, by_owner=True, db=db)
    assert saved.risk.max_open_positions == 3 and mg.load_desk_mandate(desk, db=db).notes == "Debit structures only."
    with db.read() as u:
        row = u.session.one("SELECT version, approved_by FROM desk_mandates WHERE desk_id = :d", {"d": desk})
        text = u.session.one("SELECT mandate FROM desks WHERE id = :d", {"d": desk})["mandate"]
    assert (row["version"], row["approved_by"]) == (2, "owner") and "3 open positions" in text
    mg.save_mandate(desk, saved, by_owner=False, db=db)                         # an edit by anyone else is not the owner's approval
    with db.read() as u:
        assert u.session.one("SELECT approved_by, version FROM desk_mandates WHERE desk_id = :d", {"d": desk}) == {"approved_by": None, "version": 3}
    for bad, code in (({**data, "line": "LP", "variant": None}, "line_mismatch"), ({**data, "variant": "pead"}, "line_mismatch")):
        with pytest.raises(mg.MandateError) as exc:
            mg.save_mandate(desk, bad, by_owner=True, db=db)
        assert exc.value.code == code or exc.value.code == "line_mismatch"
    with pytest.raises(mg.MandateError) as missing:
        mg.save_mandate(99999, data, by_owner=True, db=db)
    assert missing.value.code == "no_desk"
    with pytest.raises(mg.MandateError):
        mg.load_desk_mandate(99999, db=db)


def test_AH_26_an_edit_with_extra_keys_or_instruments_fails_and_saves_nothing(db, tmp_path):
    from hokma.desks.core.spec_loader import SpecError

    desk = swo_desk_with_kit(db, tmp_path)
    data = mg.load_desk_mandate(desk, db=db).model_dump(mode="json")
    for patch in ({"extra_field": 1}, {"tickers": ["AAPL"]}, {"analysts": [{"key": "nobody"}]}):
        with pytest.raises(SpecError):
            mg.save_mandate(desk, {**data, **patch}, by_owner=True, db=db)
    with db.read() as u:
        assert u.session.one("SELECT version FROM desk_mandates WHERE desk_id = :d", {"d": desk})["version"] == 1


# ---- bug 1239: a structure of several legs is measured by the net of its legs, or it waits; it never fails the cycle -------------------------

LONG, SHORT = "SPYC600", "SPYC610"


def spread_fills(closed=False):
    out = [fill("SP-1", "BUY", 1, 8.0, "2026-11-02 15:00:00", 1.0, symbol=LONG), fill("SP-1", "SELL", 1, 4.0, "2026-11-02 15:00:00", 1.0, symbol=SHORT)]
    if closed:
        out += [fill("X-1", "SELL", 1, 11.0, "2026-11-05 15:00:00", 1.0, symbol=LONG), fill("X-2", "BUY", 1, 5.0, "2026-11-05 15:00:00", 1.0, symbol=SHORT)]
    return out


def test_a_debit_spread_still_open_leaves_the_cycle_alive_and_the_lesson_pending(desks_db):
    desk, decision = make_desk(desks_db)["id"], make_decision(desks_db)
    order = make_order(desks_db, desk, decision_id=decision, broker_order_id="SP-1")
    model = ScriptedChat(reflection_reply(order))
    learning = mg.learn_from_order(model, desk_id=desk, decision_id=decision, order_id=order, as_of=AS_OF, thesis="Debit spread.", fills=spread_fills(),
                                   mark_price=7.0, mark_at="2026-11-09 15:00:00", multiplier=100.0, db=desks_db)
    assert learning.status == "pending" and "structure" in learning.reason and learning.seed.status == "proposed" and model.calls == []


def test_a_fill_the_measure_does_not_understand_is_unsettled_not_a_failed_cycle(desks_db):
    desk, decision = make_desk(desks_db)["id"], make_decision(desks_db)
    order = make_order(desks_db, desk, decision_id=decision, broker_order_id="SP-1")
    odd = [fill("SP-1", "BUY", 1, 8.0, symbol=LONG), fill("SP-1", "SELL", 1, 4.0, symbol=LONG)]            # both sides of one symbol
    learning = mg.learn_from_order(ScriptedChat(reflection_reply(order)), desk_id=desk, decision_id=decision, order_id=order, as_of=AS_OF, thesis="x", fills=odd,
                                   db=desks_db)
    assert learning.status == "unsettled" and learning.seed.status == "proposed"


def test_a_closed_spread_settles_by_the_net_of_its_legs(desks_db):
    desk, decision = make_desk(desks_db)["id"], make_decision(desks_db)
    item = lessons.propose(desk, decision, make_order(desks_db, desk, decision_id=decision, broker_order_id="SP-1"), "Debit spread.", "risk")
    done = lessons.settle(item.id, fills=spread_fills(closed=True), multiplier=100.0, now="2026-11-06 00:00:00", db=desks_db)
    out = done.outcome
    assert done.status == "settled" and out["structure"] == "debit" and out["direction"] == "long" and out["symbol"] == "SPYC600+SPYC610"
    assert out["entry_price"] == pytest.approx(4.0) and out["exit_price"] == pytest.approx(6.0)               # net debit 8-4, net value 11-5
    assert out["return"] == pytest.approx(0.5) and out["pnl"] == pytest.approx((3.0 + -1.0) * 100 - 4.0) and out["costs"] == 4.0


def test_a_swo_desk_is_born_with_the_connected_providers_turned_on(db, monkeypatch):
    from hokma.desks import service
    from hokma.desks.swo.create import create_swo_desk

    class Fake:
        loaded = True

        def __init__(self):
            from hokma.market.providers import get_catalog

            self.catalog = get_catalog()

        def is_added(self, provider_id):
            return provider_id in ("schwab", "finnhub")

        def order(self, asset_class):
            return ["schwab", "finnhub"]

    monkeypatch.setattr(service, "_market_service", Fake)
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Test Fund")
    made = create_swo_desk("Swing con Schwab", 100_000, db=db)
    with db.read() as u:
        rows = {c.provider_id: c.enabled for c in u.desks.list_connections(made["id"])}
    assert rows == {"schwab": True, "finnhub": True} and made["connections"]["fallback_only"] is False
