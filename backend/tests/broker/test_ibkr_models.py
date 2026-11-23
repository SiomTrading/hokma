"""The typed readers of the IBKR answers and the local gateway client (SPEC-018 B5: OA-72). Recorded answers under `fixtures/`, no network: the
types parse them, a field nobody knows does not break the parse, and an account identifier never comes out of a model."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from broker import harnesses_connectors as hc

from hokma.broker.ibkr import http, models

ACCOUNT = hc.IBKR_SENTINEL
MASKER = models.Masker([ACCOUNT])
DOTS = "\u2022\u2022\u2022\u2022"


def test_the_account_list_gives_the_identifiers_to_the_connector_only():
    assert models.parse_account_ids(hc.fixture("ibkr_accounts.json")) == [ACCOUNT]
    assert models.parse_account_ids({"accounts": ["DUA0001", "DUA0002", "DUA0001"]}) == ["DUA0001", "DUA0002"]       # no repeats
    assert models.parse_account_ids("garbage") == [] and models.parse_account_ids(None) == []


def test_the_summary_reads_amounts_as_decimal_and_a_null_amount_is_none():
    summary = models.parse_summary(hc.fixture("ibkr_summary.json"), ACCOUNT, MASKER)
    assert (summary.cash, summary.equity, summary.buying_power, summary.currency) == (Decimal("12000.5"), Decimal("15500.25"), Decimal("48000"), "USD")
    assert (summary.account_ref, summary.mask) == ("A1", DOTS + "4455")
    empty = models.parse_summary({"availablefunds": {"amount": None, "isNull": True}}, ACCOUNT, MASKER)
    assert empty.cash is None and empty.equity is None
    assert models.parse_summary("not an object", ACCOUNT, MASKER).cash is None


def test_positions_parse_with_an_unknown_field_kept_and_the_account_masked():
    stock, option = (models.parse_position(p, MASKER) for p in hc.fixture("ibkr_positions.json"))
    assert (stock.symbol, stock.qty, stock.avg_price, stock.market_value, stock.unrealized_pnl) == ("AAPL", Decimal("10.0"), Decimal("150.0"), Decimal("1600.0"), Decimal("100.0"))
    assert stock.extra == {"newField": "x", "currency": "USD", "avgCost": 150.0, "mktPrice": 160.0, "realizedPnl": 0.0}     # what no field knows is kept
    assert (option.qty, option.multiplier, option.put_call, option.contract_symbol) == (Decimal("-2.0"), Decimal("100.0"), "C", "AAPL261218C00200000")
    assert (stock.account_ref, stock.mask) == ("A1", DOTS + "4455")
    assert ACCOUNT not in json.dumps([stock.__dict__ if hasattr(stock, "__dict__") else str(stock), str(option)], default=str)
    assert models.parse_position("junk", MASKER) is None and models.parse_position({}, MASKER).qty == 0


def test_orders_parse_with_the_status_words_of_the_sim_broker_and_the_identifier_masked_inside_texts():
    row = hc.fixture("ibkr_orders.json")["orders"][0]
    order = models.parse_order(row, MASKER)
    assert (order.order_id, order.conid, order.symbol, order.side, order.order_type) == ("987654321", 265598, "AAPL", "buy", "limit")
    assert (order.qty, order.filled, order.remaining, order.price, order.status, order.tif) == (Decimal("1.0"), Decimal("0.0"), Decimal("1.0"), Decimal("100.00"),
                                                                                              "WORKING", "GTC")
    assert order.extra["order_ref"] == "hk-ref " + DOTS + "4455" and "acct" not in order.extra
    assert ACCOUNT not in str(order)
    for raw, word in (("Filled", "FILLED"), ("Cancelled", "CANCELED"), ("Inactive", "REJECTED"), ("PreSubmitted", "WORKING"), ("Something New", "WORKING")):
        assert models.parse_order({**row, "status": raw}, MASKER).status == word
    stop = models.parse_order({**row, "origOrderType": "STP LMT", "auxPrice": "99.5"}, MASKER)
    assert (stop.order_type, stop.stop_price) == ("stop_limit", Decimal("99.5"))
    partial = models.parse_order({**row, "filledQuantity": 0.4}, MASKER)
    assert partial.partially_filled is True and models.parse_order(row, MASKER).partially_filled is False
    assert models.parse_order(None, MASKER) is None


def test_auth_status_and_the_replies_to_an_order():
    assert models.parse_auth(hc.fixture("ibkr_auth_status.json")).ready is True
    assert models.parse_auth(hc.fixture("ibkr_auth_status_out.json")).ready is False
    assert models.parse_auth("junk").ready is False
    placed = models.parse_reply(hc.fixture("ibkr_reply_placed.json"))
    assert (placed.order_id, placed.status, placed.question) == ("987654321", "Submitted", False)
    asked = models.parse_reply(hc.fixture("ibkr_reply_question.json"))
    assert asked.order_id is None and asked.question is True and "price constraint" in asked.messages[0]
    assert models.parse_reply(None).order_id is None and models.parse_reply([]).question is False


def test_public_drops_account_keys_and_masks_every_listed_identifier():
    out = models.public({"acct": ACCOUNT, "accountId": ACCOUNT, "title": f"Test {ACCOUNT}", "rows": [{"accountVan": ACCOUNT, "ok": 1}]}, MASKER)
    assert out == {"title": f"Test {DOTS}4455", "rows": [{"ok": 1}]}


def test_numbers_with_commas_and_currency_are_read_exactly():
    assert models.number("1,001.00 USD") == Decimal("1001.00") and models.number(12) == Decimal(12) and models.number(0.1) == Decimal("0.1")
    assert models.number("n/a") is None and models.number(None) is None and models.number(True) is None


# ------------------------------------------------------------------------------------------------------ the local gateway client

def _gateway(status=200, text="{}"):
    seen = []

    def transport(method, url, headers, body):
        seen.append((method, url, headers, body))
        return status, text

    return http.Gateway(transport=transport), seen


@pytest.mark.parametrize("base", ["https://evil.example/v1/api", "http://localhost:5000/v1/api", "https://localhost.evil.example:5000/v1/api",
                                  "https://user:pw@localhost:5000/v1/api", "ftp://localhost/x", "https://localhost:notaport/v1/api", "localhost"])
def test_only_a_gateway_on_this_machine_is_called(base):
    with pytest.raises(http.IbkrError) as error:
        http.Gateway(base)
    assert error.value.code == http.BAD_BASE


def test_a_local_gateway_is_accepted_and_a_request_is_built_from_a_checked_path():
    gateway, seen = _gateway(200, '{"a": 1}')
    assert http.Gateway("https://127.0.0.1:5000/v1/api/").base == "https://127.0.0.1:5000/v1/api"
    assert gateway.get("/portfolio/accounts", {"force": True, "skip": None, "n": 5}) == {"a": 1}
    assert seen[0][1] == "https://localhost:5000/v1/api/portfolio/accounts?force=true&n=5"
    gateway.post_json("/iserver/secdef/search", {"symbol": "AAPL"})
    assert seen[1][3] == b'{"symbol":"AAPL"}' and seen[1][2]["Content-Type"] == "application/json"


@pytest.mark.parametrize("path", ["portfolio", "/", "/a//b", "/a/../b", "/a/./b", "/a/b?x=1", "/a/b#f", "/a b/c", "/a/%2e%2e/b", "/a/b\n"])
def test_a_path_with_anything_but_plain_segments_never_reaches_the_network(path):
    gateway, seen = _gateway()
    with pytest.raises(http.IbkrError) as error:
        gateway.get(path)
    assert error.value.code == http.BAD_PATH and seen == []


def test_the_status_of_the_gateway_becomes_a_code_and_a_big_body_is_refused():
    for status, code in ((401, http.NOT_SIGNED_IN), (403, http.NOT_SIGNED_IN), (404, http.NOT_FOUND), (400, http.BAD_REQUEST), (500, http.UPSTREAM),
                         (503, http.UPSTREAM)):
        gateway, _ = _gateway(status, '{"error": "secret text"}')
        with pytest.raises(http.IbkrError) as error:
            gateway.get("/a/b")
        assert error.value.code == code and error.value.status == status and "secret" not in str(error.value)
    gateway, _ = _gateway(200, "<html>")
    with pytest.raises(http.IbkrError) as error:
        gateway.get("/a/b")
    assert error.value.code == http.UPSTREAM
    assert _gateway(204, "")[0].get("/a/b") is None
    with pytest.raises(http.IbkrError):
        _gateway()[0].post_json("/a/b", {"x": "y" * http.MAX_REQUEST_BYTES})
    with pytest.raises(http.IbkrError):
        _gateway()[0].request("PUT", "/a/b")


def test_a_gateway_that_does_not_answer_is_unavailable_and_logs_no_path(caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    gateway, _ = _gateway(200, "{}")
    gateway.get("/portfolio/DUSECRET99/summary")
    assert "DUSECRET99" not in caplog.text and "ibkr GET -> 200" in caplog.text
    gateway = http.Gateway(base="https://localhost:1/v1/api")          # nothing listens on port 1: the real transport reports it
    with pytest.raises(http.IbkrError) as error:
        gateway.get("/iserver/auth/status")
    assert error.value.code == http.UNAVAILABLE
