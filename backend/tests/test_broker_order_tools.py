"""The order tools of the chat (task 3299, SPEC-015 18.4): order_preview, order_place, order_replace, order_cancel, order_status,
order_list, position_list and order_log, and the new limits field of desk_configure. Strict schema with the whole OrderRequest and
its enums, the fixed description of the writes, the writes ask and the reads do not, a desk chief operates only its own desk,
a refusal is an error result with the catalog text, nothing returns an identifier of the account."""
import json

import pytest
from broker_orders_support import (ACCOUNT, ORDERS, PREVIEW_PATH, SECRETS, account_body, connections, created, desks_db, db,  # noqa: F401
                                   env, fake, keys, limit, order_body, pos, preview_body, ready, rows, server, set_capital)

from hokma.broker import service
from hokma.desks import service as desks
from hokma.i18n import t
from hokma.kit import hokma_tools
from hokma.kit.hokma_tools import Caller
from hokma.schwab import models

WRITE_TEXT = ("Places, replaces or cancels a REAL-MONEY order in the broker account of the desk through Hokma. Hokma "
              "previews it first and asks the owner when the desk rules say so.")


def run(name, args, agent="hokma", **env):
    text, is_error = hokma_tools.call(name, args, Caller({"HOKMA_AGENT": agent, **env}))
    return (json.loads(text) if not is_error else text), is_error


def as_desk(desk_id, agent="desk-chief"):
    return Caller({"HOKMA_AGENT": agent, "HOKMA_CONVERSATION_ID": str(desks.open_conversation(desk_id)["conversation_id"])})


def call_as(caller, name, args):
    text, is_error = hokma_tools.call(name, args, caller)
    return (json.loads(text) if not is_error else text), is_error


# ------------------------------------------------------------------------------------------------------------ the schema

def test_the_eight_tools_are_declared_strict_with_the_right_writes_and_arguments():
    listed = {d["name"]: d for d in hokma_tools.definitions()}
    spec = {"order_preview": (False, ["desk_id", "order"]), "order_place": (True, ["desk_id", "order", "reason"]),
            "order_replace": (True, ["desk_id", "order_id", "order", "reason"]), "order_cancel": (True, ["desk_id", "order_id", "reason"]),
            "order_status": (False, ["desk_id", "order_id"]), "order_list": (False, ["desk_id"]), "position_list": (False, ["desk_id"]),
            "order_log": (False, ["desk_id"])}
    for name, (writes, required) in spec.items():
        schema = listed[name]["inputSchema"]
        assert hokma_tools.TOOLS[name][4] is writes and name in hokma_tools.NO_UNIT, name
        assert schema["required"] == required and schema["additionalProperties"] is False, name
        assert "desk_id" in schema["properties"]
    for name in ("order_place", "order_replace", "order_cancel"):
        assert listed[name]["description"] == WRITE_TEXT
        assert listed[name]["inputSchema"]["properties"]["reason"]["minLength"] == 1
        assert listed[name]["inputSchema"]["properties"]["reason"]["maxLength"] == 2000
    assert {"status", "fromEnteredTime", "toEnteredTime", "maxResults"} <= set(listed["order_list"]["inputSchema"]["properties"])
    assert {"since", "limit"} <= set(listed["order_log"]["inputSchema"]["properties"])
    assert "simulat" not in " ".join(d["description"].lower() for n, d in listed.items() if n.startswith(("order_", "position_")))


def test_the_order_argument_declares_the_whole_order_request_and_every_enum():
    schema = hokma_tools.definitions()[[d["name"] for d in hokma_tools.definitions()].index("order_place")]["inputSchema"]
    order = schema["properties"]["order"]
    assert order["type"] == "object" and order["required"] == ["orderStrategyType"] and order["additionalProperties"] is False
    props = order["properties"]
    for field, enum in (("session", "Session"), ("duration", "Duration"), ("orderType", "OrderTypeRequest"),
                        ("complexOrderStrategyType", "ComplexOrderStrategyType"), ("stopPriceLinkBasis", "StopPriceLinkBasis"),
                        ("stopPriceLinkType", "StopPriceLinkType"), ("stopType", "StopType"), ("priceLinkBasis", "PriceLinkBasis"),
                        ("priceLinkType", "PriceLinkType"), ("taxLotMethod", "TaxLotMethod"),
                        ("specialInstruction", "SpecialInstruction"), ("orderStrategyType", "OrderStrategyType")):
        assert props[field]["enum"] == [v for v in models.enum_values(enum) if v != "UNKNOWN"], field
    assert set(props["orderStrategyType"]["enum"]) == {"SINGLE", "CANCEL", "RECALL", "PAIR", "FLATTEN", "TWO_DAY_SWAP", "BLAST_ALL",
                                                       "OCO", "TRIGGER"}
    assert props["price"]["type"] == ["number", "string"] and props["stopPriceOffset"]["type"] == ["number", "string"]
    leg = props["orderLegCollection"]["items"]
    assert leg["required"] == ["instruction", "instrument"]
    real = lambda enum: [v for v in models.enum_values(enum) if v != "UNKNOWN"]      # UNKNOWN is only ever an answer
    assert leg["properties"]["instruction"]["enum"] == real("Instruction")
    assert leg["properties"]["positionEffect"]["enum"] == real("PositionEffect")
    assert leg["properties"]["quantityType"]["enum"] == real("QuantityType")
    assert leg["properties"]["divCapGains"]["enum"] == real("DivCapGains")
    assert leg["properties"]["orderLegType"]["enum"] == real("OrderLegType")
    instrument = leg["properties"]["instrument"]["properties"]
    assert instrument["assetType"]["enum"] == real("AccountAssetType") and instrument["putCall"]["enum"] == real("PutCall") and "OCC" in instrument["symbol"]["description"]
    assert props["childOrderStrategies"]["items"] == {"$ref": "#/$defs/OrderRequest"} == props["replacingOrderCollection"]["items"]
    assert schema["$defs"]["OrderRequest"]["properties"] == props
    json.dumps(schema)                                                                          # it is plain JSON


def test_a_wrong_argument_is_refused_by_the_schema_before_any_call():
    text, err = hokma_tools.call("order_place", {"desk_id": 1, "order": "BUY AAPL", "reason": "x"}, Caller({"HOKMA_AGENT": "hokma"}))
    assert err and "order must be object" in text
    text, err = hokma_tools.call("order_place", {"desk_id": 1, "order": {}}, Caller({"HOKMA_AGENT": "hokma"}))
    assert err and "missing argument: reason" in text
    text, err = hokma_tools.call("order_place", {"desk_id": 1, "order": {}, "reason": "x", "account": "A1"}, Caller())
    assert err and "unknown argument: account" in text


# ------------------------------------------------------------------------------------------------------- the tools work

def test_place_preview_status_list_replace_cancel_and_positions_through_the_tools(ready):
    ready.fake.on("GET", ORDERS, [order_body(555, price="9.00")])
    ready.fake.on("GET", ACCOUNT, account_body([pos("AAPL", long=10, value=1500.0, day=20.0, open_pl=50.0)]))
    d = ready.desk_id
    previewed, err = run("order_preview", {"desk_id": d, "order": limit()})
    assert not err and previewed["status"] == "previewed" and previewed["rule"]["result"] == "auto"
    placed, err = run("order_place", {"desk_id": d, "order": limit(), "reason": "tool test"})
    assert not err and placed["status"] == "sent" and placed["order_id"] == 600000123
    assert rows(ready)[0].agent_slug == "hokma" and rows(ready)[0].reason == "tool test"

    ready.fake.on("GET", f"{ORDERS}/555", order_body(555, price="9.00"))
    status, err = run("order_status", {"desk_id": d, "order_id": 555})
    assert not err and status["order"]["orderId"] == 555 and status["account_ref"] == "A1" and status["mask"] == "••••5678"
    listed, err = run("order_list", {"desk_id": d, "status": "WORKING", "maxResults": 5})
    assert not err and [o["orderId"] for o in listed["orders"]] == [555] and listed["account_ref"] == "A1"
    query = [c for c in ready.fake.calls if c.path == ORDERS and c.method == "GET"][-1].query
    assert query["status"] == "WORKING" and query["maxResults"] == "5"

    created(ready.fake, 556, f"{ORDERS}/555", "PUT")
    replaced, err = run("order_replace", {"desk_id": d, "order_id": "555", "order": limit("BUY", 1, "9.50"), "reason": "reprice"})
    assert not err and replaced["status"] == "replaced" and replaced["order_id"] == 556
    ready.fake.on("GET", f"{ORDERS}/556", order_body(556))
    ready.fake.on("DELETE", f"{ORDERS}/556", (200, None))
    canceled, err = run("order_cancel", {"desk_id": d, "order_id": 556, "reason": "done"})
    assert not err and canceled["status"] == "canceled"

    held, err = run("position_list", {"desk_id": d})
    assert not err and held["account_ref"] == "A1" and held["positions"][0]["symbol"] == "AAPL"
    assert held["total"]["market_value"] > 0 and "day_pnl" in held["total"] and "open_pnl" in held["total"]
    assert set(held["greeks"]) >= {"positions", "total", "by_underlying"} or held["greeks_error"]

    log, err = run("order_log", {"desk_id": d})
    assert not err and [o["action"] for o in log["orders"]] == ["cancel", "replace", "place", "preview"]


def test_nothing_the_tools_return_has_an_identifier_of_the_account(ready):
    ready.fake.on("GET", ORDERS, [order_body(555)])
    ready.fake.on("GET", f"{ORDERS}/555", order_body(555))
    ready.fake.on("GET", ACCOUNT, account_body([pos("AAPL", long=1, value=10.0)]))
    d = ready.desk_id
    texts = [hokma_tools.call(name, args, Caller({"HOKMA_AGENT": "hokma"}))[0] for name, args in (
        ("order_preview", {"desk_id": d, "order": limit()}), ("order_place", {"desk_id": d, "order": limit(), "reason": "x"}),
        ("order_status", {"desk_id": d, "order_id": 555}), ("order_list", {"desk_id": d}), ("position_list", {"desk_id": d}),
        ("order_log", {"desk_id": d}), ("desk_show", {"desk_id": d}), ("broker_list", {}))]
    for text in texts:
        for secret in SECRETS:
            assert secret not in text, (secret, text[:200])


def test_a_refusal_is_an_error_result_with_the_catalog_text_and_the_attempt_is_logged(ready):
    service.set_orders_enabled(ready.broker_id, False, db=ready.db)
    text, err = run("order_place", {"desk_id": ready.desk_id, "order": limit(), "reason": "x"})
    assert err and t("broker.err.orders_off") in text
    assert [(r.rule_result, r.agent_slug) for r in rows(ready)] == [("refused", "hokma")]
    text, err = run("order_place", {"desk_id": ready.desk_id, "order": limit(), "reason": ""})
    assert err and t("broker.err.bad_reason") in text
    text, err = run("order_cancel", {"desk_id": ready.desk_id, "order_id": "abc", "reason": "x"})
    assert err and t("broker.err.bad_order_id") in text
    text, err = run("order_place", {"desk_id": 999, "order": limit(), "reason": "x"})
    assert err and t("broker.err.desk_not_found") in text


def test_an_order_that_needs_the_owner_answers_needs_owner_not_an_error(ready):
    set_capital(ready, 1000)
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=5000.0))
    answer, err = run("order_place", {"desk_id": ready.desk_id, "order": limit("BUY", 500, "10"), "reason": "big"}, agent="hokma")
    assert not err and answer["status"] == "needs_owner" and isinstance(answer["decision_id"], int) and "5,000.00" in answer["why"]
    listed, err = run("decision_list", {})
    assert not err and listed[0]["id"] == answer["decision_id"] and listed[0]["status"] == "proposed"
    assert listed[0]["proposed_by"] in ("desk-chief", "hokma")


def test_a_desk_chief_operates_only_the_desk_of_its_project(ready):
    from desks_support import make_desk

    other = make_desk(ready.db, name="Otra mesa")
    chief = as_desk(ready.desk_id)
    args = {"order": limit(), "reason": "x"}
    placed, err = call_as(chief, "order_place", {"desk_id": ready.desk_id, **args})
    assert not err and placed["status"] == "sent"
    for name, extra in (("order_place", args), ("order_preview", {"order": limit()}), ("order_cancel", {"order_id": 1, "reason": "x"}),
                        ("order_replace", {"order_id": 1, **args}), ("order_status", {"order_id": 1}), ("order_list", {}),
                        ("position_list", {}), ("order_log", {})):
        text, err = call_as(chief, name, {"desk_id": other["id"], **extra})
        assert err and "is not the desk of this project" in text, name
    outside, err = run("order_place", {"desk_id": ready.desk_id, **args}, agent="desk-chief")          # no project at all
    assert err and "desk chief operates only the desk of its project" in outside


# ----------------------------------------------------------------------------------------------- desk_configure limits

def test_desk_configure_sets_merges_and_clears_the_limits_and_desk_show_returns_them(ready):
    d = ready.desk_id
    shown, err = run("desk_configure", {"desk_id": d, "limits": {"max_order_pct": 5, "short_allowed": False}})
    assert not err and shown["limits"] == {"max_order_pct": 5.0, "short_allowed": False, "guards": []}
    assert run("desk_configure", {"desk_id": d, "limits": {"max_order_pct": None}})[1]                  # turning off is the owner's
    shown, err = run("desk_money_set", {"desk_id": d, "limits": {"max_order_pct": None, "max_orders_per_day": 3}})
    assert not err and shown["limits"] == {"short_allowed": False, "max_orders_per_day": 3, "guards": []}
    assert run("desk_show", {"desk_id": d})[0]["limits"] == {"short_allowed": False, "max_orders_per_day": 3, "guards": []}
    assert service.get_limits(d, db=ready.db) == {"short_allowed": False, "max_orders_per_day": 3, "guards": []}
    assert desks.get_desk(d, ready.db)["limits"] == {"short_allowed": False, "max_orders_per_day": 3, "guards": []}


def test_a_bad_limit_changes_nothing_at_all(ready):
    d = ready.desk_id
    for bad in ({"max_order_pct": 0}, {"foo": True}, {"assets_only": "yes"}, {"max_orders_per_day": 1.5}):
        text, err = run("desk_configure", {"desk_id": d, "name": "Nombre nuevo", "mandate": "otro", "limits": bad})
        assert err and "limits" in text, bad
    desk = desks.get_desk(d, ready.db)
    assert desk["name"] == "Futuros NQ" and desk["limits"] == {"guards": []} and desk["mandate"] != "otro"
    text, err = run("desk_configure", {"desk_id": d})
    assert err and "limits" in text                                                                # the help names the new field


def test_a_desk_chief_changes_the_limits_of_its_own_desk_only(ready):
    from desks_support import make_desk

    other = make_desk(ready.db, name="Otra mesa")
    chief = as_desk(ready.desk_id)
    ok, err = call_as(chief, "desk_configure", {"desk_id": ready.desk_id, "limits": {"assets_only": True}})
    assert not err and ok["limits"] == {"assets_only": True, "guards": ["whitelist"]}
    text, err = call_as(chief, "desk_configure", {"desk_id": other["id"], "limits": {"assets_only": True}})
    assert err and "not the desk of this project" in text and service.get_limits(other["id"], db=ready.db) == {"guards": []}
