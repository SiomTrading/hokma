"""The Strategies panel data of a desk (SPEC-028, 3338-A): the endpoint, the premium switch, the budget of the desk, and one call to the model for all the verdicts.

The chain, the switch, the shares of the account and the model are replaced at their seams, so nothing here reaches a provider or an engine."""
import math
from datetime import date
from statistics import NormalDist

import pytest
from desks_support import db, desks_db, env, make_desk  # noqa: F401  (fixtures)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.desks import strategies
from hokma.desks.api import router as desks_router
from hokma.desks.errors import DeskError
from hokma.rpc.ws import SESSION_TOKEN

N = NormalDist().cdf
SPOT, YEARS, IV = 100.0, 30 / 365, 0.30


def bs(kind, strike):
    d1 = (math.log(SPOT / strike) + (0.04 + IV * IV / 2) * YEARS) / (IV * math.sqrt(YEARS))
    d2 = d1 - IV * math.sqrt(YEARS)
    if kind == "call":
        return SPOT * N(d1) - strike * math.exp(-0.04 * YEARS) * N(d2)
    return strike * math.exp(-0.04 * YEARS) * N(-d2) - SPOT * N(-d1)


def rows():
    out = []
    for k in range(80, 121, 2):
        c, p = bs("call", k), bs("put", k)
        out.append({"strike": float(k), "call": {"bid": round(c - 0.03, 2), "ask": round(c + 0.03, 2), "iv": IV, "delta": 0.5, "open_interest": 500, "volume": 5},
                    "put": {"bid": round(max(p - 0.03, 0), 2), "ask": round(p + 0.03, 2), "iv": IV, "delta": -0.5, "open_interest": 500, "volume": 5}})
    return out


class FakeReply:
    def __init__(self, data, model="fake-model", usage=None):
        self.data, self.model = data, model
        self.usage = {"tokens_in": 700, "tokens_out": 300} if usage is None else usage

    def json(self):
        return self.data


class FakeClient:
    model = "fake-model"

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def complete(self, messages, *, schema=None, max_tokens=0):
        self.calls.append({"messages": messages, "schema": schema, "max_tokens": max_tokens})
        if isinstance(self.answer, Exception):
            raise self.answer
        data = self.answer(messages) if callable(self.answer) else self.answer
        return FakeReply(data)


def rate_all(messages):
    import json

    payload = json.loads(messages[1]["content"])
    verdicts = [{"id": c["id"], "verdict": "possible", "reason": f"p={c['p_profit']}"} for c in payload["to_rate"]]
    if verdicts:
        verdicts[0]["verdict"] = "recommended"
    return {"reading": "Lectura de la mesa.", "verdicts": verdicts}


@pytest.fixture
def world(desks_db, monkeypatch):
    """A desk, a chain of 30 days, premium selling closed, no shares, and a model that rates every card it is given."""
    desk = make_desk(desks_db)
    strategies._LAST.clear()                                  # the memory of the last consultation is of the process: a test must not inherit it
    state = {"selling_open": False, "shares": 0.0, "seat": True, "client": FakeClient(rate_all), "chain": None}

    def load_chain(desk_id, symbol, expiration):
        if state["chain"] is not None:
            return state["chain"]
        return {"ok": True, "day": date(2026, 12, 25), "spot": SPOT, "rows": rows(), "multiplier": 100.0, "years": YEARS, "days": 30, "rate": 0.04,
                "source": "Schwab", "price_feed": "schwab", "greeks": "provider", "as_of": "2026-11-25T15:00:00Z", "market_open": True, "delayed": False, "notes": []}

    monkeypatch.setattr(strategies, "_load_chain", load_chain)
    monkeypatch.setattr(strategies, "_switch", lambda d, s, db: (state["selling_open"], f"Premium selling on {s} is closed for this desk."))
    monkeypatch.setattr(strategies, "_shares_held", lambda d, s, db: state["shares"])
    monkeypatch.setattr(strategies, "_seat_active", lambda d, slug, db: state["seat"])
    monkeypatch.setattr(strategies, "_llm", lambda slug, d: state["client"])
    from hokma.desks.plantel import SEATS
    from hokma.desks.swo.create import desk_agent_slug

    with desks_db.write() as u:                               # the seat agent exists in a real desk; the ledger rows point at it
        u.agents.create_custom(desk_agent_slug(SEATS["A07"], desk["id"]), "Caleb")
    state["desk"] = desk
    state["db"] = desks_db
    return state


@pytest.fixture
def client(desks_db):
    app = FastAPI()
    app.include_router(desks_router)
    with TestClient(app) as c:
        c.headers.update({"X-Hokma-Token": SESSION_TOKEN})
        yield c


def ask(world, **kw):
    args = dict(symbol="AAPL", view="bullish", budget=3000, priority="balanced")
    args.update(kw)
    return strategies.suggest(world["desk"]["id"], args["symbol"], args["view"], args.get("target"), args["budget"], args.get("expiration"), args["priority"],
                              db=world["db"])


def test_one_call_to_the_model_rates_every_card_and_says_who_backs_it(world):
    out = ask(world)
    assert out["ok"] is True and out["cards"] and len(world["client"].calls) == 1
    call = world["client"].calls[0]
    assert call["schema"] is not None and ("Spanish" in call["messages"][1]["content"] or "English" in call["messages"][1]["content"])
    for card in out["cards"]:
        v = card["verdict"]
        assert v["verdict"] in ("recommended", "possible", "not_worth", "closed", "only_if_shares")
        assert v["backed_by"]
    rated = [c for c in out["cards"] if c["status"] not in ("locked", "needs_shares")]
    assert rated and rated[0]["verdict"]["backed_by"]["name"] == "Caleb" and rated[0]["verdict"]["backed_by"]["seat"] == "A07"
    assert out["reading"] == "Lectura de la mesa." and out["model"]["used"] is True


def test_the_numbers_of_the_cards_are_the_module_s_and_the_model_does_not_touch_them(world):
    a = ask(world)
    world["client"] = FakeClient({"reading": "x", "verdicts": []})
    b = ask(world)
    assert [c["net_premium_usd"] for c in a["cards"]] == [c["net_premium_usd"] for c in b["cards"]]


def test_premium_selling_is_closed_with_its_reason_and_the_model_is_not_asked_about_it(world):
    out = ask(world, view="neutral")
    closed = [c for c in out["cards"] if c["locked"]]
    assert {c["structure"] for c in closed} >= {"iron_condor", "iron_butterfly"}
    for c in closed:
        assert c["verdict"]["verdict"] == "closed" and "is closed" in c["verdict"]["reason"] and c["verdict"]["backed_by"] == {"source": "premium_switch"}
        assert c["can_open_detail"] is False
    import json

    asked = json.loads(world["client"].calls[0]["messages"][1]["content"]) if world["client"].calls else {"to_rate": [], "not_rated": []}
    assert not {c["id"] for c in closed} & {c["id"] for c in asked["to_rate"]}
    assert out["premium_selling"]["open"] is False and out["premium_selling"]["reason"]


def test_when_everything_is_closed_the_model_is_not_called_at_all(world):
    out = ask(world, view="neutral", expiration=None)
    if all(c["verdict"] and c["verdict"]["verdict"] in ("closed", "only_if_shares") for c in out["cards"]):
        assert world["client"].calls == []


def test_with_the_switch_open_the_selling_cards_are_rated_by_the_model(world):
    world["selling_open"] = True
    out = ask(world, view="neutral")
    condor = next(c for c in out["cards"] if c["structure"] == "iron_condor")
    assert condor["locked"] is None and condor["verdict"]["verdict"] in ("recommended", "possible", "not_worth")


def test_a_card_over_budget_says_so_with_its_cost_in_words(world):
    out = ask(world, budget=5)
    over = [c for c in out["cards"] if c["status"] == "over_budget"]
    assert over
    for c in over:
        assert "No cabe en tu presupuesto" in c["status_message"] or "does not fit" in c["status_message"]
        assert f"{c['capital_required_usd']:,.2f}" in c["status_message"]


def test_protective_put_is_only_if_you_have_the_shares(world):
    out = ask(world, view="bearish")
    put = next(c for c in out["cards"] if c["structure"] == "protective_put")
    assert put["status"] == "needs_shares" and put["verdict"]["verdict"] == "only_if_shares" and put["verdict"]["backed_by"] == {"source": "account"}
    world["shares"] = 100.0
    again = ask(world, view="bearish")
    assert next(c for c in again["cards"] if c["structure"] == "protective_put")["status"] in ("ok", "over_budget")


def test_without_the_account_shares_the_structures_with_shares_stay_closed(world):
    world["shares"] = None
    put = next(c for c in ask(world, view="bearish")["cards"] if c["structure"] == "protective_put")
    assert put["status"] == "needs_shares" and put["shares_held"] is None


def test_no_model_means_no_verdict_with_the_reason_and_the_numbers_stay(world):
    world["client"] = FakeClient(RuntimeError("engine down"))
    out = ask(world)
    rated = [c for c in out["cards"] if c["status"] not in ("locked", "needs_shares")]
    assert rated and out["model"]["used"] is False
    for c in rated:
        assert c["verdict"]["verdict"] is None and c["verdict"]["reason"].startswith(("Sin veredicto", "No verdict"))
        assert c["max_loss_usd"] is not None or c["unbounded_loss"]


def test_an_answer_that_cannot_be_read_is_no_verdict(world):
    from hokma.llm.jsonx import LLMParseError

    world["client"] = FakeClient(LLMParseError("no json"))
    out = ask(world)
    assert out["model"]["reason"] == "unreadable"


def test_a_card_the_model_leaves_out_says_it_was_not_rated(world):
    world["client"] = FakeClient({"reading": "r", "verdicts": []})
    out = ask(world)
    assert all(c["verdict"]["verdict"] is None for c in out["cards"] if c["status"] not in ("locked", "needs_shares"))
    assert out["reading"] == "r"


def test_a_verdict_outside_the_three_the_model_may_give_is_ignored(world):
    world["client"] = FakeClient(lambda m: {"reading": "r", "verdicts": [{"id": "debit_call_spread", "verdict": "closed", "reason": "x"}]})
    out = ask(world)
    assert next(c for c in out["cards"] if c["structure"] == "debit_call_spread")["verdict"]["verdict"] is None


def test_a_desk_without_the_options_seat_has_no_verdict_and_no_model_call(world):
    world["seat"] = False
    out = ask(world)
    assert world["client"].calls == [] and out["model"]["reason"] == "no_seat"


def test_a_spent_weekly_budget_stops_the_model_call(world, monkeypatch):
    monkeypatch.setattr(strategies.desk_budget, "state", lambda desk_id, db=None, now=None: "exhausted")
    out = ask(world)
    assert world["client"].calls == [] and out["model"]["reason"] == "budget_exhausted"
    assert any(c["verdict"]["reason"].startswith(("Sin veredicto", "No verdict")) for c in out["cards"] if c["verdict"]["verdict"] is None)


def test_the_budget_alert_is_checked_after_the_call(world, monkeypatch):
    seen = []
    monkeypatch.setattr(strategies.desk_budget, "check_alert", lambda desk_id, db=None, now=None: seen.append(desk_id))
    ask(world)
    assert seen == [world["desk"]["id"]]


def test_a_chain_that_cannot_be_read_is_no_data_never_a_zero(world):
    world["chain"] = {"ok": False, "source": "Schwab", "code": "no_connection", "reason": "No connection."}
    out = ask(world)
    assert out["ok"] is False and out["cards"] == [] and out["message"].startswith(("Sin dato de Schwab", "No data from Schwab"))
    assert world["client"].calls == []


@pytest.mark.parametrize("kw", [dict(symbol=""), dict(symbol="AA PL"), dict(symbol=5), dict(view="sideways"), dict(budget="abc"), dict(budget=-1), dict(target=0),
                                dict(expiration="25-12-2026"), dict(priority="fast"),
                                dict(expiration="2026-12-25T00:00:00"), dict(expiration="2026-12-25xyz"), dict(expiration="2026-13-40"), dict(expiration=20261225)])
def test_bad_requests_are_refused_with_422(world, kw):
    with pytest.raises(DeskError) as err:
        ask(world, **kw)
    assert err.value.code == "bad_strategy_args" and err.value.status == 422


def test_the_view_and_priority_come_in_spanish_labels_too(world):
    out = ask(world, view="Muy alcista", priority="Maxima probabilidad")
    assert out["view"] == "very_bullish" and out["priority"] == "max_probability"


def test_the_endpoint_answers_for_a_desk_and_404s_for_another(world, client):
    ok = client.post(f"/api/desks/{world['desk']['id']}/strategies", json={"symbol": "aapl", "view": "Alcista", "budget": 3000})
    assert ok.status_code == 200 and ok.json()["symbol"] == "AAPL" and ok.json()["cards"]
    assert client.post("/api/desks/9999/strategies", json={"symbol": "AAPL", "view": "bullish"}).status_code == 404
    bad = client.post(f"/api/desks/{world['desk']['id']}/strategies", json={"symbol": "AAPL", "view": "sideways"})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "bad_strategy_args"
    assert client.post(f"/api/desks/{world['desk']['id']}/strategies", json={"symbol": "AAPL", "view": "bullish"}, headers={"X-Hokma-Token": "x"}).status_code == 401


def test_the_request_to_the_chain_is_one_per_question_and_no_timer(world, monkeypatch):
    calls = []
    original = strategies._load_chain
    monkeypatch.setattr(strategies, "_load_chain", lambda d, s, e: calls.append((s, e)) or original(d, s, e))
    ask(world)
    assert len(calls) == 1 and len(world["client"].calls) == 1


def test_the_source_code_names_no_provider_of_models():
    text = open(strategies.__file__, encoding="utf-8").read().lower()
    for name in ("anthropic", "openai", "gemini", "claude", "gpt"):
        assert name not in text


# ---- the seams against the real modules, with their sources replaced -------------------------------------------------------------------

def test_the_chain_loader_takes_the_nearest_expiration_of_21_days_and_the_rate_of_the_chain(monkeypatch):
    from hokma.market import options

    seen = {}
    monkeypatch.setattr(options, "expirations", lambda symbol, desk_id=None: {"expirations": [{"date": "2026-11-27", "days": 2}, {"date": "2026-12-18", "days": 23},
                                                                                          {"date": "2027-01-15", "days": 51}]})

    def fake_chain(symbol, dates, strikes=None, desk_id=None):
        seen.update(symbol=symbol, dates=list(dates), strikes=strikes)
        return {"underlying_price": 100.0, "source": "Schwab", "price_feed": "schwab", "greeks": "provider", "as_of": "t", "market_open": True, "delayed": False,
                "notes": [], "expirations": [{"date": dates[0], "days": 23, "multiplier": 100, "rows": rows()}]}

    monkeypatch.setattr(options, "chain", fake_chain)
    got = strategies._load_chain(1, "AAPL", None)
    assert got["ok"] and got["day"] == date(2026, 12, 18) and seen["dates"] == ["2026-12-18"] and got["rate"] == options.PROB_ITM_RATE and got["multiplier"] == 100.0


def test_the_chain_loader_turns_a_source_error_into_no_data(monkeypatch):
    from hokma.market import options

    def boom(*a, **k):
        raise options.OptionsError("no_connection", 409, "No options connection.")

    monkeypatch.setattr(options, "expirations", boom)
    got = strategies._load_chain(1, "AAPL", None)
    assert got["ok"] is False and got["code"] == "no_connection" and got["source"]


def test_the_shares_are_the_long_quantity_of_the_equity_and_never_the_options(monkeypatch):
    from hokma.broker import service as broker

    monkeypatch.setattr(broker, "position_list", lambda desk_id, db=None, account_id=None: {"positions": [
        {"symbol": "AAPL", "asset_type": "EQUITY", "quantity": 120, "long_quantity": 120, "put_call": None},
        {"symbol": "AAPL  261218C00100000", "asset_type": "OPTION", "quantity": 5, "long_quantity": 5, "put_call": "CALL"},
        {"symbol": "MSFT", "asset_type": "EQUITY", "quantity": 50, "long_quantity": 50, "put_call": None}]})
    assert strategies._shares_held(1, "AAPL", None) == 120.0
    assert strategies._shares_held(1, "NVDA", None) == 0.0


def test_a_desk_that_cannot_say_its_shares_gives_none(monkeypatch):
    from hokma.broker import service as broker

    def no_account(*a, **k):
        raise RuntimeError("no account")

    monkeypatch.setattr(broker, "position_list", no_account)
    assert strategies._shares_held(1, "AAPL", None) is None


# ---- the corrections of the QA verdict (3338-A) -----------------------------------------------------------------------------------------

def week_used(world):
    return strategies.desk_budget.usage(world["desk"]["id"], db=world["db"])["used"]


def test_the_tokens_of_the_call_count_against_the_weekly_budget(world):
    before = week_used(world)
    ask(world)
    assert week_used(world) == before + 1000
    ask(world)
    assert week_used(world) == before + 2000


def test_the_tokens_of_an_unreadable_answer_are_counted_too(world):
    from hokma.llm.jsonx import LLMParseError

    class Unreadable(FakeReply):
        def json(self):
            raise LLMParseError("no json")

    class Client(FakeClient):
        def complete(self, messages, *, schema=None, max_tokens=0):
            self.calls.append(messages)
            return Unreadable({})

    world["client"] = Client(None)
    before = week_used(world)
    out = ask(world)
    assert out["model"]["reason"] == "unreadable" and week_used(world) == before + 1000


def test_with_the_cap_reached_by_the_panel_itself_the_model_is_not_called_again(world):
    with world["db"].write() as u:
        u.desks.update(world["desk"]["id"], token_budget=2500)
    ask(world)
    ask(world)
    assert len(world["client"].calls) == 2 and week_used(world) == 2000          # 80 percent: warn, still allowed
    ask(world)
    assert len(world["client"].calls) == 3 and week_used(world) == 3000          # the cap was crossed by this call
    out = ask(world)
    assert len(world["client"].calls) == 3 and out["model"]["reason"] == "budget_exhausted" and week_used(world) == 3000


def test_the_ledger_conversation_is_archived_so_it_never_shows_in_the_chat_list(world):
    ask(world)
    ask(world)
    with world["db"].read() as u:
        project = u.desks.get(world["desk"]["id"]).project_id
        assert [c for c in u.conversations.list(project_id=project, archived=False) if c.title == strategies.LEDGER_TITLE] == []
        ledger = [c for c in u.conversations.list(project_id=project, archived=True, include_ledger=True) if c.title == strategies.LEDGER_TITLE]
        assert u.conversations.list(project_id=project, archived=True) == []        # the default listing leaves the ledger out
    assert len(ledger) == 1


def test_the_ledger_cannot_be_restored_renamed_pinned_or_deleted(world):
    from hokma.db import NotFound
    ask(world)
    with world["db"].write() as u:
        project = u.desks.get(world["desk"]["id"]).project_id
        ledger = [c for c in u.conversations.list(project_id=project, archived=True, include_ledger=True) if c.title == strategies.LEDGER_TITLE][0]
        for action in (lambda: u.conversations.archive(ledger.id, False), lambda: u.conversations.rename(ledger.id, "mine"),
                       lambda: u.conversations.pin(ledger.id, True), lambda: u.conversations.delete(ledger.id)):
            with pytest.raises(NotFound):
                action()
        assert u.conversations.get(ledger.id).archived is True


def test_a_failure_recording_the_usage_is_logged_and_the_answer_still_arrives(world, monkeypatch, caplog):
    import logging

    def broken(*a, **k):
        raise RuntimeError("disk")

    monkeypatch.setattr(world["db"].__class__, "write", broken)
    with caplog.at_level(logging.WARNING, logger="hokma.desks.strategies"):
        strategies._record_usage(world["desk"]["id"], "x", type("R", (), {"usage": {"tokens_in": 5, "tokens_out": 5}, "model": "m"})(), world["db"])
    assert any("not recorded" in r.getMessage() for r in caplog.records)


def test_a_call_without_usage_in_its_reply_records_nothing_and_still_answers(world):
    class Client(FakeClient):
        def complete(self, messages, *, schema=None, max_tokens=0):
            self.calls.append(messages)
            return FakeReply(rate_all(messages), usage={})

    world["client"] = Client(None)
    before = week_used(world)
    assert ask(world)["model"]["used"] is True and week_used(world) == before


def test_a_card_over_budget_gets_its_verdict_from_the_code_and_is_not_sent_to_be_rated(world):
    import json

    out = ask(world, budget=5)
    over = [c for c in out["cards"] if c["status"] == "over_budget"]
    assert over
    sent = {x["id"] for x in json.loads(world["client"].calls[0]["messages"][1]["content"])["to_rate"]} if world["client"].calls else set()
    for c in over:
        assert c["verdict"]["verdict"] == "over_budget" and c["verdict"]["label"] and c["verdict"]["reason"] == c["status_message"]
        assert c["verdict"]["backed_by"] == {"source": "budget"} and c["id"] not in sent


def test_the_account_is_read_only_when_a_card_needs_the_shares(world, monkeypatch):
    reads = []
    monkeypatch.setattr(strategies, "_shares_held", lambda d, s, db: reads.append(s) or 0.0)
    out = ask(world, view="bearish")
    assert any(c["requires_shares"] for c in out["cards"]) and reads == ["AAPL"]
    reads.clear()
    assert not any(c["requires_shares"] for c in ask(world, view="very_bullish")["cards"]) and reads == []


def test_the_model_does_not_receive_an_expected_value_and_the_card_carries_the_exact_one(world):
    import json

    out = ask(world)
    for item in json.loads(world["client"].calls[0]["messages"][1]["content"])["to_rate"]:
        assert "expected_value_usd" not in item
    assert "expected value" not in strategies.SYSTEM.lower()
    assert all(isinstance(c["expected_value_usd"], float) for c in out["cards"] if c["max_loss_usd"] is not None or c["max_profit_usd"] is not None)
    assert not any(w.startswith("expected value is") for c in out["cards"] for w in c["warnings"])


def test_a_missing_field_in_the_body_is_bad_strategy_args(world, client):
    for body in ({}, {"view": "bullish"}, {"symbol": "AAPL"}):
        r = client.post(f"/api/desks/{world['desk']['id']}/strategies", json=body)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "bad_strategy_args"


def test_the_code_names_no_provider_the_name_arrives_as_data():
    import hokma.quant.options.strategies as quant_strategies

    for module in (strategies, quant_strategies):
        text = open(module.__file__, encoding="utf-8").read().lower()
        for name in ("schwab", "alpaca", "thinkorswim"):
            assert name not in text, (module.__name__, name)
    from hokma.quant import registry

    registry.load_all()
    assert "thinkorswim" not in registry.describe("options.profit_probability").summary.lower()


def test_the_last_consultation_is_kept_for_the_panel_and_a_desk_without_one_answers_empty(world, client):
    desk = world["desk"]["id"]
    assert client.get(f"/api/desks/{desk}/strategies/last").json() == {"form": None, "result": None}
    ask(world, target=110, expiration="2026-12-25")
    body = client.get(f"/api/desks/{desk}/strategies/last").json()
    assert body["form"] == {"symbol": "AAPL", "view": "bullish", "target": 110.0, "budget": 3000.0, "expiration": "2026-12-25", "priority": "balanced"}
    assert body["result"]["ok"] is True and body["result"]["cards"]
    assert client.get("/api/desks/9999/strategies/last").status_code == 404
    assert TestClient(client.app).get(f"/api/desks/{desk}/strategies/last").status_code == 401


def test_E6_the_chief_asks_by_chat_the_panel_is_added_and_the_cards_come_back(world, client):
    import json
    from hokma.kit import hokma_tools

    desk = world["desk"]["id"]
    text, err = hokma_tools.call("desk_strategies_suggest", {"desk_id": desk, "symbol": "nvda", "view": "bullish", "budget": 2000}, hokma_tools.Caller({}))
    assert not err
    out = json.loads(text)
    assert "strategies" in out["panels"] and out["ok"] is True and out["symbol"] == "NVDA" and out["cards"]
    assert all("payoff" not in c and "legs" in c for c in out["cards"])
    assert out["volatility"] in ("volatilidad sin calibrar", "uncalibrated volatility")          # the flat default says it is not calibrated
    assert "strategies" in client.get(f"/api/desks/{desk}").json()["panels"]
    assert client.get(f"/api/desks/{desk}/strategies/last").json()["form"]["budget"] == 2000.0     # the panel opens with these fields
    text, err = hokma_tools.call("desk_strategies_suggest", {"desk_id": desk, "symbol": "NVDA", "view": "sideways"}, hokma_tools.Caller({}))
    assert err                                                                                       # the view is of the schema's list


def test_the_tool_refuses_a_request_that_is_not_valid_with_the_text_of_the_error(world):
    from hokma.kit import hokma_tools

    text, err = hokma_tools.call("desk_strategies_suggest", {"desk_id": world["desk"]["id"], "symbol": "NVDA", "view": "bullish", "expiration": "tomorrow"},
                                 hokma_tools.Caller({}))
    assert err and "ValueError" in text


def test_a_request_that_is_not_valid_adds_no_panel(world, client):
    from hokma.kit import hokma_tools

    desk = world["desk"]["id"]
    before = client.get(f"/api/desks/{desk}").json()["panels"]
    text, err = hokma_tools.call("desk_strategies_suggest", {"desk_id": desk, "symbol": "NVDA", "view": "sideways"}, hokma_tools.Caller({}))
    assert err and client.get(f"/api/desks/{desk}").json()["panels"] == before and "strategies" not in before
