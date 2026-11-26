"""The reads of an account (the P&L of the broker) give way to the orders (SPEC-026 v2, 3390e1, Vera): their own cap, a reserve for the orders, and a 429
answered to a read pauses only the reads."""
from __future__ import annotations

import pytest

from hokma.kit.read_budget import ORDER_RESERVE, READS_PER_MINUTE, ReadYielded
from hokma.schwab import http
from hokma.schwab.http import RATE_LIMITED, SchwabError

READ = "/trader/v1/accounts"
ORDER = "/trader/v1/accounts/ABCDEFGH12345678/orders"


class Door:
    """Stands in for the network: counts the calls and answers by method."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.status = {"GET": 200, "POST": 201}

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        return self.status[method], "[]" if method == "GET" else "", None, None


@pytest.fixture
def door(monkeypatch):
    fake = Door()
    monkeypatch.setattr(http, "_send", fake)
    http.set_token_provider(lambda: "token")
    now = [0.0]
    http.reset_limits(clock=lambda: now[0])
    fake.now = now
    yield fake
    http.set_token_provider(None)
    http.reset_limits()


def read():
    with http.background():
        return http.request("GET", READ)


def close_order():
    return http.request("POST", ORDER, body=b"{}", consumer="hokma_broker", reply=True)


def test_a_burst_of_reads_stops_at_its_own_cap_and_the_close_still_goes_out(door):
    done = yielded = 0
    for _ in range(100):
        try:
            read()
            done += 1
        except ReadYielded:
            yielded += 1
    assert done == READS_PER_MINUTE and yielded == 100 - READS_PER_MINUTE
    assert close_order().status == 201
    assert len([c for c in door.calls if c[0] == "POST"]) == 1


def test_the_reads_leave_the_reserve_to_the_orders_even_with_their_own_budget_left(door):
    http.reset_limits(clock=lambda: door.now[0], limit=100)
    for _ in range(100 - ORDER_RESERVE):                        # the rest of the door (market data) fills it up to the reserve
        http.request("GET", READ)
    with pytest.raises(ReadYielded):
        read()
    for _ in range(ORDER_RESERVE):
        assert close_order().status == 201                      # the whole reserve is there for the orders
    with pytest.raises(SchwabError) as caught:
        close_order()
    assert caught.value.code == RATE_LIMITED                    # only the door's own cap stops an order


def test_a_429_answered_to_a_read_pauses_only_the_reads(door):
    door.status["GET"] = 429
    with pytest.raises(SchwabError) as caught:
        read()
    assert caught.value.code == RATE_LIMITED
    door.status["GET"] = 200
    with pytest.raises(ReadYielded):
        read()                                                   # paused, and it makes no call
    assert [c[0] for c in door.calls] == ["GET"]
    assert close_order().status == 201                           # the order is not paused
    door.now[0] += 61
    assert read() == []                                          # the reads come back after the pause


def test_a_429_answered_to_an_order_pauses_the_door_and_the_reads_give_way(door):
    door.status["POST"] = 429
    with pytest.raises(SchwabError):
        close_order()
    with pytest.raises(ReadYielded):
        read()
    with pytest.raises(SchwabError) as caught:
        http.request("GET", READ)                                # not a read of an account: the door is paused for it
    assert caught.value.code == RATE_LIMITED
