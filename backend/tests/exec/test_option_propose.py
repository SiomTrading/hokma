"""The Option detail panel actions of a desk (SPEC-028, 3339-B): the selection the panel opens with, saved strategies, the proposal through the door of
execution (account by strategy, rules, premium switch, level of the desk) and the two chat tools.

The chain and the clock are replaced at their seams; the door of execution, the simulated broker, the guards and the database are the real ones."""
import json
from datetime import date
from decimal import Decimal as D

import pytest
from exec_support import db, desks_db, env, ready  # noqa: F401  (fixtures)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from trader_support import C200, C210, C220, P170, P180, P190, STOCK, sheet

from hokma.broker import desk_accounts, desk_rules, service
from hokma.contracts import sheet as sheet_store
from hokma.desks import option_actions, option_detail
from hokma.desks import service as desk_service
from hokma.desks.api import router as desks_router
from hokma.desks.errors import DeskError
from hokma.desks.swo import premium_switch
from hokma.exec import levels
from hokma.i18n import t
from hokma.kit import hokma_tools
from hokma.rpc.ws import SESSION_TOKEN

TODAY = date(2026, 11, 20)
P200 = "OPT:AAPL:2026-12-18:P:200"
QUOTES = {C200: ("5.10", "5.20"), C210: ("1.80", "1.90"), C220: ("0.60", "0.70"), P190: ("2.40", "2.50"), P180: ("1.10", "1.20"), P170: ("0.40", "0.50"),
          P200: ("5.00", "5.10")}
BULL = {"symbol": "AAPL", "expiration": "2026-12-18", "structure": "debit_call_spread", "strikes": [200.0, 210.0], "quantity": 1}


def cell(bid, ask):
    return {"bid": float(bid), "ask": float(ask), "iv": 0.3, "delta": 0.5, "open_interest": 500}


def chain_rows():
    out = []
    for strike in (170.0, 180.0, 190.0, 200.0, 210.0, 220.0):
        row = {"strike": strike}
        for kind, letter in (("call", "C"), ("put", "P")):
            cid = f"OPT:AAPL:2026-12-18:{letter}:{int(strike)}"
            row[kind] = cell(*QUOTES[cid]) if cid in QUOTES else cell("9.00", "9.10")
        out.append(row)
    return out


@pytest.fixture
def swo(ready, monkeypatch):
    """The desk of the execution fixture as a SWO desk: its sheets, its quotes and the chain of the panel."""
    with ready.db.write() as u:
        u.session.update("desk_profile", {"desk_id": ready.desk_id}, {"line": "SWO"})
    for cid in (*QUOTES, STOCK):
        sheet_store.save(sheet(cid), db=ready.db)
    for cid, (bid, ask) in QUOTES.items():
        ready.board.feed(cid, bid=bid, ask=ask, last=ask)
    ready.board.feed(STOCK, bid="199.90", ask="200.00", last="200.00")
    chain = {"ok": True, "spot": 200.0, "rows": chain_rows(), "multiplier": 100.0, "years": 28 / 365, "rate": 0.04, "source": "Feed", "price_feed": "live",
             "greeks": "provider", "as_of": "2026-11-20T15:00:00Z", "market_open": True, "delayed": False, "notes": []}
    monkeypatch.setattr(option_detail, "_load_chain", lambda desk_id, symbol, day: chain)
    monkeypatch.setattr(option_detail, "_today", lambda: TODAY)
    monkeypatch.setattr(option_detail, "_earnings_block", lambda d, s, today: {"ok": True, "source": "Calendar", "fetched_at": "t", "next": None})
    monkeypatch.setattr(option_detail, "_ex_dividend_block", lambda d, s: {"ok": True, "source": "Quotes", "fetched_at": "t", "next": None, "last": None, "amount": None, "frequency": None})
    option_actions._SELECTION.clear()
    return ready


def proposals(r):
    with r.db.read() as u:
        return u.session.query("SELECT id, status, code, strategy_key, account_id FROM exec_proposals WHERE desk_id = :d ORDER BY id", {"d": r.desk_id})


def propose(r, **kw):
    args = {**BULL, **kw}
    return option_actions.propose(r.desk_id, args["symbol"], args["expiration"], args.get("legs"), args.get("structure"), args.get("strikes"), args.get("quantity"),
                                  args.get("strategy_key"))


def test_a_bull_call_spread_goes_through_the_door_as_a_proposal_of_the_desk_and_waits_for_the_owner(swo):
    levels.set_level(swo.desk_id, 2, who=swo.owner, db=swo.db)
    out = propose(swo)
    assert out["status"] == "awaiting_owner" and out["structure"] == "debit_call_spread" and out["strategy_key"] == "principal"
    assert out["net_premium"] == "3.30" and out["limit_price"] == "3.30" and out["max_loss_usd"] == "330.00"
    rows = proposals(swo)
    assert [(r["status"], r["strategy_key"], r["account_id"]) for r in rows] == [("awaiting_owner", "principal", swo.account_id)]
    with swo.db.read() as u:
        stored = json.loads(u.session.one("SELECT proposal_json FROM exec_proposals WHERE id = :i", {"i": rows[0]["id"]})["proposal_json"])
    assert [(leg["contract_id"], leg["side"], leg["effect"]) for leg in stored["legs"]] == [(C200, "buy", "open"), (C210, "sell", "open")]
    assert stored["origin"] == "desk" and stored["order_type"] == "limit" and stored["limit_price"] == "3.3"


def test_at_level_1_the_desk_only_analyzes_and_nothing_waits_for_the_owner(swo):
    out = propose(swo)
    assert out["status"] == "analysis_only" and out["code"] == "level_1"


def test_a_proposal_never_sends_by_itself_even_when_the_owner_pressed_the_button(swo):
    levels.set_level(swo.desk_id, 2, who=swo.owner, db=swo.db)
    out = propose(swo)
    assert out["status"] == "awaiting_owner" and out["status"] not in ("sent", "filled")
    assert proposals(swo)[0]["status"] == "awaiting_owner"


@pytest.mark.parametrize("structure,strikes", [("iron_condor", [170.0, 180.0, 210.0, 220.0]), ("iron_butterfly", [190.0, 200.0, 210.0]), ("short_put", [190.0]),
                                                ("credit_call_spread", [200.0, 210.0])])
def test_selling_premium_is_refused_with_its_reason_before_the_door_while_the_switch_is_closed(swo, structure, strikes):
    levels.set_level(swo.desk_id, 2, who=swo.owner, db=swo.db)
    out = propose(swo, structure=structure, strikes=strikes)
    assert out["status"] == "refused" and out["code"] == "premium_selling_closed" and out["locked"] == "premium_selling"
    assert out["why"] == premium_switch.owner_text("AAPL")                     # the same sentence the Strategies cards and the filter give
    assert proposals(swo) == []                                                      # nothing was recorded


def test_the_protective_put_buys_the_shares_and_the_put_in_one_order(swo):
    levels.set_level(swo.desk_id, 2, who=swo.owner, db=swo.db)
    out = propose(swo, structure="protective_put", strikes=[190.0])
    assert out["status"] == "awaiting_owner", out
    assert out["structure"] == "protective_put" and out["net_premium"] == "2.45"
    with swo.db.read() as u:
        stored = json.loads(u.session.one("SELECT proposal_json FROM exec_proposals WHERE id = :i", {"i": out["proposal_id"]})["proposal_json"])
    assert {(leg["contract_id"], leg["side"], leg["qty"]) for leg in stored["legs"]} == {(STOCK, "buy", "100"), (P190, "buy", "1")}


def test_a_rule_of_the_owner_refuses_the_order_and_the_answer_carries_its_why(swo):
    levels.set_level(swo.desk_id, 2, who=swo.owner, db=swo.db)
    desk_rules.set_control(swo.desk_id, "size_premium", 50, service.owner_who(), mode="restriction", db=swo.db)           # at most 50 USD of premium per order (a new control says its mode, SPEC-026)
    out = propose(swo)
    assert out["status"] == "refused" and out["why"] and out["code"] != "awaiting_owner"
    assert [r["status"] for r in proposals(swo)] == ["refused"]


def test_a_strategy_without_account_asks_which_one_and_records_nothing_until_it_has_one(swo):
    levels.set_level(swo.desk_id, 2, who=swo.owner, db=swo.db)
    out = propose(swo, strategy_key="mi-call-spread")
    assert out["status"] == "needs_account" and out["code"] == "strategy_without_account" and out["strategy_key"] == "mi-call-spread"
    assert {a["name"] for a in out["accounts"]} >= {"Sim A"} and proposals(swo) == []
    desk_accounts.bind_strategy(swo.desk_id, "mi-call-spread", swo.account_id, db=swo.db)
    again = propose(swo, strategy_key="mi-call-spread")
    assert again["status"] == "awaiting_owner"
    assert [(r["strategy_key"], r["account_id"]) for r in proposals(swo)] == [("mi-call-spread", swo.account_id)]


def test_a_desk_without_account_says_so_and_records_nothing(desks_db, swo):
    from desks_support import make_desk

    desk = make_desk(desks_db, name="Empty desk")
    with desks_db.write() as u:
        u.session.insert_row("desk_profile", {"desk_id": desk["id"], "line": "SWO"})
    out = option_actions.propose(desk["id"], "AAPL", "2026-12-18", None, "debit_call_spread", [200.0, 210.0], 1)
    assert out["status"] == "needs_account" and out["code"] == "no_account" and out["accounts"] == []
    with desks_db.read() as u:
        assert u.session.query("SELECT id FROM exec_proposals WHERE desk_id = :d", {"d": desk["id"]}) == []


def test_a_desk_that_is_not_an_options_desk_does_not_propose_options(swo):
    with swo.db.write() as u:
        u.session.update("desk_profile", {"desk_id": swo.desk_id}, {"line": "DT"})
    assert propose(swo)["code"] == "not_options_desk"


def test_a_chain_that_cannot_answer_is_no_data_never_a_proposal(swo, monkeypatch):
    monkeypatch.setattr(option_detail, "_load_chain", lambda *a: {"ok": False, "source": "Feed", "code": "empty", "reason": "sin filas"})
    out = propose(swo)
    assert out["status"] == "no_data" and "Feed" in out["why"] and "sin filas" in out["why"] and proposals(swo) == []


@pytest.mark.parametrize("bad", [{"quantity": 1.5}, {"quantity": 0}, {"strikes": [210.0, 200.0]}, {"structure": "banana"}, {"symbol": "12"}, {"expiration": "tomorrow"}])
def test_a_request_that_is_not_valid_is_422_and_records_nothing(swo, bad):
    with pytest.raises(DeskError) as exc:
        propose(swo, **bad)
    assert exc.value.status == 422 and proposals(swo) == []


def test_legs_that_are_none_of_the_structures_are_refused_not_guessed(swo):
    out = propose(swo, structure=None, strikes=None, legs=[{"position": "long", "kind": "call", "strike": 200.0}, {"position": "long", "kind": "put", "strike": 190.0}])
    assert out["status"] == "refused" and out["code"] == "bad_proposal" and proposals(swo) == []


def _stored(r, proposal_id):
    with r.db.read() as u:
        return json.loads(u.session.one("SELECT proposal_json FROM exec_proposals WHERE id = :i", {"i": proposal_id})["proposal_json"])


SPREAD_LEGS = [{"position": "long", "kind": "call", "strike": 200.0}, {"position": "short", "kind": "call", "strike": 210.0}]


def test_legs_in_a_ratio_are_refused_with_their_reason_and_the_detail_says_why_not_1x1(swo):
    levels.set_level(swo.desk_id, 2, who=swo.owner, db=swo.db)
    ratio = [{**SPREAD_LEGS[0], "quantity": 1}, {**SPREAD_LEGS[1], "quantity": 2}]
    seen = option_detail.option_detail(swo.desk_id, "AAPL", "2026-12-18", ratio, None, None, 1)
    assert seen["metrics"]["unbounded_loss"] is True                       # what the detail shows is not a 1x1 spread
    out = propose(swo, structure=None, strikes=None, legs=ratio)
    assert out["status"] == "refused" and out["code"] == "bad_proposal" and out["why"] == t("desks.option_detail.propose_ratio")
    assert proposals(swo) == []


def test_legs_by_three_make_an_order_of_three_contracts_with_the_loss_the_detail_shows(swo):
    levels.set_level(swo.desk_id, 2, who=swo.owner, db=swo.db)
    legs = [{**x, "quantity": 3} for x in SPREAD_LEGS]
    seen = option_detail.option_detail(swo.desk_id, "AAPL", "2026-12-18", legs, None, None, 1)
    out = propose(swo, structure=None, strikes=None, legs=legs)
    assert out["status"] == "awaiting_owner" and out["structure"] == "debit_call_spread" and out["quantity"] == 3
    assert out["max_loss_usd"] == "990.00" and float(out["max_loss_usd"]) == pytest.approx(seen["metrics"]["max_loss_usd"]) == pytest.approx(990.0)
    stored = _stored(swo, out["proposal_id"])
    assert [(leg["contract_id"], leg["side"], leg["qty"]) for leg in stored["legs"]] == [(C200, "buy", "3"), (C210, "sell", "3")]
    assert D(stored["risk_usd"]) == D("990")                                 # the loss recorded is the one the detail shows


def test_a_fraction_of_a_contract_is_refused_not_cut_to_a_whole_one(swo):
    out = propose(swo, structure=None, strikes=None, legs=[{**x, "quantity": 1.5} for x in SPREAD_LEGS])
    assert out["status"] == "refused" and out["code"] == "bad_proposal" and proposals(swo) == []


def test_an_iron_butterfly_and_an_iron_condor_need_their_puts_and_calls_apart():
    from hokma.desks.swo.schema import derive_structure
    from hokma.exec.proposal import Leg

    def legs(p0, p1, c0, c1):
        return (Leg(f"OPT:AAPL:2026-12-18:P:{p0}", "buy", "open", D(1)), Leg(f"OPT:AAPL:2026-12-18:P:{p1}", "sell", "open", D(1)),
                Leg(f"OPT:AAPL:2026-12-18:C:{c0}", "sell", "open", D(1)), Leg(f"OPT:AAPL:2026-12-18:C:{c1}", "buy", "open", D(1)))
    assert derive_structure(legs(190, 200, 200, 210)) == "iron_butterfly"
    assert derive_structure(legs(180, 190, 210, 220)) == "iron_condor"
    assert derive_structure(legs(200, 200, 200, 210)) is None
    assert derive_structure(legs(190, 200, 210, 210)) is None


# ---- saved strategies --------------------------------------------------------------------------------------------------------------------------

def test_saving_keeps_the_legs_in_the_layout_without_a_table_and_the_panels_keep_their_order(swo):
    desk = desk_service.add_panel(swo.desk_id, "option_detail")
    before = desk["panels"]
    saved = option_actions.save(swo.desk_id, "Mi Call Spread 2", "AAPL", "2026-12-18", None, "debit_call_spread", [200.0, 210.0], 2)
    assert saved["key"] == "mi-call-spread-2" and saved["quantity"] == 2
    listed = option_actions.saved_list(swo.desk_id)["strategies"]
    assert [(s["key"], s["structure"], s["strikes"]) for s in listed] == [("mi-call-spread-2", "debit_call_spread", [200.0, 210.0])]
    assert desk_service.get_desk(swo.desk_id)["panels"] == before                    # the panels were not touched
    desk_service.add_panel(swo.desk_id, "strategies")
    assert [s["key"] for s in option_actions.saved_list(swo.desk_id)["strategies"]] == ["mi-call-spread-2"]    # and a change of panels keeps the saved ones
    option_actions.save(swo.desk_id, "Mi Call Spread 2", "AAPL", "2026-12-18", None, "long_call", [200.0], 1)  # the same name replaces
    assert len(option_actions.saved_list(swo.desk_id)["strategies"]) == 1
    out = option_actions.delete_saved(swo.desk_id, "mi-call-spread-2")
    assert out["strategies"] == []


@pytest.mark.parametrize("name", ["", "   ", "principal", "!!!", "x" * 61, None, 5])
def test_a_name_that_is_not_a_name_is_refused(swo, name):
    with pytest.raises(DeskError) as exc:
        option_actions.save(swo.desk_id, name, "AAPL", "2026-12-18", None, "long_call", [200.0], 1)
    assert exc.value.code == "bad_saved_strategy"


def test_a_desk_holds_at_most_thirty_saved_strategies_and_deleting_an_unknown_one_is_404(swo):
    for i in range(option_actions.MAX_SAVED):
        option_actions.save(swo.desk_id, f"s{i}", "AAPL", "2026-12-18", None, "long_call", [200.0], 1)
    with pytest.raises(DeskError) as full:
        option_actions.save(swo.desk_id, "one more", "AAPL", "2026-12-18", None, "long_call", [200.0], 1)
    assert full.value.code == "saved_strategies_full" and full.value.status == 409
    with pytest.raises(DeskError) as gone:
        option_actions.delete_saved(swo.desk_id, "nope")
    assert gone.value.status == 404


def test_a_saved_strategy_is_proposed_by_its_key_and_its_key_is_the_one_of_the_account(swo):
    levels.set_level(swo.desk_id, 2, who=swo.owner, db=swo.db)
    option_actions.save(swo.desk_id, "mi spread", "AAPL", "2026-12-18", None, "debit_call_spread", [200.0, 210.0], 1)
    out = option_actions.propose_saved(swo.desk_id, "mi-spread")
    assert out["status"] == "needs_account" and out["strategy_key"] == "mi-spread"
    with pytest.raises(DeskError) as missing:
        option_actions.propose_saved(swo.desk_id, "ghost")
    assert missing.value.status == 404


# ---- the selection and the routes -----------------------------------------------------------------------------------------------------------------

@pytest.fixture
def client(desks_db):
    app = FastAPI()
    app.include_router(desks_router)
    with TestClient(app) as c:
        c.headers.update({"X-Hokma-Token": SESSION_TOKEN})
        yield c


def test_the_selection_is_kept_for_the_panel_and_a_desk_without_one_answers_null(swo, client):
    desk = swo.desk_id
    assert client.get(f"/api/desks/{desk}/option-detail/selection").json() == {"selection": None}
    body = {**BULL, "range_pct": 5, "iv_mult": 1.25, "strategy_key": None, "name": None}
    assert client.put(f"/api/desks/{desk}/option-detail/selection", json=body).status_code == 200
    got = client.get(f"/api/desks/{desk}/option-detail/selection").json()["selection"]
    assert got["structure"] == "debit_call_spread" and got["strikes"] == [200.0, 210.0] and got["range_pct"] == 5 and got["iv_mult"] == 1.25
    assert client.put(f"/api/desks/{desk}/option-detail/selection", json={**BULL, "quantity": -1}).status_code == 422
    assert client.get("/api/desks/9999/option-detail/selection").status_code == 404
    assert TestClient(client.app).get(f"/api/desks/{desk}/option-detail/selection").status_code == 401


def test_the_routes_save_list_forget_and_propose(swo, client):
    desk = swo.desk_id
    levels.set_level(desk, 2, who=swo.owner, db=swo.db)
    made = client.post(f"/api/desks/{desk}/option-detail/saved", json={"name": "Mi spread", **BULL})
    assert made.status_code == 201 and made.json()["key"] == "mi-spread"
    assert [s["key"] for s in client.get(f"/api/desks/{desk}/option-detail/saved").json()["strategies"]] == ["mi-spread"]
    proposed = client.post(f"/api/desks/{desk}/option-detail/propose", json={**BULL, "strategy_key": None})
    assert proposed.status_code == 200 and proposed.json()["status"] == "awaiting_owner"
    assert client.post(f"/api/desks/{desk}/option-detail/propose", json={**BULL, "quantity": 0}).status_code == 422
    assert client.delete(f"/api/desks/{desk}/option-detail/saved/mi-spread").json() == {"strategies": []}
    assert client.delete(f"/api/desks/{desk}/option-detail/saved/mi-spread").status_code == 404


# ---- the chat tools -------------------------------------------------------------------------------------------------------------------------------

def test_the_chief_opens_the_detail_by_chat_the_panel_is_added_and_the_figures_come_back(swo, client):
    desk = swo.desk_id
    text, err = hokma_tools.call("desk_option_detail", {"desk_id": desk, "symbol": "aapl", "expiration": "2026-12-18", "structure": "debit_call_spread", "strikes": [200, 210]},
                                 hokma_tools.Caller({}))
    assert not err
    out = json.loads(text)
    assert "option_detail" in out["panels"] and out["ok"] is True and out["symbol"] == "AAPL" and out["breakevens_at_expiration"]
    assert "table" not in out and out["max_loss_usd"] > 0
    assert client.get(f"/api/desks/{desk}/option-detail/selection").json()["selection"]["strikes"] == [200.0, 210.0]
    text, err = hokma_tools.call("desk_option_detail", {"desk_id": desk, "symbol": "AAPL", "expiration": "tomorrow", "structure": "long_call", "strikes": [200]}, hokma_tools.Caller({}))
    assert err


def test_the_chief_proposes_by_chat_through_the_same_door_and_never_sends(swo):
    levels.set_level(swo.desk_id, 2, who=swo.owner, db=swo.db)
    text, err = hokma_tools.call("desk_strategy_propose", {"desk_id": swo.desk_id, **BULL}, hokma_tools.Caller({}))
    assert not err
    out = json.loads(text)
    assert out["status"] == "awaiting_owner" and out["strategy_key"] == "principal"
    text, err = hokma_tools.call("desk_strategy_propose", {"desk_id": swo.desk_id}, hokma_tools.Caller({}))
    assert err and "strategy_key" in text
    text, err = hokma_tools.call("desk_strategy_propose", {"desk_id": swo.desk_id, "strategy_key": "ghost"}, hokma_tools.Caller({}))
    assert err


def test_a_double_post_of_the_same_proposal_waits_once_and_answers_the_one_already_waiting(swo):
    levels.set_level(swo.desk_id, 2, who=swo.owner, db=swo.db)
    first = propose(swo)
    second = propose(swo)
    assert first["status"] == second["status"] == "awaiting_owner"
    assert first["proposal_id"] == second["proposal_id"] and first["fingerprint"] == second["fingerprint"]
    assert [r["status"] for r in proposals(swo)] == ["awaiting_owner"]
    other = propose(swo, quantity=2)                                                              # another order is another proposal
    assert other["proposal_id"] != first["proposal_id"] and len(proposals(swo)) == 2


def test_an_expired_twin_is_not_the_answer_a_new_proposal_is_recorded_and_the_old_one_expires(swo, monkeypatch):
    levels.set_level(swo.desk_id, 2, who=swo.owner, db=swo.db)
    first = propose(swo)
    with monkeypatch.context() as m:                                                              # the wait of the first one is over, nobody swept it yet
        m.setattr("hokma.broker.service.PENDING_MINUTES", 0)
        second = propose(swo)
    assert second["status"] == "awaiting_owner" and second["proposal_id"] != first["proposal_id"]
    by_id = {r["id"]: r["status"] for r in proposals(swo)}
    assert by_id[first["proposal_id"]] == "expired" and by_id[second["proposal_id"]] == "awaiting_owner"


def test_a_twin_of_another_level_is_not_reused(swo):
    levels.set_level(swo.desk_id, 2, who=swo.owner, db=swo.db)
    first = propose(swo)
    with swo.db.write() as u:                                                                     # the desk was at another level when the twin was made
        u.session.update("exec_proposals", {"id": first["proposal_id"]}, {"level": 3})
    second = propose(swo)
    assert second["proposal_id"] != first["proposal_id"]
