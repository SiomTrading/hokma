"""The close never repeats a close (SPEC-026, 3390c2, review L1): a broker that fills late, one contract that closes while another fails, a broker that ignores
reduce_only (Schwab) and the retries that survive a restart."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db  # noqa: F401  (fixtures)
from exec_support import NQ, SPY, ready  # noqa: F401  (fixtures)
from test_live_blockers import open_qty
from test_live_freeze import fill

from hokma.broker import day_close
from hokma.exec import live, submit
from hokma.exec.proposal import OrderProposal
from hokma.i18n import t

D = Decimal


class LateBroker:
    """A broker like Schwab: it accepts an order and leaves it WORKING, it fills it later, and it ignores `reduce_only` (it would open the other side)."""
    kind = "sim"
    capabilities = SimpleNamespace(positions=True, orders=True)

    def __init__(self, held: dict[str, str], fills_on_cancel: bool = False) -> None:
        self.pos = {c: D(q) for c, q in held.items()}
        self.live: list = []
        self.sent: list[dict] = []
        self.unbooked: list[tuple[str, str, Decimal]] = []
        self.fills_on_cancel = fills_on_cancel

    def positions(self, account_id):
        return [SimpleNamespace(contract_id=c, qty=float(q), symbol=c) for c, q in self.pos.items() if q != 0]

    def orders(self, account_id, *a, **k):
        return list(self.live)

    def place_order(self, account_id, order):
        self.sent.append(order)
        leg = order["legs"][0]
        o = SimpleNamespace(order_id=str(len(self.sent)), contract_id=leg["contract_id"], side=leg["side"], qty=float(leg["qty"]), status="WORKING",
                            filled_qty=0.0, remaining_qty=None, legs=())
        self.live.append(o)
        return {"order_id": o.order_id, "status": "WORKING"}

    def _fill(self, o) -> None:
        qty = D(str(o.qty))
        self.pos[o.contract_id] = self.pos.get(o.contract_id, D(0)) + (qty if o.side == "buy" else -qty)      # no reduce_only: a double close opens the other side
        self.unbooked.append((o.contract_id, o.side, qty))
        o.status = "FILLED"
        self.live.remove(o)

    def fill_all(self) -> None:
        for o in list(self.live):
            self._fill(o)

    def cancel_order(self, account_id, order_id):
        for o in list(self.live):
            if o.order_id == order_id:
                if self.fills_on_cancel:
                    self._fill(o)
                else:
                    o.status = "CANCELED"
                    self.live.remove(o)
        return {"status": "CANCELED"}


@pytest.fixture
def wired(ready, monkeypatch):
    def make(held, *, fills_on_cancel=False, book_pull=True):
        broker = LateBroker(held, fills_on_cancel)

        @contextmanager
        def open_broker(database, desk_id, account_id):
            yield broker, SimpleNamespace(id=ready.account_id)

        def pull(database, account_id):
            while book_pull and broker.unbooked:
                cid, side, qty = broker.unbooked.pop(0)
                fill(ready, ready.account_id, ready.desk_id, cid, side, qty, 21000 if cid == NQ else 600, f"late{len(broker.sent)}{cid}", mult=20 if cid == NQ else 1)

        monkeypatch.setattr(submit, "open_broker", open_broker)
        monkeypatch.setattr(live, "pull_fills", pull)
        monkeypatch.setattr(live, "_notify", lambda database, text: None)
        return broker
    return make


def _monitor(ready, include_long=True):
    mon = live.LiveMonitor(ready.db)
    mon.retry_close(ready.account_id, None, include_long)
    return mon


def test_l1_a_close_that_fills_late_is_not_sent_again_and_nothing_is_inverted(ready, wired):
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "l1a")
    broker = wired({NQ: "1"})
    text, remaining = live.close_positions(ready.db, ready.account_id, None, include_long_shares=True)
    assert len(broker.sent) == 1 and remaining == [NQ]                      # the broker accepted and has not filled: it is still open
    broker.fill_all()                                                        # the fill arrives between two ticks
    mon = _monitor(ready)
    mon._retry_closes()
    assert len(broker.sent) == 1 and broker.pos[NQ] == 0 and open_qty(ready) == {} and not mon._retries


def test_l1_a_fill_the_book_has_not_seen_yet_is_cut_by_what_the_broker_holds(ready, wired):
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "l1b")
    broker = wired({NQ: "1"}, book_pull=False)                               # the stream lags: the book still shows the long
    live.close_positions(ready.db, ready.account_id, None, include_long_shares=True)
    broker.fill_all()
    assert open_qty(ready) == {NQ: D(1)} and broker.pos[NQ] == 0
    live.close_positions(ready.db, ready.account_id, None, include_long_shares=True)
    assert len(broker.sent) == 1 and broker.pos[NQ] == 0                    # the broker is flat: the second close is cut to zero, not sent


def test_l1_the_cancel_of_the_retry_that_finds_the_close_filled_does_not_send_another(ready, wired):
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "l1c")
    broker = wired({NQ: "1"}, fills_on_cancel=True)
    live.close_positions(ready.db, ready.account_id, None, include_long_shares=True)
    live.close_positions(ready.db, ready.account_id, None, include_long_shares=True)       # the cancel of the working close fills it instead
    assert len(broker.sent) == 1 and broker.pos[NQ] == 0
    live.close_positions(ready.db, ready.account_id, None, include_long_shares=True)       # the next pass pulls the fill first and finds nothing to do
    assert len(broker.sent) == 1 and open_qty(ready) == {}


def test_l1_one_contract_closes_and_the_other_fails_the_closed_one_is_not_sent_again(ready, wired, monkeypatch):
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "l1d")
    fill(ready, ready.account_id, ready.desk_id, SPY, "buy", 10, 600, "l1e", mult=1)
    broker = wired({NQ: "1", SPY: "10"})
    real, broken = submit.dispatch, {"on": True}

    def flaky(b, account_id, action, order=None, order_id=None, *, owner=None):
        if broken["on"] and action == "place" and "SPY" in repr(order):
            raise RuntimeError("the broker did not answer")
        return real(b, account_id, action, order, order_id, owner=owner)

    monkeypatch.setattr(submit, "dispatch", flaky)
    text, remaining = live.close_positions(ready.db, ready.account_id, None, include_long_shares=True)
    broker.fill_all()                                                        # the NQ close fills late
    broken["on"] = False
    mon = _monitor(ready)
    mon._retry_closes()
    broker.fill_all()
    mon._retry_closes()
    sent = [o["legs"][0]["contract_id"] for o in broker.sent]
    assert sent.count(NQ) == 1 and sent.count(SPY) == 1 and broker.pos[NQ] == 0 and broker.pos[SPY] == 0 and open_qty(ready) == {}
    assert t("live.close.closed", list=NQ) in text and t("live.close.failed", list=SPY) in text


def test_l1_a_broker_that_ignores_reduce_only_gets_a_close_cut_to_what_it_holds(ready, wired):
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 3, 21000, "l1f")           # the book is stale: it says 3
    fill(ready, ready.account_id, ready.desk_id, SPY, "buy", 10, 600, "l1g", mult=1)   # and the broker is short of it: never a sell against that
    broker = wired({NQ: "1", SPY: "-4"})
    live.close_positions(ready.db, ready.account_id, None, include_long_shares=True)
    sent = {o["legs"][0]["contract_id"]: (o["legs"][0]["side"], o["legs"][0]["qty"]) for o in broker.sent}
    assert sent == {NQ: ("sell", "1")}                                       # cut to the 1 the broker holds; nothing on the side of the short


def test_l1_the_other_routes_of_close_cut_net_of_the_live_orders(ready):
    """flatten, the killswitch and the approvals go through the prefilter: a reduce_only leg is cut to the position NET of the closing orders that are live."""
    from hokma.exec import prefilter
    from hokma.exec.proposal import Leg

    p = OrderProposal(1, "DT", "rule", (Leg(NQ, "sell", "close", D(1)),), "market", reduce_only=True)
    held = (SimpleNamespace(contract_id=NQ, qty=1),)
    working = SimpleNamespace(contract_id=NQ, side="sell", qty=1.0, status="WORKING", filled_qty=0.0, remaining_qty=None, legs=())
    assert prefilter.clip_reduce_only(p, held, ())[0] is not None
    reduced, cuts = prefilter.clip_reduce_only(p, held, (working,))
    assert reduced is None                                                   # a second close of the same unit would open the short


def test_e_the_retries_of_a_closed_day_come_back_after_a_restart(ready, monkeypatch):
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "e1")
    day_close.close_day(ready.account_id, "daily_loss", 900, "block_and_flatten", ready.db)
    told = []
    monkeypatch.setattr(live, "_notify", lambda database, text: told.append(text))
    mon = live.LiveMonitor(ready.db)                                          # a new process: nothing in memory
    assert mon._retries == {} and mon.resume_closes() == [(ready.account_id, None)]
    mon._retry_closes()
    assert open_qty(ready) == {} and not mon._retries and len(told) == 1
    assert mon.resume_closes() == []                                          # nothing open, nothing to resume


def test_e_a_closed_day_that_only_blocks_entries_resumes_nothing(ready):
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "e2")
    day_close.close_day(ready.account_id, "daily_loss", 900, "block_entries", ready.db)
    assert live.LiveMonitor(ready.db).resume_closes() == []


def _age_close(ready, closed_at):
    with ready.db.write() as u:
        u.session.execute("UPDATE account_day_state SET closed_at = :t WHERE account_id = :a", {"t": closed_at, "a": ready.account_id})


def test_r1_a_close_of_a_previous_session_is_not_resumed_and_the_account_reopens(ready, monkeypatch):
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "r1")
    day_close.close_day(ready.account_id, "daily_loss", 900, "block_and_flatten", ready.db)
    _age_close(ready, "2000-01-03 12:00:00")                                  # closed in a session long gone
    monkeypatch.setattr(live, "_notify", lambda database, text: None)
    mon = live.LiveMonitor(ready.db)
    assert mon.resume_closes() == [] and mon._retries == {}                   # today's position is not flattened by yesterday's close
    assert open_qty(ready) == {NQ: D(1)}
    live._ensure_session(ready.db, ready.account_id, datetime.now(timezone.utc))      # what the tick does: the new session lifts the close
    with ready.db.read() as u:
        assert not day_close.is_closed(u, ready.account_id)
    assert open_qty(ready) == {NQ: D(1)}


def test_r2_a_close_of_the_current_session_is_resumed_after_a_restart(ready, monkeypatch):
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "r2")
    day_close.close_day(ready.account_id, "daily_loss", 900, "block_and_flatten", ready.db)
    monkeypatch.setattr(live, "_notify", lambda database, text: None)
    assert live.LiveMonitor(ready.db).resume_closes() == [(ready.account_id, None)]


def test_r3_a_stop_of_a_desk_that_was_lifted_or_expired_is_not_resumed(ready):
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "r3")
    for lifted, expires in (("2026-01-01 00:00:00", "2999-01-01 00:00:00"), (None, "2000-01-01 00:00:00")):
        with ready.db.write() as u:
            u.session.execute("DELETE FROM desk_day_stops")
            u.session.execute("INSERT INTO desk_day_stops (desk_id, account_id, rule_id, loss, on_trip, closed_at, lifted_at, expires_at) "
                              "VALUES (:d, 0, 'r', '900', 'block_and_flatten', '2026-01-01 00:00:00', :l, :e)", {"d": ready.desk_id, "l": lifted, "e": expires})
        assert live.LiveMonitor(ready.db).resume_closes() == []


def test_b1_the_close_holds_the_account_lock_while_it_reads_the_broker(ready, wired):
    """Vera B1: the read of positions and live orders happens under the lock of the account, so nothing else can move the position before the send."""
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "b1a")
    broker = wired({NQ: "1"})
    seen: list[bool] = []
    import threading
    lock = submit.account_lock(ready.account_id)

    def held_by_other_thread() -> bool:
        got: list[bool] = []
        def probe():
            ok = lock.acquire(blocking=False)
            got.append(ok)
            if ok:
                lock.release()

        th = threading.Thread(target=probe)
        th.start()
        th.join()
        return not got[0]

    real_orders, real_positions = broker.orders, broker.positions
    broker.orders = lambda *a, **k: (seen.append(held_by_other_thread()), real_orders(*a, **k))[1]
    broker.positions = lambda *a, **k: (seen.append(held_by_other_thread()), real_positions(*a, **k))[1]
    live.close_positions(ready.db, ready.account_id, None, include_long_shares=True)
    assert len(seen) >= 3 and seen[-3:] == [True, True, False]                # the two reads of _broker_room are under the lock; the last one only asks what is still open and sends nothing


def test_b1_another_route_that_closes_in_parallel_does_not_invert_the_position(ready, wired):
    """Vera B1: another route that flattens the account (3390b) only sells if no closing order is alive; it runs from _broker_room, after the close read the broker.
    With the lock held until the last send it waits, sees the live sell and does nothing; without it, it zeroes the position and the late fill leaves -1."""
    import inspect
    import threading
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "b1b")
    broker = wired({NQ: "1"})
    lock = submit.account_lock(ready.account_id)
    real_positions = broker.positions
    started, done = threading.Event(), threading.Event()

    def other_route():
        started.set()
        with lock:
            closing_live = any(o.side == "sell" for o in broker.live)
            if broker.pos[NQ] != 0 and not closing_live:
                broker.pos[NQ] = D(0)
        done.set()

    th = threading.Thread(target=other_route)

    def positions(account_id):
        out = real_positions(account_id)
        if any(fr.function == "_broker_room" for fr in inspect.stack()) and not started.is_set():       # not in the cancel phase
            th.start()
            started.wait(2)
            done.wait(0.5)
        return out

    broker.positions = positions
    live.close_positions(ready.db, ready.account_id, None, include_long_shares=True)
    th.join(2)
    assert started.is_set()
    broker.fill_all()
    assert broker.pos[NQ] == 0, broker.pos[NQ]