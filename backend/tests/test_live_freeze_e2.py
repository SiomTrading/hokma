"""The freeze on the data of the broker, piece 3390e2 (SPEC-026 v2; tests A3, A4, A5, A7, A14, A15, A20, A21 and the plan 10.1).

What is here and was not before: the peak of the equity that survives a restart, the notice that tells the owner when the broker's number reaches the cap, the desk rule
that is refused in a shared account (and the one that already existed is told to move), the close that is confirmed by the BROKER and not by the book (A21), the closing
order that is never stopped by the freeze, and the reads that keep giving way to the orders.
The rest of A3 to A15 and A20 are in test_live_freeze.py, test_live_blockers.py, test_live_close_no_double.py and test_rule_modes.py; the table at the end of this file says where."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db  # noqa: F401  (fixtures)
from exec_support import NQ, SPY, proposal, ready  # noqa: F401  (fixtures)
from test_desk_rules import OWNER
from test_live_blockers import open_qty
from test_live_close_no_double import LateBroker, wired  # noqa: F401  (fixtures)
from test_live_freeze import code_of, check, feed, fill, quote, rule, second_desk, START  # noqa: F401  (fixtures)

from hokma.broker import account_rules, day_close, desk_rules, pnl
from hokma.broker.service import BrokerError
from hokma.exec import live, prefilter
from hokma.i18n import catalog, t, use_language

D = Decimal


# ---- the peak of the equity lives in the database ------------------------------------------------------------------------------------------

def test_the_peak_of_the_session_survives_a_restart_and_a_new_session_clears_it(ready, feed):
    feed.set(ready.account_id, now=D(105_000))                                                      # the highest equity the broker reported
    assert live.measure(ready.account_id, db=ready.db).drawdown == D(0)
    feed.set(ready.account_id, now=D(99_000))
    assert live.measure(ready.account_id, db=ready.db).drawdown == D(6_000)                         # 105000 - 99000
    with ready.db.read() as u:
        assert D(live._row(u, ready.account_id)["peak_equity"]) == D(105_000)                       # it was written when it went up
    live._peaks.clear()                                                                              # a restart of Hokma: nothing in memory
    assert live.measure(ready.account_id, db=ready.db).drawdown == D(6_000)                         # the peak came back from the database, not from the next reading
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1, hours=1)
    feed.set(ready.account_id, now=D(99_500))
    m = live.measure(ready.account_id, db=ready.db, now=tomorrow)                                    # the next session: the peak starts again
    assert m.drawdown == D(500)                                                                      # against the start of the new session (100000), not against the 105000 of yesterday
    with ready.db.read() as u:
        assert D(live._row(u, ready.account_id)["peak_equity"]) == D(100_000)


def test_a_lower_reading_never_lowers_the_saved_peak(ready, feed):
    feed.set(ready.account_id, now=D(103_000))
    live.measure(ready.account_id, db=ready.db)
    feed.set(ready.account_id, now=D(101_000))
    live.measure(ready.account_id, db=ready.db)
    with ready.db.read() as u:
        assert D(live._row(u, ready.account_id)["peak_equity"]) == D(103_000)


# ---- a rule in notice: the owner is told when the broker's number reaches the cap, and nothing closes ---------------------------------------

def _told(monkeypatch) -> list[str]:
    told: list[str] = []
    monkeypatch.setattr(live, "_notify", lambda database, text: told.append(text))
    monkeypatch.setattr(live, "notify_reached", lambda database, text: told.append(text) or True)
    return told


def test_a_notice_that_reaches_its_cap_tells_the_owner_once_in_that_reading_and_closes_nothing(ready, feed, monkeypatch):
    told = _told(monkeypatch)
    account_rules.set_rule(ready.desk_id, ready.account_id, "daily_loss", 500, OWNER, mode="notice", timeout_min=5, db=ready.db)
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "n1")
    mon = live.start(ready.db)
    try:
        feed.loss(ready.account_id, 499)
        live.on_reading(ready.account_id)
        assert told == []                                                                            # below the cap: nothing, there are no lines before it
        feed.loss(ready.account_id, 500)
        live.on_reading(ready.account_id)                                                           # the reading that crosses
        assert len(told) == 1 and "500.00" in told[0] and "daily_loss" in told[0]
        feed.loss(ready.account_id, 700)
        live.on_reading(ready.account_id)
        mon.tick()
        assert len(told) == 1                                                                        # once, not on every reading
        with ready.db.read() as u:
            assert not day_close.is_closed(u, ready.account_id)                                      # a notice closes nothing and blocks nothing by itself
        assert open_qty(ready) == {NQ: D(1)}
        feed.loss(ready.account_id, 100)
        live.on_reading(ready.account_id)
        feed.loss(ready.account_id, 600)
        live.on_reading(ready.account_id)
        assert len(told) == 1                                                                        # it fell and crossed again while the first notice still waits: that one is still the question
        with ready.db.write() as u:
            u.session.execute("UPDATE cap_notices SET expires_at = '2000-01-01 00:00:00'")          # nobody answered and its wait ended
        feed.loss(ready.account_id, 100)
        live.on_reading(ready.account_id)
        feed.loss(ready.account_id, 600)
        live.on_reading(ready.account_id)
        assert len(told) == 3                                                                        # one text for the expiry and one for the new notice
    finally:
        live.stop()


def test_the_new_texts_are_in_both_languages_with_the_same_placeholders():
    import re

    for key in ("live.notice.reached", "live.shared.rejected", "live.shared.move", "pnl.yielding", "live.close.closed"):
        es, en = catalog("es")[key], catalog("en")[key]
        assert es != en and set(re.findall(r"\{(\w+)\}", es)) == set(re.findall(r"\{(\w+)\}", en))


# ---- a rule of a desk in an account of several desks is refused when it is made ----------------------------------------------------------

def test_a_rule_of_one_desk_in_a_shared_account_is_refused_where_it_is_created_in_both_languages(ready):
    second_desk(ready)
    for lang in ("es", "en"):
        with use_language(lang):
            with pytest.raises(BrokerError) as e:
                account_rules.set_rule(ready.desk_id, ready.account_id, "daily_loss", 200, OWNER, mode="restriction", on_trip="block_entries", only_desk=True, db=ready.db)
        assert e.value.code == "shared_account" and e.value.text == catalog(lang)["live.shared.rejected"]
        with use_language(lang):
            with pytest.raises(BrokerError) as e:
                desk_rules.set_control(ready.desk_id, "daily_loss", 200, OWNER, mode="notice", db=ready.db)                # a notice is not measurable there either
        assert e.value.code == "shared_account"
    assert catalog("es")["live.shared.rejected"].startswith("Esta cuenta") and catalog("en")["live.shared.rejected"].startswith("This account")
    with ready.db.read() as u:
        assert u.session.query("SELECT id FROM account_rules") == [] and not (u.broker.get_limits(ready.desk_id) or {}).get("funding")      # nothing was saved


def test_the_agent_of_risk_cannot_even_propose_the_rule_of_a_desk_in_a_shared_account_but_can_propose_it_on_the_account(ready):
    second_desk(ready)
    with pytest.raises(BrokerError) as e:
        desk_rules.propose(ready.desk_id, "daily loss of 900 dollars", mode="restriction", on_trip="block_entries", db=ready.db)
    assert e.value.code == "shared_account"
    with pytest.raises(BrokerError) as e:
        desk_rules.propose(ready.desk_id, "daily loss of 900 dollars", mode="restriction", on_trip="block_entries", account_id=ready.account_id, only_desk=True, db=ready.db)
    assert e.value.code == "shared_account"
    item = desk_rules.propose(ready.desk_id, "daily loss of 900 dollars", mode="restriction", on_trip="block_entries", account_id=ready.account_id, db=ready.db)
    assert item["account_id"] == ready.account_id                                                    # the rule of the ACCOUNT is the way, and it is accepted


def test_the_rule_of_the_account_is_accepted_in_a_shared_account_and_the_desk_rule_of_an_exclusive_account_too(ready, feed):
    rule(ready, usd=300, only_desk=True)                                                             # exclusive account: fine
    second_desk(ready)
    account_rules.set_rule(ready.desk_id, ready.account_id, "daily_loss", 250, OWNER, mode="restriction", on_trip="block_entries", db=ready.db)    # of the account: fine
    with ready.db.read() as u:
        assert len(u.session.query("SELECT id FROM account_rules")) == 2


def test_a_desk_rule_that_already_exists_in_a_shared_account_stays_unmeasured_blocks_and_the_owner_is_told_once_to_move_it(ready, feed, monkeypatch):
    told = _told(monkeypatch)
    rule(ready, usd=200, only_desk=True)
    second_desk(ready)
    feed.loss(ready.account_id, 50)
    mon = live.start(ready.db)
    try:
        mon.evaluate(ready.account_id)
        mon.evaluate(ready.account_id)
        mon.tick()
    finally:
        live.stop()
    rid = account_rules.rule_id_of(ready.account_id, "daily_loss", ready.desk_id)
    sentences = {catalog(lang)["live.shared.move"].format(rule=rid, account=ready.account_id) for lang in ("es", "en")}
    moved = [x for x in told if x in sentences]
    assert len(moved) == 1                                                                              # told once, however many times the monitor looked
    assert code_of(check(ready)) == ("live_loss", "broker_data_missing")                             # it still blocks the openings, as before


# ---- A5: the number of the broker, with a position under water and nothing realized ------------------------------------------------------

def test_a5_a_position_under_water_by_the_broker_blocks_the_opening_with_the_value_and_the_hour_of_the_reading(ready, feed):
    rule(ready, usd=500)
    reading = pnl.BrokerPnl(ready.account_id, 98_000.0, 100_000.0, -2_000.0, (pnl.PositionPnl("/NQZ6", 1.0, -2_000.0, -2_000.0),),
                            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "schwab")
    feed.rows[ready.account_id] = reading                                                             # zero realized: the whole loss is open P&L the broker reports
    result = check(ready)
    assert code_of(result) == ("live_loss", "day_loss_reached")
    why = result.blocked_by.why
    assert "2,000.00" in why and "500.00" in why and why.count(":") >= 2                              # the value, the cap and the hour of the reading


# ---- the closing order is never stopped by the freeze: not without data, not over the cap --------------------------------------------------

def _closing(ready):
    return proposal(ready, side="sell", effect="close", qty="1")


def test_a_closing_order_goes_out_with_no_data_from_the_broker_and_with_the_day_loss_reached_while_an_opening_does_not(ready, feed):
    rule(ready, usd=500)
    held = (SimpleNamespace(contract_id=NQ, qty=1, symbol=NQ),)
    opening = prefilter.run(ready.desk_id, proposal(ready), db=ready.db, skip=("monitor",))
    closing = prefilter.run(ready.desk_id, _closing(ready), db=ready.db, skip=("monitor",), virtual=held)
    assert code_of(opening) == ("live_loss", "broker_data_missing")                                    # no reading: the rule cannot be checked
    assert closing.verdict == "PASS" and closing.closing and _live_loss(closing).code == "closing_exempt"
    feed.loss(ready.account_id, 600)                                                                  # now the broker says the cap was passed
    opening = prefilter.run(ready.desk_id, proposal(ready), db=ready.db, skip=("monitor",))
    closing = prefilter.run(ready.desk_id, _closing(ready), db=ready.db, skip=("monitor",), virtual=held)
    assert code_of(opening) == ("live_loss", "day_loss_reached")
    assert closing.verdict == "PASS" and closing.closing and _live_loss(closing).code == "closing_exempt"


def _live_loss(result):
    return next(r for r in result.results if r.guard == "live_loss")


# ---- A21: the broker fills the close only in part ------------------------------------------------------------------------------------------

class PartialBroker(LateBroker):
    """A broker that fills only `step` of a close each time (0: it never fills). The order stays working with what is left. It ignores reduce_only like Schwab."""

    def __init__(self, held, step) -> None:
        super().__init__(held)
        self.step = step

    def place_order(self, account_id, order):
        out = super().place_order(account_id, order)
        o = self.live[-1]
        part = min(D(str(o.qty)), D(self.step))
        if part > 0:
            leg = order["legs"][0]
            self.pos[o.contract_id] = self.pos.get(o.contract_id, D(0)) + (part if o.side == "buy" else -part)
            self.unbooked.append((o.contract_id, o.side, part))
            o.qty = float(D(str(o.qty)) - part)
            if o.qty <= 0:
                o.status = "FILLED"
                self.live.remove(o)
        return out


def test_a21_the_position_is_not_closed_until_the_broker_stops_reporting_it_and_the_retries_end_with_what_the_broker_holds(ready, wired, monkeypatch):
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 3, 21000, "a21")
    broker = wired({NQ: "3"})
    never = PartialBroker({NQ: "3"}, 0)
    broker.place_order, broker.fill_all = never.place_order, never.fill_all
    broker.step = 0
    told = _told(monkeypatch)
    text, remaining = live.close_positions(ready.db, ready.account_id, None, include_long_shares=True)
    assert remaining == [NQ] and t("live.close.closed", list=NQ) in text                                # an order was sent; the broker still holds 3: it is not closed
    mon = live.LiveMonitor(ready.db)
    mon.retry_close(ready.account_id, None, True)
    for _ in range(live.CLOSE_RETRIES + 2):
        mon._retry_closes()
    assert not mon._retries and len(told) == 1 and "3" in told[0] and NQ in told[0]                     # it tells what the broker reports open: 3 of NQ
    assert all(D(str(q)) >= 0 for q in broker.pos.values()) and broker.pos[NQ] == D(3)                  # and it never inverted the position


def test_a21_a_close_that_fills_a_part_each_time_ends_closed_when_the_broker_reports_nothing(ready, wired, monkeypatch):
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 3, 21000, "a21b")
    broker = wired({NQ: "3"})
    step = PartialBroker({NQ: "3"}, 1)
    step.pos = broker.pos                                                                               # one position, two ways to place
    step.live, step.unbooked, step.sent = broker.live, broker.unbooked, broker.sent
    broker.place_order = step.place_order
    told = _told(monkeypatch)
    text, remaining = live.close_positions(ready.db, ready.account_id, None, include_long_shares=True)
    assert remaining == [NQ]                                                                            # 1 of 3 filled: the broker still reports 2
    mon = live.LiveMonitor(ready.db)
    mon.retry_close(ready.account_id, None, True)
    for _ in range(live.CLOSE_RETRIES):
        mon._retry_closes()
        if not mon._retries:
            break
    assert not mon._retries and broker.pos.get(NQ, D(0)) == D(0)
    assert len(told) == 1 and told[0].startswith(t("live.close.retry_done", account=ready.account_id, text="")[:20])


# ---- where the other acceptance tests are -----------------------------------------------------------------------------------------------------
# A3  test_live_freeze.py::test_the_monitor_closes_the_day_in_the_same_event_of_the_reading_that_reaches_the_cap and ::test_the_close_happens_in_the_first_reading_at_or_over_the_cap...
# A4  test_live_freeze.py::test_block_and_flatten_closes_the_futures_and_does_not_sell_the_shares_held_long (and _all)
# A7  test_live_freeze.py::test_a7_the_close_is_lifted_alone_when_the_next_session_opens_and_the_start_is_new
# A14 test_live_freeze.py::test_a14_a_rule_of_the_account_closes_it_for_every_desk_and_leaves_their_other_accounts
# A15 test_live_freeze.py::test_a15_* (the (ii) one is made in two steps now: the rule exists first, then the account is shared)
# A20 test_rule_modes.py (lift by hand: confirm_required, then the event with who and when)
