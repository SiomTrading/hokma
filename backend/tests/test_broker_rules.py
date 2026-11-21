"""The rules of an order of a desk (task 3299, SPEC-015 18.3 and 18.5, criteria 23 to 29): every row of the table, every optional
limit on and off, closing orders and cancellations that leave over the capital, the pending decision that is created, announced,
previewed again on approval and expired at 30 minutes. The pure function is tested with plain data; the service with a fake
Schwab. Nothing leaves the machine."""
import datetime as dt
import json

import pytest
from broker_orders_support import (ORDERS, PREVIEW_PATH, account_body, calls, connections, created, desks_db, detail,  # noqa: F401
                                   db, env, eq, fake, keys, limit, link_telegram, order_body, outbox, pos, preview_body,
                                   ready, rows, sent, server, set_capital, set_status)

from hokma.broker import rules, service
from hokma.broker.service import BrokerError, Who
from hokma.broker.types import Order, Position
from hokma.i18n import t
from hokma.kit import hokma_tools
from hokma.permissions import modes

AGENT = Who(agent="desk-chief", conversation_id=3, task_id=None)
OWNER = service.owner_who()


# ====================================================================================================== the pure function

def position(symbol="AAPL", long=0.0, short=0.0, value=0.0, day=0.0, mult=None, asset="EQUITY"):
    return Position(account_id=1, symbol=symbol, qty=long - short, avg_price=10.0, asset_type=asset, long_qty=long,
                    short_qty=short, market_value=value, day_pl=day, multiplier=mult)


def open_order(order_id, price, quantity, symbol="AAPL", instruction="BUY", status="WORKING"):
    data = order_body(order_id, price=price, quantity=quantity, symbol=symbol, instruction=instruction)
    return Order(account_id=1, symbol=symbol, side=instruction, qty=quantity, order_id=str(order_id), status=status, data=data)


def good(**more):
    """The facts of a quiet desk of 10,000: orders on, active, with an account, a clean preview of 1,000, a limit buy."""
    facts = dict(action="place", orders_enabled=True, desk_status="active", account_ok=True, order=limit(),
                 preview=preview_body(value=1000.0), capital=10000.0)
    facts.update(more)
    return rules.evaluate(**facts)


def test_a_quiet_order_leaves_on_its_own():
    answer = good()
    assert (answer["result"], answer["code"], answer["why"], answer["flags"]) == ("auto", "ok", "", [])


@pytest.mark.parametrize("action", ["preview", "place", "replace", "cancel"])
def test_the_switch_off_refuses_every_action_with_its_catalog_text(action):
    answer = good(action=action, orders_enabled=False)
    assert (answer["result"], answer["code"]) == ("refused", "orders_off") and answer["why"] == t("broker.err.orders_off")


@pytest.mark.parametrize("status", ["paused", "archived"])
def test_a_paused_or_archived_desk_refuses_even_a_cancellation(status):
    for action in ("place", "cancel"):
        answer = good(action=action, desk_status=status)
        assert (answer["result"], answer["code"]) == ("refused", "desk_inactive")
        assert answer["why"] == t("broker.err.desk_inactive")


def test_a_desk_without_an_account_that_trades_is_refused():
    answer = good(account_ok=False)
    assert (answer["result"], answer["code"]) == ("refused", "no_trading_account")
    assert answer["why"] == t("broker.err.no_trading_account")


def test_rejects_of_schwab_refuse_with_its_reason_and_closing_orders_are_not_exempt():
    sell = limit("SELL", 100, "10.00")
    answer = good(preview=preview_body(rejects=["Order would exceed the position"]), order=sell,
                  positions=[position(long=100, value=1000)])
    assert (answer["result"], answer["code"]) == ("refused", "rejected")
    assert "Order would exceed the position" in answer["why"] and answer["why"].startswith(t("broker.err.rejected", reason="")[:10])


def test_a_negative_projected_buying_power_is_refused_with_the_figure():
    answer = good(preview=preview_body(power=-250.5))
    assert (answer["result"], answer["code"]) == ("refused", "rejected") and "-250.50" in answer["why"]


@pytest.mark.parametrize("key", ["reviews", "warns", "alerts"])
def test_reviews_warns_and_alerts_ask_the_owner_with_the_messages(key):
    answer = good(preview=preview_body(**{key: ["Check the symbol"]}))
    assert (answer["result"], answer["code"], answer["flags"]) == ("needs_owner", "review", ["review"])
    assert "Check the symbol" in answer["why"]


def test_a_closing_order_still_asks_when_schwab_asks_for_a_review():
    answer = good(order=limit("SELL", 10), positions=[position(long=10, value=100)], preview=preview_body(warns=["Penny stock"]))
    assert answer["result"] == "needs_owner" and answer["flags"] == ["review"]


def test_the_capital_adds_positions_open_orders_that_open_and_the_new_order():
    held = [position("MSFT", long=10, value=-6000.0)]                       # the absolute value counts
    waiting = [open_order(1, "50.00", 20), open_order(2, "9.00", 10, instruction="SELL", symbol="MSFT"),
               open_order(3, "40.00", 10, status="FILLED")]                  # a sell of a long is not exposure; a filled one is gone
    held += [position("AAPL", long=0)]
    fits = good(positions=held, open_orders=waiting, preview=preview_body(value=2000.0))
    assert fits["result"] == "auto" and fits["numbers"]["committed"] == 6000 + 1000 + 2000
    over = good(positions=held, open_orders=waiting, preview=preview_body(value=3000.5))
    assert over["result"] == "needs_owner" and over["flags"] == ["capital"] and over["code"] == "capital"
    assert over["numbers"]["committed"] == 10000.5 and over["numbers"]["capital"] == 10000.0
    assert "10,000.50" in over["why"] and "10,000.00" in over["why"]


def test_the_order_without_a_figure_from_schwab_is_valued_by_its_price_quantity_and_multiplier():
    option = {"orderType": "LIMIT", "session": "NORMAL", "duration": "DAY", "price": "2.50", "orderStrategyType": "SINGLE",
              "orderLegCollection": [{"instruction": "BUY_TO_OPEN", "quantity": 10,
                                      "instrument": {"symbol": "AAPL  261218C00250000", "assetType": "OPTION"}}]}
    no_value = preview_body(value=0.0)
    answer = good(order=option, preview=no_value, capital=2000.0)
    assert answer["numbers"]["order_value"] == 2500.0 and answer["flags"] == ["capital"]      # 2.50 * 10 * 100
    vertical = {"orderType": "NET_DEBIT", "session": "NORMAL", "duration": "DAY", "price": "1.20", "orderStrategyType": "SINGLE",
                "orderLegCollection": [{"instruction": "BUY_TO_OPEN", "quantity": 2,
                                        "instrument": {"symbol": "XYZ   261218C00045000", "assetType": "OPTION"}},
                                       {"instruction": "SELL_TO_OPEN", "quantity": 2,
                                        "instrument": {"symbol": "XYZ   261218C00050000", "assetType": "OPTION"}}]}
    assert good(order=vertical, preview=no_value)["numbers"]["order_value"] == 240.0           # the net price counts once per spread
    market = limit("BUY", 10, "10")
    del market["price"]
    market["orderType"] = "MARKET"
    answer = good(order=market, preview=no_value, positions=[position("AAPL", long=1, value=300.0)])
    assert answer["numbers"]["order_value"] == 3000.0                                          # the mark of the position, 300 each


@pytest.mark.parametrize("closing", [
    limit("SELL", 100),                                                                         # a sale of a long
    limit("BUY", 40, symbol="TSLA"),                                                            # a purchase that covers a short
    limit("BUY_TO_COVER", 10, symbol="TSLA"),
    {**limit("SELL_TO_CLOSE", 1, symbol="XYZ   261218C00045000")},
    limit("BUY", 5, symbol="NVDA", **{}) | {"orderLegCollection": [{**eq("BUY", 5, "NVDA"), "positionEffect": "CLOSING"}]},
    limit("SELL", 100) | {"orderLegCollection": [{"instruction": "SELL", "quantityType": "ALL_SHARES",
                                                  "instrument": {"symbol": "AAPL", "assetType": "EQUITY"}}]},
])
def test_closing_and_reducing_orders_leave_on_their_own_over_the_capital(closing):
    held = [position("AAPL", long=100, value=60000.0), position("TSLA", short=50, value=-9000.0)]
    answer = good(order=closing, positions=held, preview=preview_body(value=50000.0))
    assert answer["result"] == "auto" and answer["flags"] == [], closing
    assert rules.order_facts(closing, held)["closing"] is True


@pytest.mark.parametrize("opening", [
    limit("SELL", 101),                                  # 100 closes a long, the extra opens a short
    limit("BUY", 1, symbol="NVDA"),
    limit("BUY", 60, symbol="TSLA"),                    # covers 50 and opens 10
    limit("SELL_SHORT", 1),
    limit("BUY_TO_OPEN", 1),
])
def test_an_order_that_opens_or_increases_asks_the_owner_when_the_desk_is_over_its_capital(opening):
    held = [position("AAPL", long=100, value=60000.0), position("TSLA", short=50, value=-9000.0)]
    answer = good(order=opening, positions=held, preview=preview_body(value=100.0))
    assert answer["result"] == "needs_owner" and answer["flags"] == ["capital"], opening


def test_a_bracket_is_an_opening_order_and_a_pair_of_exits_is_a_closing_one():
    entry = {"orderType": "LIMIT", "session": "NORMAL", "duration": "DAY", "price": "40.00", "orderStrategyType": "TRIGGER",
             "orderLegCollection": [eq("BUY", 10)], "childOrderStrategies": [{"orderStrategyType": "OCO", "childOrderStrategies": [
                 limit("SELL", 10, "45.00"), limit("SELL", 10, "38.00")]}]}
    held = [position("AAPL", long=0, value=0.0)]
    assert rules.order_facts(entry, held)["closing"] is False
    exits = {"orderStrategyType": "OCO", "childOrderStrategies": [limit("SELL", 10, "45.00"), limit("SELL", 10, "38.00")]}
    assert rules.order_facts(exits, [position("AAPL", long=10, value=400.0)])["closing"] is True
    assert rules.order_facts(exits, held)["closing"] is False


def test_a_cancellation_leaves_on_its_own_but_the_switch_and_the_desk_still_apply():
    assert rules.evaluate(action="cancel", orders_enabled=True, desk_status="active", account_ok=True,
                          capital=1.0, positions=[position(value=1e9)])["result"] == "auto"
    assert rules.evaluate(action="cancel", orders_enabled=False, desk_status="active", account_ok=True)["code"] == "orders_off"


# ------------------------------------------------------------------------------------------------- the optional limits

HELD = [position("AAPL", long=10, value=1000.0, day=-400.0)]


def test_every_limit_is_off_by_default_and_the_values_that_mean_off_are_off():
    big = limit("BUY", 1, "10")
    assert good(limits={}, preview=preview_body(value=9999.0), order=big, positions=HELD)["result"] in ("auto", "needs_owner")
    only_off = {"assets_only": False, "options_allowed": True, "short_allowed": True, "extended_hours_allowed": True}
    short_option = {**limit("SELL_TO_OPEN", 1, symbol="ZZZZ  261218C00010000"), "session": "SEAMLESS"}
    short_option["orderLegCollection"][0]["instrument"]["assetType"] = "OPTION"
    assert good(limits=only_off, order=short_option, preview=preview_body(value=100.0), assets=["AAPL"])["result"] == "auto"


def test_max_order_pct_on_and_off():
    over = good(limits={"max_order_pct": 5}, preview=preview_body(value=501.0))            # 5% of 10,000 is 500
    assert over["flags"] == ["max_order_pct"] and over["result"] == "needs_owner" and "5%" in over["why"]
    assert good(limits={"max_order_pct": 5}, preview=preview_body(value=500.0))["result"] == "auto"
    assert good(limits={}, preview=preview_body(value=501.0))["result"] == "auto"


def test_max_daily_loss_pct_stops_the_orders_that_open_but_not_the_ones_that_close():
    held = [position("AAPL", long=10, value=1000.0, day=-300.0), position("MSFT", long=1, value=100.0, day=-250.0)]
    on = good(limits={"max_daily_loss_pct": 5}, positions=held)                              # -550 is below -500
    assert on["flags"] == ["max_daily_loss_pct"] and on["numbers"]["day_pl"] == -550.0
    assert good(limits={"max_daily_loss_pct": 6}, positions=held)["result"] == "auto"
    assert good(limits={}, positions=held)["result"] == "auto"
    assert good(limits={"max_daily_loss_pct": 5}, positions=held, order=limit("SELL", 10))["result"] == "auto"


def test_assets_only_counts_the_symbols_of_the_desk_and_the_underlying_of_its_options():
    on = {"assets_only": True}
    assert good(limits=on, assets=["AAPL"])["result"] == "auto"
    assert good(limits=on, assets=["aapl"])["result"] == "auto"
    out = good(limits=on, assets=["MSFT"])
    assert out["flags"] == ["assets_only"] and "AAPL" in out["why"]
    occ = {"orderType": "LIMIT", "session": "NORMAL", "duration": "DAY", "price": "2.50", "orderStrategyType": "SINGLE",
           "orderLegCollection": [{"instruction": "BUY_TO_OPEN", "quantity": 1,
                                   "instrument": {"symbol": "AAPL  261218C00250000", "assetType": "OPTION"}}]}
    assert good(limits=on, assets=["AAPL"], order=occ)["result"] == "auto"                   # its option has AAPL as underlying
    assert good(limits=on, assets=["MSFT"], order=occ)["flags"] == ["assets_only"]
    assert good(limits={}, assets=["MSFT"])["result"] == "auto"


def test_options_allowed_short_allowed_and_extended_hours_are_on_only_when_false():
    option = {**limit("BUY_TO_OPEN", 1, symbol="AAPL  261218C00250000")}
    option["orderLegCollection"][0]["instrument"]["assetType"] = "OPTION"
    assert good(limits={"options_allowed": False}, order=option)["flags"] == ["options_allowed"]
    assert good(limits={"options_allowed": True}, order=option)["result"] == "auto"
    assert good(limits={"options_allowed": False})["result"] == "auto"                       # a stock order is not an option
    for short in (limit("SELL_SHORT", 1), limit("SELL", 5), limit("SELL_TO_OPEN", 1, symbol="AAPL  261218P00100000")):
        assert good(limits={"short_allowed": False}, order=short)["flags"] == ["short_allowed"], short
        assert good(limits={"short_allowed": True}, order=short)["result"] == "auto"
    assert good(limits={"short_allowed": False}, order=limit("SELL", 5), positions=[position(long=5, value=50.0)])["result"] == "auto"
    extended = {**limit(), "session": "SEAMLESS"}
    assert good(limits={"extended_hours_allowed": False}, order=extended)["flags"] == ["extended_hours_allowed"]
    assert good(limits={"extended_hours_allowed": True}, order=extended)["result"] == "auto"
    assert good(limits={"extended_hours_allowed": False})["result"] == "auto"                # NORMAL is the regular session


def test_max_orders_per_day_asks_for_the_order_after_the_limit():
    assert good(limits={"max_orders_per_day": 3}, orders_today=2)["result"] == "auto"
    out = good(limits={"max_orders_per_day": 3}, orders_today=3)
    assert out["flags"] == ["max_orders_per_day"] and "3" in out["why"]
    assert good(limits={}, orders_today=99)["result"] == "auto"


def test_a_closing_order_is_exempt_from_every_optional_limit():
    limits = {"max_order_pct": 1, "max_daily_loss_pct": 1, "assets_only": True, "options_allowed": False, "short_allowed": False,
              "extended_hours_allowed": False, "max_orders_per_day": 1}
    out = good(limits=limits, order={**limit("SELL", 10), "session": "SEAMLESS"}, positions=[position(long=10, value=500.0, day=-900.0)],
               assets=["MSFT"], orders_today=50, preview=preview_body(value=5000.0))
    assert out["result"] == "auto"


def test_several_conditions_come_together_in_one_reason():
    out = good(limits={"max_order_pct": 1, "options_allowed": False}, preview=preview_body(value=2000.0, warns=["Heads up"]))
    assert out["result"] == "needs_owner" and out["flags"] == ["review", "max_order_pct"] and out["code"] == "review"
    assert "Heads up" in out["why"] and "1%" in out["why"]


def test_validate_limits_checks_everything_merges_and_removes_with_null():
    assert rules.validate_limits({"max_order_pct": 5, "assets_only": True}) == {"max_order_pct": 5.0, "assets_only": True}
    merged = rules.validate_limits({"max_order_pct": None, "short_allowed": False}, {"max_order_pct": 5.0, "assets_only": True})
    assert merged == {"assets_only": True, "short_allowed": False}
    for bad in ({"max_order_pct": 0}, {"max_order_pct": -1}, {"max_order_pct": True}, {"max_order_pct": "5"}, {"max_daily_loss_pct": 101},
                {"max_orders_per_day": 0}, {"max_orders_per_day": 1.5}, {"max_orders_per_day": True}, {"assets_only": "yes"},
                {"options_allowed": 1}, {"nonsense": 1}, "x", None, [1]):
        with pytest.raises(ValueError):
            rules.validate_limits(bad)


# ========================================================================================================= the service

def test_a_desk_with_the_switch_off_answers_orders_off_and_nothing_is_called(ready):
    service.set_orders_enabled(ready.broker_id, False, db=ready.db)
    with pytest.raises(BrokerError) as refused:                                                   # criterion 23
        service.place(ready.desk_id, limit(), "test", AGENT, db=ready.db)
    assert refused.value.code == "orders_off" and refused.value.message == t("broker.err.orders_off") and refused.value.status == 409
    assert calls(ready.fake) == []
    row = rows(ready)[0]
    assert (row.action, row.rule_result, row.rule_why, row.http_status, row.broker_order_id) == (
        "place", "refused", t("broker.err.orders_off"), None, None)


def test_a_preview_still_works_with_the_switch_off_and_says_what_the_rule_would_do(ready):
    service.set_orders_enabled(ready.broker_id, False, db=ready.db)
    answer = service.preview(ready.desk_id, limit(), AGENT, db=ready.db)
    assert answer["status"] == "previewed" and answer["rule"]["code"] == "orders_off"
    assert [c.path for c in calls(ready.fake, "POST")] == [PREVIEW_PATH]


@pytest.mark.parametrize("status", ["paused", "archived"])
def test_a_paused_or_archived_desk_sends_nothing(ready, status):
    set_status(ready, status)
    for call in (lambda: service.place(ready.desk_id, limit(), "t", AGENT, db=ready.db),
                 lambda: service.cancel(ready.desk_id, 5, "t", AGENT, db=ready.db)):
        with pytest.raises(BrokerError) as refused:
            call()
        assert refused.value.code == "desk_inactive"
    assert calls(ready.fake) == []


def test_a_desk_without_an_account_or_with_an_account_that_does_not_trade_or_is_archived_is_refused(desks_db, fake, connections, ready):
    from desks_support import make_desk

    bare = make_desk(ready.db, name="Sin cuenta")
    with pytest.raises(BrokerError) as refused:
        service.place(bare["id"], limit(), "t", AGENT, db=ready.db)
    assert refused.value.code == "no_trading_account"
    internal = service.create_account(1, "Interna", 5000, db=ready.db)                            # the internal broker takes no orders
    other = make_desk(ready.db, name="Interna")
    service.assign_account(other["id"], internal["id"], db=ready.db)
    with pytest.raises(BrokerError) as refused:
        service.place(other["id"], limit(), "t", AGENT, db=ready.db)
    assert refused.value.code == "no_trading_account"
    with ready.db.write() as u:
        u.broker.archive_account(ready.account_id)
    with pytest.raises(BrokerError) as refused:
        service.place(ready.desk_id, limit(), "t", AGENT, db=ready.db)
    assert refused.value.code == "no_trading_account"
    assert calls(ready.fake) == []
    with pytest.raises(BrokerError) as missing:
        service.place(999, limit(), "t", AGENT, db=ready.db)
    assert missing.value.code == "desk_not_found" and missing.value.status == 404


def test_rejects_of_schwab_refuse_the_order_with_its_reason_and_record_the_preview(ready):
    ready.fake.on("POST", PREVIEW_PATH, preview_body(rejects=["Insufficient buying power"]))
    with pytest.raises(BrokerError) as refused:
        service.place(ready.desk_id, limit(), "test", AGENT, db=ready.db)
    assert refused.value.code == "rejected" and refused.value.status == 422 and "Insufficient buying power" in refused.value.message
    assert sent(ready.fake) == []                                                                       # only the preview went
    row = rows(ready)[0]
    assert row.rule_result == "refused" and "Insufficient buying power" in row.rule_why
    assert row.response_json["preview"]["orderValidationResult"]["rejects"][0]["message"] == "Insufficient buying power"


def test_a_small_limit_order_within_the_capital_leaves_on_its_own(ready):                         # criterion 24
    answer = service.place(ready.desk_id, limit("BUY", 5, "10.00"), "start small", AGENT, db=ready.db)
    assert answer["status"] == "sent" and answer["approved_by"] == "auto"
    assert rows(ready)[0].reason == "start small"


def test_a_sale_that_closes_a_position_leaves_on_its_own_over_the_capital(ready):                  # criterion 27
    from broker_orders_support import preview_body as pb

    set_capital(ready, 1000)
    ready.fake.on("GET", f"{ACCOUNT_PATH}", account_body([pos("AAPL", long=100, value=50000.0, day=100.0)]))
    ready.fake.on("POST", PREVIEW_PATH, pb(value=50000.0))
    answer = service.place(ready.desk_id, limit("SELL", 100, "500.00"), "take profit", AGENT, db=ready.db)
    assert answer["status"] == "sent" and answer["rule"]["result"] == "auto"
    ready.fake.on("GET", f"{ORDERS}/555", order_body(555))
    ready.fake.on("DELETE", f"{ORDERS}/555", (200, None))
    assert service.cancel(ready.desk_id, 555, "cancel it", AGENT, db=ready.db)["status"] == "canceled"


ACCOUNT_PATH = ORDERS.rsplit("/orders", 1)[0]


def test_an_order_over_the_capital_waits_for_the_owner_and_announces_it(ready):                    # criterion 26
    set_capital(ready, 1000)
    link_telegram(ready.db)
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=1500.0))
    order = limit("BUY", 150, "10.00")
    answer = service.place(ready.desk_id, order, "I like the setup", AGENT, db=ready.db)

    assert answer["status"] == "needs_owner" and isinstance(answer["decision_id"], int) and "1,500.00" in answer["why"]
    assert answer["expires_in_minutes"] == 30
    assert sent(ready.fake) == []                                                                     # nothing was sent
    with ready.db.read() as u:
        decision = u.decisions.get(answer["decision_id"])
        pending = u.broker.pending_order_records(ready.desk_id)
    assert decision.status == "proposed" and decision.proposed_by == "desk-chief" and decision.project_id == ready.project_id
    assert "I like the setup" in decision.proposal and "1,500.00" in decision.proposal and "BUY 150 AAPL" in decision.topic
    assert json.loads(decision.context) == order
    assert len(pending) == 1 and pending[0].decision_id == decision.id and pending[0].rule_result == "needs_owner"
    assert pending[0].request_json == order and pending[0].approved_by is None
    texts = outbox(ready.db)
    assert len(texts) == 1 and "1,500.00" in texts[0] and ready.db and "Futuros NQ" in texts[0]
    with ready.db.read() as u:
        assert u.journal.list(project_id=ready.project_id)[0].outcome == "info"


def test_approving_previews_again_with_the_prices_of_now_and_sends_as_the_owner(ready):            # criterion 26
    set_capital(ready, 1000)
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=1500.0))
    held = service.place(ready.desk_id, limit("BUY", 150, "10.00"), "idea", AGENT, db=ready.db)
    previews_before = len([c for c in calls(ready.fake, "POST") if c.path == PREVIEW_PATH])
    answer = service.approve_pending(held["decision_id"], db=ready.db, who=OWNER)

    assert answer["status"] == "sent" and answer["order_id"] == 600000123 and answer["approved_by"] == "owner"
    assert len([c for c in calls(ready.fake, "POST") if c.path == PREVIEW_PATH]) == previews_before + 1      # previewed again
    assert [c.path for c in sent(ready.fake)][-1] == ORDERS
    log = rows(ready)
    assert [(r.rule_result, r.approved_by, r.decision_id, r.http_status) for r in reversed(log)] == [
        ("needs_owner", None, held["decision_id"], None), ("needs_owner", "owner", held["decision_id"], 201)]
    with ready.db.read() as u:
        decision = u.decisions.get(held["decision_id"])
        approval = u.approvals.latest_for("decision", decision.id)
        assert u.broker.pending_order_records(ready.desk_id) == []
    assert (decision.status, decision.decided_by_kind, decision.decided_by) == ("approved", "human", "owner")
    assert approval.verdict == "approved" and decision.approval_id == approval.id
    with pytest.raises(BrokerError) as twice:
        service.approve_pending(held["decision_id"], db=ready.db, who=OWNER)                                   # it is already answered
    assert twice.value.code == "decision_closed"


def test_an_approved_order_that_no_longer_passes_stays_approved_without_an_order_and_says_why(ready):
    set_capital(ready, 1000)
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=1500.0))
    held = service.place(ready.desk_id, limit("BUY", 150, "10.00"), "idea", AGENT, db=ready.db)
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=1500.0, rejects=["Market is closed"]))
    answer = service.approve_pending(held["decision_id"], db=ready.db, who=OWNER)
    assert answer["status"] == "not_sent" and "Market is closed" in answer["why"]
    assert [c for c in sent(ready.fake) if c.path == ORDERS] == []
    with ready.db.read() as u:
        assert u.decisions.get(held["decision_id"]).status == "approved"
    assert rows(ready)[0].rule_result == "refused" and rows(ready)[0].decision_id == held["decision_id"]


def test_an_approved_order_with_a_new_condition_the_owner_did_not_see_is_not_sent(ready):
    set_capital(ready, 1000)
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=1500.0))
    held = service.place(ready.desk_id, limit("BUY", 150, "10.00"), "idea", AGENT, db=ready.db)
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=1500.0, warns=["Halted"]))               # a new reason
    answer = service.approve_pending(held["decision_id"], db=ready.db, who=OWNER)
    assert answer["status"] == "not_sent" and "Halted" in answer["why"]
    assert [c for c in sent(ready.fake) if c.path == ORDERS] == []


def test_rejecting_sends_nothing_and_leaves_the_decision_rejected(ready):
    set_capital(ready, 1000)
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=1500.0))
    held = service.place(ready.desk_id, limit("BUY", 150, "10.00"), "idea", AGENT, db=ready.db)
    assert service.reject_pending(held["decision_id"], db=ready.db, who=OWNER) == {"status": "rejected", "decision_id": held["decision_id"]}
    assert [c for c in sent(ready.fake) if c.path == ORDERS] == []
    with ready.db.read() as u:
        assert u.decisions.get(held["decision_id"]).status == "rejected"
    assert rows(ready)[0].rule_result == "refused"
    with pytest.raises(BrokerError) as late:
        service.approve_pending(held["decision_id"], db=ready.db, who=OWNER)
    assert late.value.code == "decision_closed"


def test_a_decision_that_nobody_answers_expires_at_30_minutes_and_sends_nothing(ready, monkeypatch):
    set_capital(ready, 1000)
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=1500.0))
    first = service.place(ready.desk_id, limit("BUY", 150, "10.00"), "idea", AGENT, db=ready.db)
    second = service.place(ready.desk_id, limit("BUY", 151, "10.00"), "idea 2", AGENT, db=ready.db)
    start = dt.datetime.now(dt.UTC)
    monkeypatch.setattr(service, "_now", lambda: start + dt.timedelta(minutes=29, seconds=30))
    assert [p["decision_id"] for p in service.list_pending(ready.desk_id, db=ready.db)] == [first["decision_id"], second["decision_id"]]
    monkeypatch.setattr(service, "_now", lambda: start + dt.timedelta(minutes=31))
    with pytest.raises(BrokerError) as late:
        service.approve_pending(first["decision_id"], db=ready.db, who=OWNER)                                # approving late sends nothing
    assert late.value.code == "decision_expired" and late.value.status == 409 and "30" in late.value.message
    assert [c for c in sent(ready.fake) if c.path == ORDERS] == []
    with ready.db.read() as u:
        assert u.decisions.get(first["decision_id"]).status == "rejected"
        assert u.decisions.get(second["decision_id"]).status == "proposed"
    assert service.expire_pending(db=ready.db) == 1                                                # the sweep takes the other one
    assert service.expire_pending(db=ready.db) == 0
    with ready.db.read() as u:
        assert u.decisions.get(second["decision_id"]).status == "rejected"
        assert u.broker.pending_order_records(ready.desk_id) == []
    assert [r.rule_why for r in rows(ready)[:2]] == [t("broker.decision.expired", minutes="30")] * 2


def test_reading_the_log_and_the_decisions_sweeps_the_expired_ones(ready, monkeypatch):
    set_capital(ready, 1000)
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=1500.0))
    held = service.place(ready.desk_id, limit("BUY", 150, "10.00"), "idea", AGENT, db=ready.db)
    monkeypatch.setattr(service, "_now", lambda: dt.datetime.now(dt.UTC) + dt.timedelta(minutes=45))
    out = hokma_tools.call("decision_list", {}, hokma_tools.Caller({"HOKMA_AGENT": "desk-chief"}))
    assert out[1] is False and json.loads(out[0])[0]["status"] == "rejected"
    assert service.order_log(ready.desk_id, db=ready.db)["orders"][0]["rule_why"] == t("broker.decision.expired", minutes="30")
    assert held["decision_id"]


def test_an_order_that_needs_the_owner_goes_out_at_once_when_the_owner_places_it_in_the_app(ready):
    set_capital(ready, 1000)
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=1500.0))
    answer = service.place(ready.desk_id, limit("BUY", 150, "10.00"), "owner order", OWNER, db=ready.db)
    assert answer["status"] == "sent" and answer["approved_by"] == "owner"
    assert rows(ready)[0].decision_id is None


def test_a_refusal_is_not_softened_when_the_owner_places_it_in_the_app(ready):
    ready.fake.on("POST", PREVIEW_PATH, preview_body(rejects=["No"]))
    with pytest.raises(BrokerError):
        service.place(ready.desk_id, limit(), "owner order", OWNER, db=ready.db)


def test_a_limit_that_is_on_asks_the_owner_and_off_does_not(ready):
    assert service.set_limits(ready.desk_id, {"max_order_pct": 5}, db=ready.db) == {"max_order_pct": 5.0}
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=800.0))                                    # 8% of 10,000
    assert service.place(ready.desk_id, limit(), "t", AGENT, db=ready.db)["status"] == "needs_owner"
    service.set_limits(ready.desk_id, {"max_order_pct": None}, db=ready.db)
    assert service.get_limits(ready.desk_id, db=ready.db) == {}
    assert service.place(ready.desk_id, limit(), "t", AGENT, db=ready.db)["status"] == "sent"


def test_assets_only_uses_the_symbols_of_the_desk(ready):
    from hokma.desks import service as desks

    desks.set_assets(ready.desk_id, ["MSFT"], db=ready.db)
    service.set_limits(ready.desk_id, {"assets_only": True}, db=ready.db)
    assert service.place(ready.desk_id, limit(symbol="AAPL"), "t", AGENT, db=ready.db)["status"] == "needs_owner"
    assert service.place(ready.desk_id, limit(symbol="MSFT"), "t", AGENT, db=ready.db)["status"] == "sent"


def test_max_orders_per_day_counts_only_what_was_sent_today(ready, monkeypatch):
    service.set_limits(ready.desk_id, {"max_orders_per_day": 2}, db=ready.db)
    assert service.place(ready.desk_id, limit(), "1", AGENT, db=ready.db)["status"] == "sent"
    ready.fake.on("GET", f"{ORDERS}/555", order_body(555))
    created(ready.fake, 556, f"{ORDERS}/555", "PUT")
    assert service.replace(ready.desk_id, 555, limit(), "2", AGENT, db=ready.db)["status"] == "replaced"      # a replace counts
    assert service.place(ready.desk_id, limit(), "3", AGENT, db=ready.db)["status"] == "needs_owner"           # the third asks
    assert service.preview(ready.desk_id, limit(), AGENT, db=ready.db)["rule"]["flags"] == ["max_orders_per_day"]
    with ready.db.read() as u:
        assert u.broker.count_sent_orders(ready.desk_id, "2000-01-01 00:00:00") == 2
        assert u.broker.count_sent_orders(ready.desk_id, "2999-01-01 00:00:00") == 0


def test_the_limits_are_saved_per_desk_merge_validate_and_show_in_the_desk(ready):
    from hokma.broker.service import BrokerError as Err
    from hokma.desks import service as desks

    service.set_limits(ready.desk_id, {"short_allowed": False, "max_orders_per_day": 4}, db=ready.db)
    assert service.set_limits(ready.desk_id, {"max_orders_per_day": None, "assets_only": True}, db=ready.db) == {
        "short_allowed": False, "assets_only": True}
    assert desks.get_desk(ready.desk_id, ready.db)["limits"] == {"short_allowed": False, "assets_only": True}
    for bad in ({"max_order_pct": -2}, {"foo": 1}, "x"):
        with pytest.raises(Err) as refused:
            service.set_limits(ready.desk_id, bad, db=ready.db)
        assert refused.value.code == "bad_limits" and refused.value.status == 400
    assert service.get_limits(ready.desk_id, db=ready.db) == {"short_allowed": False, "assets_only": True}
    with pytest.raises(Err):
        service.get_limits(999, db=ready.db)


# ---------------------------------------------------------------------------------------- the two permission modes (28)

def test_in_the_ask_mode_every_write_asks_and_the_reads_do_not():                                   # criterion 28
    writes = ("order_place", "order_replace", "order_cancel")
    reads = ("order_preview", "order_status", "order_list", "order_log", "position_list")
    allowed = json.loads((hokma_tools_dir() / "templates" / "settings.base.json").read_text(encoding="utf-8"))["permissions"]["allow"]
    for name in writes:
        assert hokma_tools.TOOLS[name][4] is True and f"mcp__hokma__{name}" not in allowed and modes.asks("ask", name)
    for name in reads:
        assert hokma_tools.TOOLS[name][4] is False and f"mcp__hokma__{name}" in allowed
    assert not modes.asks("auto", "order_place")                  # "auto": the rules of the desk decide (a soft condition asks the owner)


def hokma_tools_dir():
    from pathlib import Path

    return Path(hokma_tools.__file__).parent
