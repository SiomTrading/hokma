"""The SWO desk tools and the runtime of the app (SPEC-019, B13): classified refusals, sizes checked before anything runs, one desk per call, and the
wiring of the cycle to the real model clients, the data layer and the back office. No network: the model client is a stub and the data is recorded."""
import json
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest
from exec_support import db, desks_db, env, ready  # noqa: F401  (fixtures)

from hokma.backoffice import book
from hokma.broker.types import Fill
from hokma.desks.plantel import SEATS
from hokma.desks.swo import app_runtime, graph
from hokma.desks.swo.create import desk_agent_slug
from hokma.kit import desk_tools, hokma_tools
from hokma.kit.hokma_tools import Caller

OWNER = None


def call(name, args, agent=OWNER):
    text, err = hokma_tools.call(name, args, Caller({"HOKMA_AGENT": agent} if agent else {}))
    try:
        return json.loads(text), err
    except ValueError:
        return text, err


@pytest.fixture
def desk(ready):
    out, err = call("desk_create_swo", {"name": "Tools swing", "capital": 100_000})
    assert not err, out
    ready.swo_id = out["desk_id"]
    ready.grant = desk_agent_slug(SEATS["A15"], ready.swo_id)
    yield ready
    graph.register_runtime_factory(None)


def test_the_tools_are_declared_with_strict_schemas_and_only_the_cycle_tools_that_act_write():
    listed = {d["name"]: d for d in hokma_tools.definitions()}
    for name, writes in (("desk_create_swo", True), ("desk_mandate_save", True), ("desk_cycle_run", True), ("desk_cycle_learn", True),
                         ("lessons_propose", True), ("desk_cycle_status", False), ("desk_signals", False)):
        assert hokma_tools.TOOLS[name][4] is writes and name in hokma_tools.NO_UNIT, name
        assert listed[name]["inputSchema"]["additionalProperties"] is False and listed[name]["description"]
    assert set(desk_tools.TOOLS) == {"desk_create_swo", "desk_mandate_save", "desk_cycle_run", "desk_cycle_status", "desk_cycle_learn", "desk_signals",
                                    "lessons_propose", "desk_analysis_start", "desk_analysis_get", "desk_analysis_list"}
    for name in desk_tools.TOOLS:                                       # no tool takes a path or a pattern, and none lets the agent declare a control
        props = set(hokma_tools.TOOLS[name][1])
        assert not props & {"path", "file", "regex", "pattern", "origin", "line", "effect", "price", "risk", "owner_order", "level"}, name


def test_the_desk_is_created_with_the_sizes_checked_and_a_bad_request_writes_nothing(ready):
    bad = [{"name": "", "capital": 1000}, {"name": "x" * 81, "capital": 1000}, {"name": "A", "capital": True}, {"name": "A", "capital": 5},
           {"name": "A", "capital": 1000, "assets": ["AAPL"] * 51}, {"name": "A", "capital": 1000, "assets": "AAPL"},
           {"name": "A", "capital": 1000, "mandate": {"pad": "x" * 13_000}}, {"name": "A", "capital": 1000, "variant": "dt"},
           {"name": "A", "capital": 1000, "mandate": {"name": "m", "line": "DT", "strategies": []}}]
    for args in bad:
        out, err = call("desk_create_swo", args)
        assert err, args
    with ready.db.read() as u:
        assert u.session.query("SELECT id FROM desks WHERE name = 'A'", {}) == []


def test_a_pead_desk_carries_all_the_seats_and_the_owner_flag_is_false_by_default(ready):
    out, err = call("desk_create_swo", {"name": "Drift swing", "capital": 50_000, "variant": "pead", "assets": ["AAPL", "MSFT"]})
    assert not err and out["variant"] == "pead" and len(out["seats"]) == 21 and out["mandate_approved_by_owner"] is False
    with ready.db.read() as u:
        assert u.session.one("SELECT approved_by FROM desk_mandates WHERE desk_id = :d", {"d": out["desk_id"]})["approved_by"] is None


def test_the_mandate_edit_goes_through_save_mandate_as_the_agent_and_never_changes_the_line(desk):
    from hokma.desks.agents.manager import load_desk_mandate

    data = load_desk_mandate(desk.swo_id, db=desk.db).model_dump(mode="json")
    data["risk"] = {"max_risk_per_trade_pct": 0.5, "max_open_positions": 3, "max_drawdown_pct": 8}
    out, err = call("desk_mandate_save", {"desk_id": desk.swo_id, "mandate": data}, agent=desk.grant)
    assert not err and out["approved_by_owner"] is False
    assert load_desk_mandate(desk.swo_id, db=desk.db).risk.max_open_positions == 3
    with desk.db.read() as u:
        assert u.session.one("SELECT approved_by, version FROM desk_mandates WHERE desk_id = :d", {"d": desk.swo_id}) == {"approved_by": None, "version": 2}
    changed = {**data, "line": "DT"}
    out, err = call("desk_mandate_save", {"desk_id": desk.swo_id, "mandate": changed}, agent=desk.grant)
    assert err and out["error_code"] == "bad_input"
    out, err = call("desk_mandate_save", {"desk_id": desk.swo_id, "mandate": {"pad": "x" * 13_000}}, agent=desk.grant)
    assert err and "too large" in out["message"]
    out, err = call("desk_mandate_save", {"desk_id": desk.desk_id, "mandate": data}, agent=desk.grant)          # a desk of another line
    assert err and out["error_code"] == "bad_input"


def test_the_cycle_tool_checks_its_arguments_and_only_queues_the_run(desk, monkeypatch):
    graph.register_runtime_factory(None)
    monkeypatch.setattr(app_runtime, "build_runtime", lambda desk_id: (_ for _ in ()).throw(graph.CycleError("no_data", "the chain is down")))
    for bad in ({"desk_id": desk.swo_id, "symbol": ""}, {"desk_id": desk.swo_id, "symbol": "A" * 41}, {"desk_id": desk.swo_id, "symbol": "AAPL", "as_of": "not a date"},
                {"desk_id": desk.swo_id, "symbol": "AAPL", "as_of": "2999-01-01"}, {"desk_id": desk.swo_id, "symbol": "AAPL", "strategy": ""},
                {"desk_id": 0, "symbol": "AAPL"}, {"desk_id": desk.desk_id, "symbol": "AAPL"}):
        out, err = call("desk_cycle_run", bad)
        assert err and out["error_code"] in ("bad_input", "blocked"), bad
        assert graph._FACTORY is None                                  # nothing was registered by a call that failed its arguments
    assert graph._FACTORY is None                                      # the tool server never builds a runtime for a cycle: it only queues it
    out, err = call("desk_cycle_run", {"desk_id": desk.swo_id, "symbol": "AAPL", "as_of": "2026-11-20T15:00:00Z"})
    assert not err and out["status"] == "queued" and out["run_id"] and graph._FACTORY is None
    from hokma.backoffice import runs

    assert runs.get(out["run_id"])["status"] == "running"            # opened, waiting for the app
    other = lambda desk_id: None                                      # noqa: E731
    graph.register_runtime_factory(other)
    desk_tools.ensure_runtime()
    assert graph._FACTORY is other                                     # a runtime that is already there is left as it is


def test_a_desk_chief_works_only_in_the_desk_of_its_project_and_the_read_tools_say_no_data(desk):
    for name, args in (("desk_cycle_run", {"symbol": "AAPL"}), ("desk_cycle_status", {}), ("desk_signals", {}), ("desk_cycle_learn", {"run_id": 1}),
                       ("lessons_propose", {"items": [{"decision_id": 1, "order_id": 1, "text": "x"}]})):
        out, err = call(name, {"desk_id": desk.swo_id, **args}, agent="desk-chief")
        assert err and out["error_code"] == "blocked", name
    out, err = call("desk_cycle_status", {"desk_id": desk.swo_id})
    assert err and out["error_code"] == "no_data"
    out, err = call("desk_cycle_status", {"desk_id": desk.swo_id, "limit": 21})
    assert err and out["error_code"] == "bad_input"
    out, err = call("desk_signals", {"desk_id": desk.swo_id})
    assert err and out["error_code"] == "no_data"
    for bad in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"symbol": "../x"}, {"run_id": 0}):
        assert call("desk_signals", {"desk_id": desk.swo_id, **bad})[1], bad


def test_the_signals_come_back_newest_first_with_the_view_and_the_abstentions(desk):
    stamp = datetime(2026, 11, 20, tzinfo=timezone.utc).isoformat()
    with desk.db.write() as u:
        for seat, value, abstained in (("A16", 0.4, 0), ("A17", None, 1)):
            u.session.insert_row("desk_signals", {"desk_id": desk.swo_id, "run_id": None, "seat_code": seat, "symbol": "AAPL", "as_of": stamp, "value": value,
                                                  "confidence": 60, "abstained": abstained, "signal_json": json.dumps({"seat": seat}), "snapshot_hash": "h"})
    out, err = call("desk_signals", {"desk_id": desk.swo_id, "symbol": "aapl"}, agent=desk.grant)
    assert not err and [s["seat"] for s in out["signals"]] == ["A17", "A16"]
    assert out["signals"][0]["abstained"] is True and out["signals"][0]["value"] is None and out["signals"][1]["signal"] == {"seat": "A16"}


def test_the_lesson_tool_checks_the_size_of_the_batch_and_of_each_text_and_never_takes_the_author_from_the_agent(desk):
    for items in ([], [{"decision_id": 1, "order_id": 1, "text": "x"}] * 11, [{"decision_id": 1, "order_id": 1, "text": ""}],
                  [{"decision_id": 1, "order_id": 1, "text": "x" * 1001}], [{"decision_id": 0, "order_id": 1, "text": "x"}],
                  [{"decision_id": 1, "order_id": 1, "text": "x", "proposed_by": "me"}]):
        out, err = call("lessons_propose", {"desk_id": desk.swo_id, "items": items}, agent=desk.grant)
        assert err and out["error_code"] == "bad_input", items
    assert "proposed_by" not in hokma_tools.TOOLS["lessons_propose"][1]


def test_the_learn_tool_needs_a_cycle_of_the_desk_and_says_so(desk):
    graph.register_runtime_factory(lambda desk_id: object())
    out, err = call("desk_cycle_learn", {"desk_id": desk.swo_id, "run_id": 999}, agent=desk.grant)
    assert err and out["error_code"] == "no_data" and "no_run" in out["message"]
    assert call("desk_cycle_learn", {"desk_id": desk.swo_id, "run_id": 0}, agent=desk.grant)[1]


# ---- the runtime of the app --------------------------------------------------------------------------------------------------------

class Stub:
    model = "stub"

    def __init__(self, slug, desk_id):
        self.slug, self.desk_id = slug, desk_id


def test_the_runtime_gives_each_seat_its_own_agent_of_the_desk_and_two_separate_reviewers(desk, monkeypatch):
    made = []
    monkeypatch.setattr("hokma.llm.factory.make_llm", lambda slug, desk_id=None, **kw: made.append((slug, desk_id)) or Stub(slug, desk_id))
    runtime = app_runtime.build_runtime(desk.swo_id)
    assert made == [(desk_agent_slug(SEATS["A11"], desk.swo_id), desk.swo_id)] and runtime.trader_model.slug == made[0][0]
    first, second, judge, again = runtime.chat("A10:review1"), runtime.chat("A10:review2"), runtime.chat("A10"), runtime.chat("A10:review1")
    assert first is not second and first is again and judge is not first
    slugs = [m[0] for m in made[1:]]
    assert slugs == [desk_agent_slug(SEATS[c], desk.swo_id) for c in ("A10", "A13", "A10")]
    assert runtime.caller.agent == desk_agent_slug(SEATS["A11"], desk.swo_id) and runtime.fills_of is app_runtime.fills_of
    assert runtime.load is app_runtime.load and runtime.trade_context is app_runtime.trade_context


def test_the_fills_of_an_order_are_the_ones_the_back_office_booked_from_the_broker(desk):
    sheet_id = "OPT:AAPL:2026-12-18:C:200"
    for n, (qty, price) in enumerate(((1, "5.20"), (2, "5.25"))):
        book.record_fill(Fill(desk.account_id, sheet_id, "buy", qty, float(price), 0.65 * qty, "2026-11-20 15:00:00", order_id="ORD1", multiplier=D(100), contract_id=sheet_id),
                         desk_id=desk.desk_id, source="sim", source_ref=f"ORD1:{n}", db=desk.db)
    book.record_fill(Fill(desk.account_id, sheet_id, "buy", 9, 9.0, 0, "2026-11-20 15:00:00", order_id="ORD10", multiplier=D(100), contract_id=sheet_id),
                     desk_id=desk.desk_id, source="sim", source_ref="ORD10:0", db=desk.db)
    got = app_runtime.fills_of({"broker_order_id": "ORD1", "account_id": desk.account_id})
    assert [(f.side, f.qty, f.price, f.commission, f.multiplier, f.contract_id) for f in got] == [("buy", 1.0, 5.2, 0.65, D(100), sheet_id), ("buy", 2.0, 5.25, 1.3, D(100), sheet_id)]
    assert app_runtime.fills_of({"broker_order_id": "ORD", "account_id": desk.account_id}) == []                     # a prefix is not the same order
    assert app_runtime.fills_of({"broker_order_id": None, "account_id": desk.account_id}) == []


def test_the_data_of_a_symbol_that_is_not_ready_stops_the_cycle_with_the_reason_and_invents_nothing():
    import pandas as pd

    with pytest.raises(graph.CycleError) as exc:
        app_runtime._statements([])
    assert exc.value.code == "no_data"
    with pytest.raises(graph.CycleError):
        app_runtime._statements([{"report_period": "2026-03-31", "filing_date": "2026-05-01", "note": "text only"}])
    frame = app_runtime._statements([{"report_period": "2026-06-30", "filing_date": "2026-08-01", "revenue": 10.0, "ticker": "AAPL"},
                                     {"report_period": "2026-03-31", "filing_date": "2026-05-01", "revenue": 9.0, "ticker": "AAPL"}])
    assert list(frame.index) == ["2026-03-31", "2026-06-30"] and list(frame.columns) == ["revenue", "filed"] and isinstance(frame, pd.DataFrame)
    from hokma.data import sentinels

    with pytest.raises(graph.CycleError) as bad:
        app_runtime._need(sentinels.no_data("src", "gone"), "bars of AAPL")
    assert "bars of AAPL" in str(bad.value) and bad.value.code == "no_data"
    assert app_runtime._contract_id("AAPL", {"strike": 200.0, "expiration": "2026-12-18", "type": "call"}) == "OPT:AAPL:2026-12-18:C:200"
    assert app_runtime._contract_id("AAPL", {"strike": "187.5", "expiration": "2026-12-18", "type": "put"}) == "OPT:AAPL:2026-12-18:P:187.5"
