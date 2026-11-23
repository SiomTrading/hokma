"""The tools of execution, contracts, calendar and the back office (SPEC-018 B10)."""
import json
from decimal import Decimal

import pytest
from exec_support import NQ, db, desks_db, env, ready  # noqa: F401

from hokma import exec as hexec
from hokma.exec import levels
from hokma.kit import hokma_tools
from hokma.kit.hokma_tools import Caller


def call(name, args, agent="hugo"):
    text, err = hokma_tools.call(name, args, Caller({"HOKMA_AGENT": agent}))
    try:
        return json.loads(text), err
    except ValueError:
        return text, err


def body(**extra):
    return {"line": "DT", "order_type": "market", "thesis": "test", "legs": [{"contract_id": NQ, "side": "buy", "effect": "open", "qty": 1}], **extra}


def test_an_agent_proposes_and_at_level_1_nothing_goes_out(ready):
    out, err = call("exec_propose", {"desk_id": ready.desk_id, "proposal": body()})
    assert not err and out["status"] == "analysis_only"
    status, err = call("exec_status", {"proposal_id": out["proposal_id"]})
    assert not err and status["status"] == "analysis_only"
    pending, err = call("exec_pending", {"desk_id": ready.desk_id})
    assert not err and pending["pending"] == []


def test_the_proposal_is_validated_before_anything_runs(ready):
    for bad in (body(color="red"), body(legs=[]), {**body(), "legs": [{"contract_id": NQ, "side": "buy", "effect": "open", "qty": 1}] * 5},
                body(origin="chat"), {"line": "DT", "order_type": "market", "legs": [{"contract_id": "NQ", "side": "buy", "effect": "open", "qty": 1}]}):
        out, err = call("exec_propose", {"desk_id": ready.desk_id, "proposal": bad})
        assert err and json.loads(json.dumps(out))["error_code"] in ("bad_input", "refused"), bad
    out, err = call("exec_propose", {"desk_id": True, "proposal": body()})
    assert err


def test_an_agent_cannot_raise_the_level_flatten_or_approve_and_the_refusals_are_classified(ready):
    out, err = call("desk_level_set", {"desk_id": ready.desk_id, "level": 3, "rules": {"id": "x", "max_qty": 1}})
    assert err and out["error_code"] == "blocked"
    out, err = call("desk_level_set", {"desk_id": ready.desk_id, "level": 2})
    assert err and out["error_code"] == "blocked"
    out, err = call("exec_flatten", {"desk_id": ready.desk_id, "reason": "now"})
    assert err and out["error_code"] == "blocked"
    levels.set_level(ready.desk_id, 3, rules={"id": "r", "max_qty": 5, "symbols": ["FUT:NQ"]}, who=ready.owner, db=ready.db)
    out, err = call("desk_level_set", {"desk_id": ready.desk_id, "level": 1})                      # lowering is free
    assert not err and out["level"] == 1
    got, err = call("desk_level_get", {"desk_id": ready.desk_id})
    assert not err and got["level"] == 1
    refused, err = call("exec_propose", {"desk_id": ready.desk_id, "proposal": body(legs=[{"contract_id": "FUT:ZZZ:2026-12", "side": "buy", "effect": "open", "qty": 1}])})
    assert err and refused["error_code"] == "refused" and "no_contract" in refused["message"]


def test_contract_get_gives_the_sheet_and_an_unknown_symbol_is_no_data(ready):
    out, err = call("contract_get", {"symbol": "NQZ26"})
    assert not err and out["multiplier"] == "20" and out["contract_id"] == "FUT:NQ:2026-12"
    out, err = call("contract_get", {"symbol": "OPT:SPY:2026-12-18:C:123.4567"})
    assert err and out["error_code"] == "no_data"


def test_a_level_3_desk_sends_alone_and_the_back_office_tools_read_the_book(ready, tmp_path, monkeypatch):
    levels.set_level(ready.desk_id, 3, rules={"id": "r", "max_qty": 5, "symbols": ["FUT:NQ"], "order_types": ["market"]}, who=ready.owner, db=ready.db)
    # the notional of one NQ is the whole risk when the broker places no stop: the rule needs max_qty only, so it covers it
    out, err = call("exec_propose", {"desk_id": ready.desk_id, "proposal": body()})
    assert not err and out["status"] == "filled"
    pos, err = call("book_positions", {"desk_id": ready.desk_id})
    assert not err and pos["positions"][0]["contract_id"] == NQ
    snap, err = call("book_snapshot", {"desk_id": ready.desk_id})
    assert not err and snap["positions_value"] is not None and snap["equity"] is not None
    pnl, err = call("book_pnl", {"desk_id": ready.desk_id, "start": "2020-01-01", "end": "2030-01-01"})
    assert not err and "net" in pnl
    nope, err = call("book_pnl", {"desk_id": ready.desk_id, "start": "2030-01-01", "end": "2020-01-01"})
    assert err
    monkeypatch.setattr("hokma.broker.sim.time.time", __import__("time").time)                 # the export writes a real zip
    exported, err = call("export_book", {"desk_id": ready.desk_id, "format": "csv"})
    assert not err and all("/" not in f and ".." not in f for f in exported["files"]) and exported["files"]
    none, err = call("recon_breaks", {"desk_id": ready.desk_id})
    assert err and none["error_code"] == "no_data"
    too_big = {"as_of": "2026-11-20", "lines": [{"ref": str(i), "kind": "fill", "amount": 1, "date": "2026-11-20"} for i in range(5001)]}
    out, err = call("recon_run", {"desk_id": ready.desk_id, "statement": too_big})
    assert err and out["error_code"] == "bad_input"
    clean = {"as_of": "2026-11-20", "lines": []}
    assert call("recon_run", {"desk_id": ready.desk_id, "statement": clean})[1] is True


def test_the_calendar_tools_validate_their_sizes(ready):
    out, err = call("calendar_events", {"start": "2026-01-01", "end": "2026-06-01"})
    assert err and out["error_code"] == "bad_input"
    out, err = call("calendar_windows", {"desk_id": ready.desk_id})
    assert not err and out["windows"] == []
    out, err = call("schedule_run", {"desk_id": ready.desk_id, "expr": "x" * 500, "instruction": "run"})
    assert err


def test_the_line_comes_from_the_desk_profile_and_a_different_line_is_refused(ready):
    out, err = call("exec_propose", {"desk_id": ready.desk_id, "proposal": body(line="SWO")})
    assert err and out["error_code"] == "refused" and "line_mismatch" in out["message"]
    out, err = call("exec_propose", {"desk_id": ready.desk_id, "proposal": {k: v for k, v in body().items() if k != "line"}})
    assert not err and out["status"] == "analysis_only"                          # the line was taken from the profile
    from hokma import exec as hexec
    from hokma.exec.proposal import Leg, OrderProposal
    wrong = OrderProposal(ready.desk_id, "LP", "desk", (Leg(NQ, "buy", "open", Decimal(1)),), "market", created_by="x")
    got = hexec.submit(wrong, who=hokma_tools_who(), db=ready.db)
    assert (got.status, got.code) == ("refused", "line_mismatch")
    with ready.db.write() as u:
        u.session.execute("DELETE FROM desk_profile WHERE desk_id = :d", {"d": ready.desk_id})
    assert hexec.submit(wrong, who=hokma_tools_who(), db=ready.db).code == "no_line"


def hokma_tools_who():
    from hokma.broker.service import Who

    return Who(agent="hugo")
