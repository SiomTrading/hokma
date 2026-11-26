"""The rules of a desk (SPEC-025, delivery 1, task 3334): controls the guards apply, instructions the chief reads, proposals the owner approves."""
import dataclasses
import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from desks_support import db, desks_db, env, make_desk  # noqa: F401  (fixtures)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.backoffice.snapshot import AccountSnapshot
from hokma.broker import desk_rules, service
from hokma.broker.api import router as broker_router
from hokma.broker.service import BrokerError, Who
from hokma.contracts import identity
from hokma.exec.guards import GuardContext, pipeline, registry
from hokma.exec.limits import apply_limits
from hokma.exec.proposal import Leg, OrderProposal
from hokma.kit import hokma_tools
from hokma.rpc.ws import SESSION_TOKEN

OWNER = service.owner_who()
AGENT = Who(agent="desk-chief")
NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
NQ, SPY = "FUT:NQ:2026-12", "OPT:SPY:2026-12-18:C:600"


def prop(contract=NQ, side="buy", qty="1", effect="open", line="DT", **kw):
    return OrderProposal(1, line, "desk", (Leg(contract, side, effect, Decimal(qty)),), kw.pop("order_type", "market"), created_by="x", **kw)


def book(**kw):
    base = dict(account_id=1, as_of="2026-11-20 15:00:00", cash=Decimal(1_000_000), positions_value=Decimal(0), equity=Decimal(1_000_000),
                day_pnl=Decimal(0), peak_equity=Decimal(1_000_000), total_profit=Decimal(0), open_contracts=Decimal(0))
    base.update(kw)
    return AccountSnapshot(**base)


def ctx(p, limits, **kw):
    sheets = {c: identity.resolve(c) for c in (NQ, SPY)}
    return GuardContext(proposal=p, contracts=sheets, positions=kw.pop("positions", ()), book=kw.pop("book", book()), limits=limits, level=2, windows=(), now=NOW,
                        desk_id=1, capital=Decimal(1_000_000), marks={NQ: Decimal(21000), SPY: Decimal("5.5")})


def config(db, desk_id):
    with db.read() as u:
        return u.broker.get_limits(desk_id)


def set_line(db, desk_id, line):
    with db.write() as u:
        u.session.insert_row("desk_profile", {"desk_id": desk_id, "line": line, "variant": None, "benchmark": None, "updated_at": "2026-11-24 00:00:00"})


def legacy(db, desk_id, cfg):
    """A desk created before this delivery: its configuration is whatever it had, with no list of guards."""
    with db.write() as u:
        u.broker.set_limits(desk_id, cfg)


def names(guards):
    return [g.name for g in guards]


# ---- R1 ----------------------------------------------------------------------------------------------------------------

def test_a_new_desk_has_no_rules_and_only_the_fixed_guards_run(desks_db):
    desk = make_desk(desks_db)
    cfg = config(desks_db, desk["id"])
    assert cfg == {"guards": []}
    assert names(registry.build(cfg)) == ["contract", "monitor", "live_loss", "premium_selling", "stop", "broker_rules"]
    shown = desk_rules.view(desk["id"], desks_db)
    assert shown["active"] == [] and shown["legacy"] is False and shown["fixed"] == list(registry.ALWAYS)


# ---- R2 ----------------------------------------------------------------------------------------------------------------

def test_a_daily_loss_added_by_hand_blocks_the_next_opening_and_never_a_closing(desks_db):
    desk = make_desk(desks_db)
    desk_rules.set_control(desk["id"], "daily_loss", 400, OWNER, mode="restriction", on_trip="block_entries", db=desks_db)
    cfg = config(desks_db, desk["id"])
    assert cfg["funding"] == {"daily_loss_usd": 400} and cfg["guards"] == ["account"] and cfg["rule_sources"] == {"daily_loss": "hand"}
    account = [g for g in registry.build(cfg) if g.name == "account"]
    assert len(account) == 1
    down = book(day_pnl=Decimal(-400))
    opening = pipeline.run(account, ctx(prop(), cfg, book=down))
    assert opening.verdict == "BLOCK" and opening.blocked_by.code == "daily_loss"
    held = (SimpleNamespace(contract_id=NQ, qty=1, symbol=NQ, market_value=None, multiplier=None, day_pl=None),)       # a long position to close
    closing = pipeline.run(account, ctx(prop(side="sell", effect="close"), cfg, book=down, positions=held))
    assert closing.verdict == "PASS"
    assert desk_rules.view(desk["id"], desks_db)["active"][0]["value"] == 400


def test_removing_a_control_takes_its_guard_out_only_when_no_other_control_needs_it(desks_db):
    desk = make_desk(desks_db)
    desk_rules.set_control(desk["id"], "daily_loss", 400, OWNER, mode="restriction", on_trip="block_entries", db=desks_db)
    desk_rules.set_control(desk["id"], "drawdown", 900, OWNER, mode="restriction", on_trip="block_entries", db=desks_db)
    desk_rules.remove_rule(desk["id"], "daily_loss", OWNER, desks_db)
    cfg = config(desks_db, desk["id"])
    assert cfg["funding"] == {"trailing_drawdown_usd": 900} and cfg["guards"] == ["account"]
    desk_rules.remove_rule(desk["id"], "drawdown", OWNER, desks_db)
    assert config(desks_db, desk["id"]) == {"guards": []}


def test_the_pause_is_shown_in_minutes_and_stored_in_seconds_and_an_event_keeps_its_word(desks_db):
    desk = make_desk(desks_db)
    desk_rules.set_control(desk["id"], "cooldown", 10, OWNER, mode="restriction", db=desks_db)
    desk_rules.set_control(desk["id"], "events", 30, OWNER, match="FOMC", mode="restriction", db=desks_db)
    cfg = config(desks_db, desk["id"])
    assert cfg["cooldown_seconds"] == 600 and cfg["event_windows"] == [{"match": "FOMC", "minutes_before": 30, "minutes_after": 30}]
    assert sorted(cfg["guards"]) == ["cooldown", "event_window"]
    rows = {r["id"]: r for r in desk_rules.view(desk["id"], desks_db)["active"]}
    assert rows["cooldown"]["value"] == 10 and rows["events:fomc"]["value"] == 30
    desk_rules.remove_rule(desk["id"], "events:fomc", OWNER, desks_db)
    assert "event_windows" not in config(desks_db, desk["id"])


# ---- R3 ----------------------------------------------------------------------------------------------------------------

def test_a_sentence_without_a_control_is_proposed_as_an_instruction_and_blocks_nothing(desks_db):
    desk = make_desk(desks_db)
    item = desk_rules.propose(desk["id"], "do not trade the week of earnings", db=desks_db)
    assert item["kind"] == "instruction"
    cfg = config(desks_db, desk["id"])
    assert cfg["guards"] == [] and "instructions" not in cfg and [p["id"] for p in cfg["proposed"]] == [item["id"]]
    assert desk_rules.view(desk["id"], desks_db)["active"] == []              # a proposal is not a rule yet
    desk_rules.approve(desk["id"], item["id"], OWNER, desks_db)
    cfg = config(desks_db, desk["id"])
    assert "proposed" not in cfg and cfg["guards"] == [] and [i["text"] for i in cfg["instructions"]] == ["do not trade the week of earnings"]
    assert cfg["instructions"][0]["source"] == "chat"
    active = desk_rules.view(desk["id"], desks_db)["active"]
    assert active[0]["kind"] == "instruction" and active[0]["source"] == "chat"
    assert names(registry.build(cfg)) == list(registry.ALWAYS)                  # no guard was added: nothing blocks because of it
    from hokma.desks.agents import trader

    assert "do not trade the week of earnings" in trader._limits_text(cfg)
    shown = json.loads(hokma_tools.desk_show(None, hokma_tools.Caller({}), {"desk_id": desk["id"]}))
    assert shown["limits"]["instructions"][0]["text"] == "do not trade the week of earnings"      # what the chief reads in desk_show


# ---- R4 ----------------------------------------------------------------------------------------------------------------

def test_a_premium_by_chat_becomes_a_proposal_of_a_control_and_an_order_respects_it_once_approved(desks_db):
    desk = make_desk(desks_db)
    set_line(desks_db, desk["id"], "SWO")
    item = desk_rules.propose(desk["id"], "at most 500 dollars of premium", mode="restriction", db=desks_db)
    assert (item["kind"], item["control_id"], item["value"]) == ("control", "size_premium", 500)
    assert "max_premium_usd" not in config(desks_db, desk["id"])               # nothing applied until the owner approves
    desk_rules.approve(desk["id"], item["id"], OWNER, desks_db)
    cfg = config(desks_db, desk["id"])
    assert cfg["max_premium_usd"] == 500 and cfg["guards"] == ["limits"] and cfg["rule_sources"] == {"size_premium": "chat"}
    assert "limits" in names(registry.build(cfg))
    big = prop(SPY, qty="2", line="SWO", order_type="limit", limit_price=Decimal("3"))          # 2 x 3 x 100 = 600 of premium
    out = apply_limits(big, limits=cfg, contracts=ctx(big, cfg).contracts, positions=(), equity=Decimal(1_000_000), marks={SPY: Decimal(3)})
    assert out.refused is None and out.proposal.legs[0].qty == 1                  # cut to what fits under 500


def test_the_chief_can_name_the_control_and_a_wrong_value_is_refused_at_the_proposal(desks_db):
    desk = make_desk(desks_db)
    item = desk_rules.propose(desk["id"], "10 minutes between orders", control_id="cooldown", value=10, mode="restriction", db=desks_db)
    assert item["control_id"] == "cooldown"
    with pytest.raises(BrokerError) as bad:
        desk_rules.propose(desk["id"], "daily loss", control_id="daily_loss", value=-5, db=desks_db)
    assert bad.value.code == "bad_limits"
    with pytest.raises(BrokerError):
        desk_rules.propose(desk["id"], "anything", control_id="whitelist", value=1, db=desks_db)


def test_a_value_that_does_not_agree_with_the_sentence_is_refused_and_the_card_shows_the_saved_value(desks_db):
    desk = make_desk(desks_db)
    d = desk["id"]
    for text, cid, value, match in (("daily loss of 400 dollars", "daily_loss", 4000, None),           # the sentence says 400, the agent sends 4000
                                    ("daily loss of 400 dollars", "drawdown", 400, None),              # another control
                                    ("at most 500 dollars of daily loss", "daily_loss", 500, "FOMC"),  # a word of event that is not in the sentence
                                    ("something with no number", "daily_loss", 400, None)):
        with pytest.raises(BrokerError) as err:
            desk_rules.propose(d, text, control_id=cid, value=value, match=match, db=desks_db)
        assert err.value.code == "bad_limits"
    assert config(desks_db, d) == {"guards": []}
    item = desk_rules.propose(d, "30 minutes around FOMC", control_id="events", value=30, match="fomc", mode="restriction", db=desks_db)
    card = desk_rules.view(d, desks_db)["proposed"][0]
    assert (card["control_id"], card["value"], card["unit"], card["match"], card["loosens"], card["current"]) == ("events", 30, "minutes", "FOMC", False, None)
    assert card["id"] == item["id"]


def test_approving_a_proposal_that_loosens_a_control_asks_for_the_confirmation_of_the_owner(desks_db):
    desk = make_desk(desks_db)
    d = desk["id"]
    desk_rules.set_control(d, "daily_loss", 400, OWNER, mode="restriction", on_trip="block_entries", db=desks_db)
    soft = desk_rules.propose(d, "daily loss of 900 dollars", mode="restriction", on_trip="block_entries", db=desks_db)
    card = desk_rules.view(d, desks_db)["proposed"][0]
    assert (card["value"], card["unit"], card["current"], card["loosens"]) == (900, "usd", 400, True)
    with pytest.raises(BrokerError) as err:
        desk_rules.approve(d, soft["id"], OWNER, desks_db)
    assert err.value.code == "loosens_limit" and err.value.status == 409
    assert config(desks_db, d)["funding"] == {"daily_loss_usd": 400} and len(config(desks_db, d)["proposed"]) == 1      # nothing changed
    desk_rules.approve(d, soft["id"], OWNER, desks_db, confirm_loosen=True)
    assert config(desks_db, d)["funding"] == {"daily_loss_usd": 900} and "proposed" not in config(desks_db, d)
    tight = desk_rules.propose(d, "daily loss of 300 dollars", mode="restriction", on_trip="block_entries", db=desks_db)                  # stricter, or a new control: no confirmation
    assert desk_rules.view(d, desks_db)["proposed"][0]["loosens"] is False
    desk_rules.approve(d, tight["id"], OWNER, desks_db)
    assert config(desks_db, d)["funding"] == {"daily_loss_usd": 300}
    new = desk_rules.propose(d, "10 minutes between orders", mode="restriction", db=desks_db)
    desk_rules.approve(d, new["id"], OWNER, desks_db)
    assert config(desks_db, d)["cooldown_seconds"] == 600


def test_the_route_answers_409_until_the_owner_confirms_and_the_agent_still_cannot_approve(client, desks_db):
    d = make_desk(desks_db)["id"]
    desk_rules.set_control(d, "daily_loss", 400, OWNER, mode="restriction", on_trip="block_entries", db=desks_db)
    pid = desk_rules.propose(d, "daily loss of 900 dollars", mode="restriction", on_trip="block_entries", db=desks_db)["id"]
    url = f"/api/desks/{d}/rules-proposals/{pid}/approve"
    assert client.post(url, json={}).status_code == 409 and client.post(url).status_code == 409
    assert client.post(url, json={"confirm_loosen": False}).status_code == 409
    with pytest.raises(BrokerError) as err:
        desk_rules.approve(d, pid, AGENT, desks_db, confirm_loosen=True)
    assert err.value.status == 403 and config(desks_db, d)["funding"] == {"daily_loss_usd": 400}
    assert client.post(url, json={"confirm_loosen": True}).status_code == 200
    assert config(desks_db, d)["funding"] == {"daily_loss_usd": 900}


def test_the_tool_of_the_chief_only_proposes(desks_db):
    desk = make_desk(desks_db)
    assert "desk_rule_propose" in hokma_tools.TOOLS
    out = json.loads(hokma_tools.desk_rule_propose(None, hokma_tools.Caller({}), {"desk_id": desk["id"], "text": "close before earnings"}))
    assert out["proposed"]["kind"] == "instruction" and "approve" in out["tell_the_owner"]
    assert config(desks_db, desk["id"]).get("instructions") is None


# ---- R5 ----------------------------------------------------------------------------------------------------------------

def test_an_agent_can_neither_approve_nor_remove_nor_add_a_rule(desks_db):
    desk = make_desk(desks_db)
    d = desk["id"]
    item = desk_rules.propose(d, "an instruction", db=desks_db)
    desk_rules.set_control(d, "daily_loss", 400, OWNER, mode="restriction", on_trip="block_entries", db=desks_db)
    calls = (lambda: desk_rules.approve(d, item["id"], AGENT, desks_db), lambda: desk_rules.reject(d, item["id"], AGENT, desks_db),
             lambda: desk_rules.remove_rule(d, "daily_loss", AGENT, desks_db), lambda: desk_rules.set_control(d, "drawdown", 1, AGENT, db=desks_db),
             lambda: desk_rules.add_instruction(d, "x", AGENT, db=desks_db), lambda: desk_rules.dismiss(d, "s_i1", AGENT, desks_db),
             lambda: desk_rules.set_control(d, "drawdown", 1, None, db=desks_db))
    for call in calls:
        with pytest.raises(BrokerError) as err:
            call()
        assert err.value.code == "owner_only" and err.value.status == 403
    assert config(desks_db, d)["funding"] == {"daily_loss_usd": 400} and len(config(desks_db, d)["proposed"]) == 1


def test_the_tools_of_the_agents_cannot_write_the_notes_of_the_rules_and_tightening_keeps_the_guard_listed(desks_db):
    desk = make_desk(desks_db)
    with pytest.raises(BrokerError) as err:
        service.set_limits(desk["id"], {"instructions": [{"id": "i1", "text": "x"}]}, db=desks_db, tighten_only=True)
    assert err.value.code == "owner_only"
    service.set_limits(desk["id"], {"max_premium_usd": 300}, db=desks_db, tighten_only=True)       # the committee fills a limit
    assert config(desks_db, desk["id"])["guards"] == ["limits"]                                      # and the code really applies it


# ---- R6 ----------------------------------------------------------------------------------------------------------------

def test_a_desk_that_already_existed_keeps_running_every_guard_and_the_tab_lists_its_real_controls(desks_db):
    desk = make_desk(desks_db)
    legacy(desks_db, desk["id"], {"funding": {"daily_loss_usd": 900, "max_contracts": 3}, "max_premium_usd": 100, "max_daily_loss_pct": 2})
    shown = desk_rules.view(desk["id"], desks_db)
    assert shown["legacy"] is True and shown["assets_on"] is True
    rows = {r["id"]: r for r in shown["active"]}
    assert set(rows) == {"daily_loss", "size_contracts", "size_premium", "daily_loss_pct"} and all(r["on"] for r in rows.values())
    assert rows["daily_loss"]["source"] == "existing"
    assert names(registry.build(config(desks_db, desk["id"]))) == list(registry.known_types())
    desk_rules.set_control(desk["id"], "drawdown", 500, OWNER, mode="restriction", on_trip="block_entries", db=desks_db)                  # adding a control does not create the list
    assert "guards" not in config(desks_db, desk["id"])


def test_only_these_assets_turns_the_whitelist_on_and_is_edited_in_the_settings_not_here(desks_db):
    desk = make_desk(desks_db)
    service.set_limits(desk["id"], {"assets_only": True}, db=desks_db)
    assert config(desks_db, desk["id"])["guards"] == ["whitelist"]
    shown = desk_rules.view(desk["id"], desks_db)
    row = shown["active"][0]
    assert (row["id"], row["readonly"], row["on"]) == ("assets_only", True, True) and shown["assets_on"] is True
    with pytest.raises(BrokerError) as err:
        desk_rules.remove_rule(desk["id"], "assets_only", OWNER, desks_db)
    assert err.value.code == "bad_limits"
    service.set_limits(desk["id"], {"assets_only": False}, db=desks_db)             # off in the settings: the guard type stays listed, it passes
    assert desk_rules.view(desk["id"], desks_db)["active"] == []


# ---- suggestions, dismissed, the catalog -------------------------------------------------------------------------------

def test_suggestions_are_by_line_none_is_active_and_a_dismissed_one_stays_hidden(desks_db):
    desk = make_desk(desks_db)
    set_line(desks_db, desk["id"], "SWO")
    shown = desk_rules.view(desk["id"], desks_db)
    ids = [s["id"] for s in shown["suggested"]]
    assert ids == ["s_size_premium", "s_daily_loss", "s_events"] + [f"s_i{n}" for n in range(1, 7)] and shown["active"] == []
    assert {c["id"] for c in shown["catalog"]} == {"size_premium", "position_value", "daily_loss", "drawdown", "cooldown", "events"}
    desk_rules.dismiss(desk["id"], "s_daily_loss", OWNER, desks_db)
    desk_rules.set_control(desk["id"], "size_premium", 500, OWNER, source="suggested", mode="restriction", db=desks_db)
    desk_rules.add_instruction(desk["id"], "Look at the open interest", OWNER, source="suggested", suggested_id="s_i1", db=desks_db)
    after = [s["id"] for s in desk_rules.view(desk["id"], desks_db)["suggested"]]
    assert after == ["s_events"] + [f"s_i{n}" for n in range(2, 7)]


def test_bad_values_and_too_many_instructions_are_refused_and_nothing_is_saved(desks_db):
    desk = make_desk(desks_db)
    for control, value in (("daily_loss", -1), ("daily_loss", "400"), ("cooldown", 1.5), ("nope", 1)):
        with pytest.raises(BrokerError):
            desk_rules.set_control(desk["id"], control, value, OWNER, mode="restriction", db=desks_db)
    with pytest.raises(BrokerError):
        desk_rules.set_control(desk["id"], "events", 30, OWNER, mode="restriction", db=desks_db)                  # an event rule needs its word
    with pytest.raises(BrokerError):
        desk_rules.add_instruction(desk["id"], "x" * 501, OWNER, db=desks_db)
    assert config(desks_db, desk["id"]) == {"guards": []}


# ---- the routes --------------------------------------------------------------------------------------------------------

@pytest.fixture
def client(desks_db):
    app = FastAPI()
    app.include_router(broker_router)
    with TestClient(app) as c:
        c.headers.update({"X-Hokma-Token": SESSION_TOKEN})
        yield c


def test_the_routes_add_edit_approve_and_remove_as_the_owner(client, desks_db):
    d = make_desk(desks_db)["id"]
    assert client.get(f"/api/desks/{d}/rules", headers={"X-Hokma-Token": "no"}).status_code == 401
    res = client.post(f"/api/desks/{d}/rules", json={"kind": "control", "control_id": "daily_loss", "value": 400, "mode": "restriction", "on_trip": "block_entries"})
    assert res.status_code == 201 and res.json()["active"][0]["value"] == 400
    assert client.put(f"/api/desks/{d}/rules/daily_loss", json={"value": 250}).json()["active"][0]["value"] == 250
    res = client.post(f"/api/desks/{d}/rules", json={"kind": "instruction", "text": "Leave before earnings", "source": "hand"})
    iid = res.json()["active"][-1]["id"]
    assert client.put(f"/api/desks/{d}/rules/{iid}", json={"text": "Leave a day before earnings"}).json()["active"][-1]["text"].startswith("Leave a day")
    pid = desk_rules.propose(d, "do not trade on fridays", db=desks_db)["id"]
    assert [p["id"] for p in client.get(f"/api/desks/{d}/rules").json()["proposed"]] == [pid]
    assert client.post(f"/api/desks/{d}/rules-proposals/{pid}/approve").status_code == 200
    assert client.post(f"/api/desks/{d}/rules-proposals/{pid}/approve").status_code == 404
    assert client.delete(f"/api/desks/{d}/rules/{iid}").status_code == 200
    assert client.post(f"/api/desks/{d}/rules", json={"kind": "control", "control_id": "daily_loss", "value": -3, "mode": "restriction", "on_trip": "block_entries"}).status_code == 400
    assert client.get("/api/desks/9999/rules").status_code == 404


# ---- a new desk with assets applies its list ---------------------------------------------------------------------------

def _symbol_ctx(desks_db, desk_id):
    with desks_db.read() as u:
        assets = [a.symbol for a in u.desks.list_assets(desk_id)]
    return config(desks_db, desk_id), assets


def _whitelist_result(cfg, assets, contract):
    p = prop(contract)
    c = ctx(p, cfg)
    c = dataclasses.replace(c, assets=tuple(assets))
    return [g for g in registry.build(cfg) if g.name == "whitelist"], c


def test_a_new_desk_with_assets_keeps_the_list_and_without_assets_does_not_block(desks_db):
    with_list = make_desk(desks_db, name="Con lista", assets=["NQ"])
    cfg, assets = _symbol_ctx(desks_db, with_list["id"])
    assert cfg == {"guards": ["whitelist"], "rule_modes": {"assets_only": {"mode": "notice", "timeout_min": 30}}} and assets == ["NQ"]
    guards, c = _whitelist_result(cfg, assets, SPY)
    assert len(guards) == 1 and guards[0].check(c).verdict == "block"                      # SPY is not in the list
    guards, c = _whitelist_result(cfg, assets, NQ)
    assert guards[0].check(c).verdict == "pass"
    empty = make_desk(desks_db, name="Sin lista")
    cfg, assets = _symbol_ctx(desks_db, empty["id"])
    assert cfg == {"guards": []} and assets == []
    assert [g for g in registry.build(cfg) if g.name == "whitelist"] == []              # nothing to restrict, nothing runs


def test_adding_assets_later_turns_the_restriction_on_and_an_old_desk_does_not_change(desks_db):
    from hokma.desks import service as desks_service

    desk = make_desk(desks_db)
    desks_service.set_assets(desk["id"], ["NQ"], desks_db)
    cfg, assets = _symbol_ctx(desks_db, desk["id"])
    assert cfg["guards"] == ["whitelist"] and assets == ["NQ"]
    desks_service.set_assets(desk["id"], ["NQ", "ES"], desks_db)
    assert config(desks_db, desk["id"])["guards"] == ["whitelist"]                       # once, not twice
    old = make_desk(desks_db, name="Vieja")
    legacy(desks_db, old["id"], {"max_premium_usd": 100})
    desks_service.set_assets(old["id"], ["NQ"], desks_db)
    assert "guards" not in config(desks_db, old["id"])                                   # no list of guards: every guard already runs
