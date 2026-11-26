"""Orders of a desk with Schwab (task 3299, SPEC-015 18.2 points 6 to 12): every kind of order of 18.2 point 7 goes out as the
service sends it: previewOrder first with the same body, then the POST of the orders template, the order id read from
Location, the attempt recorded; a replace and a cancel read the order first and refuse (without calling) when it is not
editable or cancelable. Bodies come from the recipes of REF 4 and the schema of B.1 (the ones of test_schwab_order_endpoints.py);
the answers of Schwab are made up and come from a fake server. Nothing leaves the machine."""
import json

import pytest
from broker_orders_support import (ACCOUNT, account_body, pos, FIRST_ID, ORDERS, PREVIEW_PATH, SECRETS, calls, connections, created, desks_db,  # noqa: F401
                                   env, db, eq, fake, keys, limit, order_body, ready, rows, sent, server)
from test_schwab_order_endpoints import RECIPES

from hokma.broker import service
from hokma.broker.service import BrokerError, Who

AGENT = Who(agent="desk-chief", conversation_id=7, task_id=None)

# one test per kind of order of 18.2 point 7 (REF 3.5 and 4): the names are the ones of RECIPES
KINDS = {
    "mercado": "market",
    "limite": "limit_gtc",
    "stop": "stop",
    "stop_limite": "stop_limit",
    "trailing_stop_porcentaje": "trailing_stop_percent",
    "trailing_stop_valor": "trailing_stop_value",
    "trailing_stop_limite": "trailing_stop_limit",
    "al_cierre_mercado": "market_on_close",
    "al_cierre_limite": "limit_on_close",
    "net_debit": "vertical_net_debit",
    "net_credit": "vertical_net_credit",
    "opcion_una_pata_occ": "option_one_leg_occ",
    "vertical": "vertical_net_debit",
    "calendario": "calendar",
    "iron_condor": "iron_condor",
    "trigger_con_hija": "trigger_with_child",
    "oco": "oco",
    "bracket_trigger_con_oco": "bracket_trigger_with_oco",
}


CREDIT_KINDS = {"net_credit", "iron_condor"}          # they receive a credit: premium selling, closed for a desk until a calibrated threshold exists (SPEC-019 4.5)


def _debit_marks(order):
    """Marks for the option legs of an order: a bought leg is worth more than a sold one, so a covered structure reads as a debit."""
    from decimal import Decimal

    from hokma.contracts import symbols

    marks = {}
    for leg in order.get("orderLegCollection", []):
        if leg["instrument"].get("assetType") == "OPTION":
            marks[symbols.from_provider("schwab", leg["instrument"]["symbol"])] = Decimal(5 if leg["instruction"].startswith("BUY") else 1)
    return lambda ids: {i: marks[i] for i in ids if i in marks}


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_the_service_previews_then_posts_the_same_body_reads_the_id_and_records(ready, kind, monkeypatch):
    from hokma.exec import prefilter

    order = RECIPES[KINDS[kind]]
    monkeypatch.setattr(prefilter, "_mark_source", _debit_marks(order))
    if kind in CREDIT_KINDS:
        with pytest.raises(BrokerError) as refused:
            service.place(ready.desk_id, order, f"test of {kind}", AGENT, db=ready.db)
        assert "premium_selling_closed" in str(refused.value)
        assert ORDERS not in [c.path for c in calls(ready.fake, "POST")]            # nothing was posted to the broker
        return
    if kind == "oco":                                                         # an OCO of exits only goes with something held to exit
        ready.fake.on("GET", ACCOUNT, account_body([pos("XYZ", long=5, value=50)]))
    answer = service.place(ready.desk_id, order, f"test of {kind}", AGENT, db=ready.db)

    assert answer["status"] == "sent" and answer["order_id"] == FIRST_ID and answer["approved_by"] == "auto"
    assert answer["correl_id"] == "test-correl-id"
    posts = [c for c in calls(ready.fake, "POST")]
    assert [c.path for c in posts] == [PREVIEW_PATH, ORDERS]                    # preview first, then the template of 18.7
    assert [json.loads(c.body) for c in posts] == [order, order]                # the same body, as given (a price may be text)
    assert all(c.headers["authorization"].startswith("Bearer ") for c in posts)
    assert not [c for c in calls(ready.fake) if c.method in ("PUT", "DELETE")]

    log = rows(ready)
    assert len(log) == 1
    row = log[0]
    assert (row.action, row.rule_result, row.approved_by, row.http_status) == ("place", "auto", "auto", 201)
    assert (row.broker_order_id, row.agent_slug, row.conversation_id, row.broker_kind) == (str(FIRST_ID), "desk-chief", 7, "schwab")
    assert row.request_json == order and row.reason == f"test of {kind}" and row.correl_id == "test-correl-id"
    assert row.response_json["order_id"] == FIRST_ID and "preview" in row.response_json
    with ready.db.read() as u:
        entry = u.journal.list(project_id=ready.project_id)[0]
    assert (entry.kind, entry.outcome, entry.agent_slug) == ("action", "success", "desk-chief")


@pytest.fixture(autouse=True)
def _no_stop_check(monkeypatch):
    """These tests are about the preview, the post and the record of Schwab orders (futures and spreads without a stop among them); the stop of an entry has its own tests (SPEC-026, 3390e3)."""
    monkeypatch.setattr("hokma.exec.guards.stop.check_stop", lambda *a, **k: None)


def test_the_preview_that_comes_back_never_carries_the_account_number(ready):
    answer = service.place(ready.desk_id, RECIPES["limit_gtc"], "test", AGENT, db=ready.db)
    text = json.dumps(answer)
    assert [s for s in SECRETS if s in text] == [], text
    assert answer["preview"]["orderStrategy"]["account_ref"] == "A1" and answer["preview"]["orderStrategy"]["mask"] == "••••5678"


def test_a_preview_alone_sends_nothing_and_returns_the_validation_and_the_rule(ready):
    answer = service.preview(ready.desk_id, RECIPES["bracket_trigger_with_oco"], AGENT, db=ready.db)
    assert answer["status"] == "previewed" and answer["rule"]["result"] == "auto"
    assert "orderValidationResult" in answer["preview"] and "commissionAndFee" in answer["preview"]
    assert [c.path for c in calls(ready.fake, "POST")] == [PREVIEW_PATH]
    assert [r.action for r in rows(ready)] == ["preview"]


def test_a_replace_previews_the_new_order_puts_it_and_records_the_old_id(ready):
    ready.fake.on("GET", f"{ORDERS}/555", order_body(555, editable=True))
    created(ready.fake, 556, f"{ORDERS}/555", "PUT")
    new = limit("BUY", 1, "11.00")
    answer = service.replace(ready.desk_id, 555, new, "move the price up", AGENT, db=ready.db)

    assert answer["status"] == "replaced" and answer["order_id"] == 556 and answer["replaces_order_id"] == "555"
    methods = [(c.method, c.path) for c in calls(ready.fake) if c.method != "GET"]
    assert methods == [("POST", PREVIEW_PATH), ("PUT", f"{ORDERS}/555")]
    assert [m for m in (c.method for c in calls(ready.fake))][0] == "GET"        # the order was read first
    assert json.loads(calls(ready.fake, "PUT")[0].body) == new
    row = rows(ready)[0]
    assert (row.action, row.replaces_order_id, row.broker_order_id, row.http_status) == ("replace", "555", "556", 201)


def test_a_replace_of_an_order_that_is_not_editable_is_refused_without_calling(ready):
    ready.fake.on("GET", f"{ORDERS}/555", order_body(555, editable=False))
    with pytest.raises(BrokerError) as refused:
        service.replace(ready.desk_id, 555, limit(), "try", AGENT, db=ready.db)
    assert refused.value.code == "not_editable" and refused.value.status == 409
    assert not [c for c in calls(ready.fake) if c.method in ("PUT", "POST", "DELETE")]       # no preview, no change
    row = rows(ready)[0]
    assert (row.action, row.rule_result, row.http_status, row.replaces_order_id) == ("replace", "refused", None, "555")


def test_a_cancel_reads_the_order_deletes_it_and_records_it(ready):
    ready.fake.on("GET", f"{ORDERS}/555", order_body(555, cancelable=True))
    ready.fake.on("DELETE", f"{ORDERS}/555", (200, None))
    answer = service.cancel(ready.desk_id, 555, "the setup is gone", AGENT, db=ready.db)
    assert answer["status"] == "canceled" and answer["order_id"] == 555 and answer["approved_by"] == "auto"
    assert [(c.method, c.path) for c in calls(ready.fake) if c.method != "GET"] == [("DELETE", f"{ORDERS}/555")]
    row = rows(ready)[0]
    assert (row.action, row.rule_result, row.http_status, row.broker_order_id) == ("cancel", "auto", 200, "555")


def test_a_cancel_of_an_order_that_is_not_cancelable_is_refused_without_calling(ready):
    ready.fake.on("GET", f"{ORDERS}/555", order_body(555, status="FILLED", cancelable=False, editable=False))
    with pytest.raises(BrokerError) as refused:
        service.cancel(ready.desk_id, 555, "try", AGENT, db=ready.db)
    assert refused.value.code == "not_editable"
    assert not [c for c in calls(ready.fake) if c.method in ("PUT", "POST", "DELETE")]
    assert [(r.action, r.rule_result) for r in rows(ready)] == [("cancel", "refused")]


def test_an_order_that_does_not_exist_is_a_404_and_calls_nothing(ready):
    ready.fake.on("GET", f"{ORDERS}/777", (404, {"errors": [{"status": "404"}]}))
    with pytest.raises(BrokerError) as refused:
        service.cancel(ready.desk_id, 777, "try", AGENT, db=ready.db)
    assert refused.value.code == "order_not_found" and refused.value.status == 404
    assert not [c for c in calls(ready.fake) if c.method == "DELETE"]


def test_an_invalid_order_is_refused_before_any_call_and_the_attempt_is_recorded_without_the_body(ready):
    bad = limit()
    bad["accountNumber"] = "12345678"                   # an agent must not name an account: it is the desk's
    with pytest.raises(BrokerError) as refused:
        service.place(ready.desk_id, bad, "test", AGENT, db=ready.db)
    assert refused.value.status == 422
    assert calls(ready.fake, "POST") == []
    row = rows(ready)[0]
    assert row.request_json is None and row.rule_result == "refused"


def test_a_failure_of_schwab_at_the_send_is_recorded_and_says_the_order_did_not_go(ready):
    ready.fake.on("POST", ORDERS, (400, {"message": "no", "errors": ["bad price"]}))
    with pytest.raises(BrokerError) as failed:
        service.place(ready.desk_id, limit(), "test", AGENT, db=ready.db)
    assert failed.value.status == 400
    row = rows(ready)[0]
    assert (row.action, row.http_status, row.rule_result, row.broker_order_id) == ("place", 400, "auto", None)
    with ready.db.read() as u:
        assert u.journal.list(project_id=ready.project_id)[0].outcome == "failure"


def test_a_201_without_a_readable_location_is_an_order_that_exists_not_an_error(ready):
    ready.fake.on("POST", ORDERS, (201, None, {}))
    answer = service.place(ready.desk_id, limit(), "test", AGENT, db=ready.db)
    assert answer["status"] == "sent" and answer["order_id"] is None and "read the orders" in answer["note"]


def test_a_reason_is_required_and_a_bad_one_is_refused_before_anything(ready):
    for reason in (None, "", "   ", "x" * 2001, 7):
        with pytest.raises(BrokerError) as refused:
            service.place(ready.desk_id, limit(), reason, AGENT, db=ready.db)
        assert refused.value.code == "bad_reason"
    assert calls(ready.fake) == [] and rows(ready) == []
    assert service.place(ready.desk_id, limit(), "x" * 2000, AGENT, db=ready.db)["status"] == "sent"


def test_the_order_that_a_replace_is_changing_is_not_counted_twice_against_the_capital(ready):
    """The open order of 900 is the one being replaced: the new one of 900 fits in a desk of 1,000."""
    from broker_orders_support import preview_body, set_capital

    set_capital(ready, 1000)
    ready.fake.on("GET", ORDERS, [order_body(555, price="9.00", quantity=100.0)])
    ready.fake.on("GET", f"{ORDERS}/555", order_body(555, price="9.00", quantity=100.0))
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=900.0))
    created(ready.fake, 556, f"{ORDERS}/555", "PUT")
    answer = service.replace(ready.desk_id, 555, limit("BUY", 100, "9.00"), "same size", AGENT, db=ready.db)
    assert answer["status"] == "replaced"
    ready.fake.on("POST", ORDERS, (201, None, {"Location": ORDERS + "/557"}))
    again = service.place(ready.desk_id, limit("BUY", 100, "9.00"), "a second one", AGENT, db=ready.db)
    assert again["status"] == "needs_owner"          # a NEW order does count the open one: 900 + 900 > 1000
