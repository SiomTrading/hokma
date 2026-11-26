"""The freeze of the loss of the day on what the BROKER reports (SPEC-026 v2, pieces 3390e and 3390c): the measure from the last reading (never from the book or
from prices), the door that blocks at the cap and not before, the data that is missing, the monitor that closes the day in the event of the reading that reaches
the cap, the session of the account, a shared account, the part of a desk, what the close does to long shares, the second line and the way back of the migrations.
The reading of the broker is a fake reader registered in `hokma.broker.pnl` (the real ones are piece 3390e1)."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from account_links_support import link_principal
from dbsupport import db, env, take_back_the_migrations_after_0033  # noqa: F401  (fixtures)
from desks_support import desks_db, make_desk  # noqa: F401  (fixtures)
from exec_support import NQ, SPY, proposal, ready  # noqa: F401  (fixtures)
from test_desk_rules import OWNER

from hokma.backoffice import book as book_module
from hokma.broker import account_rules, day_close, desk_rules, pnl
from hokma.broker.service import BrokerError
from hokma.broker.types import Fill
from hokma.db.downgrade import rollback_0028, rollback_0029, rollback_0030, rollback_0031, rollback_0032, rollback_0033
from hokma.db.timeutil import utcnow
from hokma.exec import killswitch, live, prefilter
from hokma.exec import session as sessions
from hokma.i18n import catalog

D = Decimal
START = D(100_000)


class Feed:
    """The reader of the broker of the tests: the last good reading of each account, as 3390e1 gives it (`pnl.latest_pnl`, replaced for the test)."""

    def __init__(self) -> None:
        self.rows: dict[int, pnl.BrokerPnl] = {}

    def __call__(self, account_id: int):
        return self.rows.get(account_id)

    def set(self, account_id: int, *, now, start=START, source="sim", as_of=None) -> None:
        now_f = None if now is None else float(now)
        start_f = None if start is None else float(start)
        self.rows[account_id] = pnl.BrokerPnl(account_id, now_f, start_f, None if now_f is None or start_f is None else now_f - start_f, (),
                                              as_of or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), source)

    def loss(self, account_id: int, amount, start=START) -> None:
        self.set(account_id, now=D(start) - D(amount), start=start)

    def drop(self, account_id: int) -> None:
        self.rows.pop(account_id, None)


@pytest.fixture
def feed(monkeypatch):
    f = Feed()
    monkeypatch.setattr(pnl, "latest_pnl", f)
    return f


def fill(r, account_id, desk_id, cid, side, qty, price, ref, mult=20):
    book_module.record_fill(Fill(account_id, cid, side, float(qty), float(price), 0.0, utcnow(), order_id=ref, multiplier=D(mult), contract_id=cid),
                            desk_id=desk_id, source="sim", source_ref=ref, db=r.db)


def quote(r, cid, price):
    r.board.feed(cid, bid=price, ask=price, last=price)


def rule(r, account_id=None, desk_id=None, usd=500, *, on_trip="block_entries", only_desk=False, control="daily_loss"):
    account_rules.set_rule(desk_id or r.desk_id, account_id or r.account_id, control, usd, OWNER, mode="restriction", on_trip=on_trip, only_desk=only_desk, db=r.db)


def check(r, desk_id=None, **kw):
    return prefilter.run(desk_id or r.desk_id, proposal(r, **({"qty": "1"} | kw)), db=r.db, skip=("monitor",))


def code_of(result):
    return None if result.blocked_by is None else (result.blocked_by.guard, result.blocked_by.code)


def second_desk(r, name="B"):
    desk = make_desk(r.db, name=name, capital=1000)
    desk_id = desk["id"] if isinstance(desk, dict) else desk.id
    with r.db.write() as u:
        link_principal(u, desk_id, r.account_id)
        u.session.insert_row("desk_profile", {"desk_id": desk_id, "line": "DT"})
    return desk_id


# ---- the measure: what the broker said, and unknown when it said nothing -----------------------------------------------------------

def test_the_loss_is_the_start_minus_the_equity_now_that_the_broker_reported(ready, feed):
    m = live.measure(ready.account_id, db=ready.db)
    assert not m.measured and m.loss is None and m.reason == live.NO_DATA and m.equity_live is None       # no reading: unknown, never zero
    feed.loss(ready.account_id, 300)
    m = live.measure(ready.account_id, db=ready.db)
    assert m.measured and m.loss == D(300) and m.day_pnl == D(-300) and m.equity_start == START and m.equity_live == START - 300
    assert m.source == "sim" and m.read_at and m.drawdown == D(300)
    feed.set(ready.account_id, now=START + 120)
    assert live.measure(ready.account_id, db=ready.db).loss == D(-120)                                   # a profit is a negative loss


def test_a_position_in_the_book_with_no_reading_is_not_measured_from_the_book_or_from_prices(ready, feed):
    rule(ready)
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "nb")
    quote(ready, NQ, "20900")                                                                              # 2000 below the entry on Hokma's own board
    m, evals = live.evaluations(ready.db, ready.account_id, ready.desk_id)
    assert not m.measured and m.reason == live.NO_DATA and evals[0].status == "unmeasured"


def test_a_reading_without_the_equity_now_is_no_data(ready, feed):
    feed.set(ready.account_id, now=None)
    m = live.measure(ready.account_id, db=ready.db)
    assert not m.measured and m.reason == live.NO_DATA


def test_without_the_start_of_the_session_the_loss_is_unknown_until_the_owner_declares_it(ready, feed):
    feed.set(ready.account_id, now=99_700, start=None)
    m = live.measure(ready.account_id, db=ready.db)
    assert not m.measured and m.reason == live.START_UNKNOWN and m.equity_live == D(99_700)


def test_the_drawdown_is_the_highest_equity_of_the_readings_minus_the_equity_now(ready, feed):
    feed.set(ready.account_id, now=START + 500)
    assert live.measure(ready.account_id, db=ready.db).drawdown == D(0)
    feed.set(ready.account_id, now=START + 100)
    m = live.measure(ready.account_id, db=ready.db)
    assert m.drawdown == D(400) and live.peak_of(m) == START + 500


def test_the_stub_of_the_reader_gives_none_and_a_reader_that_breaks_is_no_data(ready, monkeypatch):
    assert pnl.latest_pnl(ready.account_id) is None                                                        # until 3390e1 puts the real readers: no data for every account
    monkeypatch.setattr(pnl, "latest_pnl", lambda account_id: (_ for _ in ()).throw(RuntimeError("down")))
    m = live.measure(ready.account_id, db=ready.db)
    assert not m.measured and m.reason == live.NO_DATA                                                     # a reader that breaks is no data: the rules fail closed


# ---- the door: at the cap and not before, and no data blocks ---------------------------------------------------------------------

def test_the_door_blocks_only_when_the_loss_reaches_the_cap_there_are_no_lines_before_it(ready, feed):
    rule(ready, usd=500)
    for loss in (0, 100, 325, 425, 499):                                                                   # the old soft line (325) and hard line (425) are gone
        feed.loss(ready.account_id, loss)
        assert code_of(check(ready)) != ("live_loss", "day_loss_reached"), loss
        assert (code_of(check(ready)) or ("", ""))[0] != "live_loss", loss
    feed.loss(ready.account_id, 500)
    result = check(ready)
    assert result.verdict == "BLOCK" and code_of(result) == ("live_loss", "day_loss_reached")
    assert "500.00" in result.blocked_by.why and "UTC" not in result.blocked_by.why and str(datetime.now(timezone.utc).year) in result.blocked_by.why     # the value, the cap and the hour of the reading
    with ready.db.read() as u:
        assert not day_close.is_closed(u, ready.account_id)                                                 # no monitor ran: the door alone stopped it


def test_without_data_from_the_broker_a_rule_in_restriction_blocks_the_entries_and_the_message_says_why(ready, feed):
    rule(ready)
    result = check(ready)
    assert code_of(result) == ("live_loss", "broker_data_missing")
    assert catalog("en")["live.why.no_data"] in result.blocked_by.why or catalog("es")["live.why.no_data"] in result.blocked_by.why
    feed.set(ready.account_id, now=99_900, start=None)                                                     # a reading with no start of the session
    assert code_of(check(ready)) == ("live_loss", "broker_data_missing")
    feed.loss(ready.account_id, 10)
    assert (code_of(check(ready)) or ("", ""))[0] != "live_loss"                                           # the data came back: the door opens


def test_a_notice_and_no_rule_never_block_for_missing_data(ready, feed):
    assert (code_of(check(ready)) or ("", ""))[0] != "live_loss"                                           # no rule: nothing to check
    account_rules.set_rule(ready.desk_id, ready.account_id, "daily_loss", 500, OWNER, mode="notice", db=ready.db)
    assert (code_of(check(ready)) or ("", ""))[0] != "live_loss"                                           # a notice is asked, never frozen


def test_the_day_result_of_the_account_guard_is_the_one_of_the_broker_and_unknown_stays_unknown(ready, feed):
    with ready.db.write() as u:
        u.broker.set_limits(ready.desk_id, {"funding": {"daily_loss_usd": 500}})
    feed.loss(ready.account_id, 300)
    ctx = prefilter.load_context(ready.desk_id, proposal(ready), db=ready.db)
    assert ctx.book.day_pnl == D(-300) and "day_loss" not in ctx.book.missing
    feed.drop(ready.account_id)
    ctx = prefilter.load_context(ready.desk_id, proposal(ready), db=ready.db)
    assert ctx.book.day_pnl is None and "day_loss" in ctx.book.missing
    result = check(ready)
    assert result.verdict == "BLOCK" and code_of(result)[1] in ("book_unavailable", "broker_data_missing")


@pytest.mark.parametrize("lang", ["es", "en"])
def test_the_messages_of_the_door_exist_in_both_languages(lang):
    words = catalog(lang)
    for key in ("live.loss_reached", "live.data_missing", "live.why.no_data", "live.why.start_unknown", "live.why.shared_account", "live.why.other",
                "risk.stop_required", "risk.stop_invalid", "risk.stop_qty", "risk.stop_unpriced", "risk.stop_shape", "live.freeze.notice", "live.start.unknown"):
        assert words[key], key
    assert not any(key.startswith("live.subs.") or key in ("live.unpriced.notice", "risk.risk_budget", "risk.reserve_shape", "risk.reserve_unpriced") for key in words)


# ---- the monitor acts in the event of the reading that reaches the cap --------------------------------------------------------------

def test_the_monitor_closes_the_day_in_the_same_event_of_the_reading_that_reaches_the_cap(ready, feed):
    rule(ready, usd=500)
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "m1")
    mon = live.start(ready.db)
    try:
        feed.loss(ready.account_id, 200)
        live.on_reading(ready.account_id)
        feed.loss(ready.account_id, 450)                                                                   # 90% of the cap: nothing yet, there is no line before it
        live.on_reading(ready.account_id)
        with ready.db.read() as u:
            assert not day_close.is_closed(u, ready.account_id)
        feed.loss(ready.account_id, 520)
        live.on_reading(ready.account_id)
        with ready.db.read() as u:
            state = day_close.state_of(u, ready.account_id)
        assert state["state"] == "day_closed" and state["rule_id"].endswith("daily_loss") and D(state["loss"]) == D(520)
        assert mon.history[-1]["value"] == D(520) and mon.history[-1]["cap"] == D(500)
        result = check(ready)
        assert result.verdict == "BLOCK" and result.blocked_by.code == "day_closed"
        closing = prefilter.run(ready.desk_id, proposal(ready, side="sell", effect="close"), db=ready.db, skip=("monitor",))
        assert closing.blocked_by is None or closing.blocked_by.code != "day_closed"                       # closes always pass
    finally:
        live.stop()


def test_the_tick_evaluates_the_last_data_of_the_broker_too(ready, feed):
    rule(ready, usd=500)
    mon = live.start(ready.db)
    try:
        feed.loss(ready.account_id, 500)
        mon.tick()
        with ready.db.read() as u:
            assert day_close.is_closed(u, ready.account_id)
    finally:
        live.stop()


def test_a_drawdown_rule_trips_on_the_equity_the_broker_reported(ready, feed):
    rule(ready, usd=300, control="drawdown")
    mon = live.start(ready.db)
    try:
        feed.set(ready.account_id, now=START + 500)
        live.on_reading(ready.account_id)
        feed.set(ready.account_id, now=START + 250)                                                         # 250 under the peak: under the cap
        live.on_reading(ready.account_id)
        with ready.db.read() as u:
            assert not day_close.is_closed(u, ready.account_id)
        feed.set(ready.account_id, now=START + 150)                                                         # 350 under the peak
        live.on_reading(ready.account_id)
        with ready.db.read() as u:
            assert day_close.is_closed(u, ready.account_id)
        assert mon.history[-1]["value"] == D(350)
    finally:
        live.stop()


# ---- A7: the session of the account, not UTC -------------------------------------------------------------------------------

def test_the_session_of_a_futures_account_starts_at_17_in_chicago_and_of_shares_at_the_open():
    assert sessions.session_at("cme", datetime(2026, 11, 25, 22, 59, tzinfo=timezone.utc)).key == "cme:2026-11-24"       # 16:59 CST
    assert sessions.session_at("cme", datetime(2026, 11, 25, 23, 0, tzinfo=timezone.utc)).key == "cme:2026-11-25"         # 17:00 CST
    assert sessions.session_at("cme", datetime(2026, 7, 1, 21, 59, tzinfo=timezone.utc)).key == "cme:2026-06-30"          # 16:59 CDT: the zone has daylight time
    assert sessions.session_at("cme", datetime(2026, 7, 1, 22, 0, tzinfo=timezone.utc)).key == "cme:2026-07-01"
    assert sessions.session_at("us_equity", datetime(2026, 11, 25, 14, 29, tzinfo=timezone.utc)).key == "us_equity:2026-11-24"
    assert sessions.session_at("us_equity", datetime(2026, 11, 25, 14, 30, tzinfo=timezone.utc)).key == "us_equity:2026-11-25"
    s = sessions.session_at("cme", datetime(2026, 11, 25, 23, 0, tzinfo=timezone.utc))
    assert s.ends_at - s.started_at == timedelta(hours=24)


def test_a7_the_close_is_lifted_alone_when_the_next_session_opens_and_the_start_is_new(ready, feed):
    rule(ready)
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "a7")
    live.start(ready.db)
    try:
        feed.loss(ready.account_id, 600)
        live.on_reading(ready.account_id)
        with ready.db.read() as u:
            assert day_close.is_closed(u, ready.account_id)
            assert live.calendar_of(u, ready.account_id) == "cme"                                           # it holds a future: CME session
    finally:
        live.stop()
    later = datetime.now(timezone.utc) + timedelta(hours=26)
    feed.set(ready.account_id, now=START - 600 + 5, start=START - 600)                                      # the broker's new session starts from where the last one ended
    m = live.measure(ready.account_id, db=ready.db, now=later)
    with ready.db.read() as u:
        assert not day_close.is_closed(u, ready.account_id)
        kinds = [(e["kind"], e["who"]) for e in day_close.events(ready.account_id, ready.db)]
    assert ("lifted", "system") in kinds and m.loss == D(-5) and m.session_key != ""


# ---- A12: a broker with no reader cannot be a restriction ---------------------------------------------------------------------

def test_a12_a_loss_restriction_is_refused_where_the_freeze_has_no_data_from_the_broker(ready):
    with ready.db.write() as u:
        outside = u.broker.create_broker("external", "Statement only")
        account = u.broker.create_account(outside.id, "Outside", 1000)
        link_principal(u, ready.desk_id, account.id)
    with pytest.raises(BrokerError) as e:
        account_rules.set_rule(ready.desk_id, account.id, "daily_loss", 100, OWNER, mode="restriction", on_trip="block_entries", db=ready.db)
    assert e.value.code == "live_freeze_unavailable" and e.value.status == 409
    with pytest.raises(BrokerError) as e2:
        desk_rules.set_control(ready.desk_id, "daily_loss", 100, OWNER, mode="restriction", on_trip="block_entries", db=ready.db)
    assert e2.value.code == "live_freeze_unavailable"
    account_rules.set_rule(ready.desk_id, account.id, "daily_loss", 100, OWNER, mode="notice", db=ready.db)               # a notice does not need it
    assert live.source_of("sim")["available"] and live.source_of("schwab")["available"]
    assert not live.source_of("external")["available"] and not live.source_of("ninjatrader")["available"] and "no data from the broker" in live.source_of("ninjatrader")["source"]


# ---- A14 and A15: a shared account ----------------------------------------------------------------------------------------------

def test_a14_a_rule_of_the_account_closes_it_for_every_desk_and_leaves_their_other_accounts(ready, feed):
    b = second_desk(ready)
    with ready.db.write() as u:
        spare = u.broker.create_account(ready.broker_id, "Spare", 50_000)
        u.broker.link(ready.desk_id, spare.id)
    rule(ready, usd=500)                                                                                    # a rule of the ACCOUNT, as the broker reports it for the whole account
    mon = live.start(ready.db)
    try:
        feed.loss(ready.account_id, 500)
        live.on_reading(ready.account_id)
        with ready.db.read() as u:
            assert day_close.is_closed(u, ready.account_id)
        assert check(ready).blocked_by.code == "day_closed" and check(ready, desk_id=b).blocked_by.code == "day_closed"
        other = prefilter.run(ready.desk_id, proposal(ready), db=ready.db, skip=("monitor",), account_id=spare.id)
        assert other.blocked_by is None or other.blocked_by.code not in ("day_closed", "day_loss_reached")
        assert mon.history[-1]["value"] == D(500)
    finally:
        live.stop()


def test_a15_a_rule_of_a_desk_in_an_account_of_that_desk_alone_stops_that_desk(ready, feed):
    rule(ready, usd=200, only_desk=True)                                                                    # the account belongs to this desk only: the broker's number is the desk's
    live.start(ready.db)
    try:
        feed.loss(ready.account_id, 210)
        live.on_reading(ready.account_id)
        with ready.db.read() as u:
            assert not day_close.is_closed(u, ready.account_id)
            assert live.active_stop(u, ready.desk_id, ready.account_id) is not None
        assert code_of(check(ready)) == ("live_loss", "desk_stopped")
    finally:
        live.stop()


def test_a15_a_rule_of_a_desk_in_a_shared_account_is_not_measured_and_blocks_that_desk_only(ready, feed):
    rule(ready, usd=200, only_desk=True)                                                                    # made while the account was this desk's alone
    b = second_desk(ready)                                                                                  # a second desk joins the account afterwards: the rule stays, unmeasured
    feed.loss(ready.account_id, 50)
    result = check(ready)
    assert code_of(result) == ("live_loss", "broker_data_missing") and catalog("en")["live.why.shared_account"] in result.blocked_by.why or \
        catalog("es")["live.why.shared_account"] in result.blocked_by.why
    assert (code_of(check(ready, desk_id=b)) or ("", ""))[0] != "live_loss"                                 # the rule is not the other desk's
    m, evals = live.evaluations(ready.db, ready.account_id, ready.desk_id)
    assert m.measured and evals[0].status == "unmeasured" and evals[0].why == live.SHARED                   # the account IS measured; the part of one desk is not


# ---- on_trip: what the close does ---------------------------------------------------------------------------------------------------

def _two_positions(ready, feed, on_trip):
    rule(ready, usd=500, on_trip=on_trip)
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "t1")
    fill(ready, ready.account_id, ready.desk_id, SPY, "buy", 10, 600, "t2", mult=1)
    quote(ready, NQ, "21000")                                                                               # the sim broker needs a price to close with; the freeze does not read it
    quote(ready, SPY, "600")
    live.start(ready.db)
    feed.loss(ready.account_id, 2000)                                                                       # the broker says -2000: past the cap
    live.on_reading(ready.account_id)


def _held(ready):
    return {k: v.qty for k, v in book_module.Book.load(ready.account_id, db=ready.db).cost_positions().items()}


def test_block_and_flatten_closes_the_futures_and_does_not_sell_the_shares_held_long(ready, feed):
    _two_positions(ready, feed, "block_and_flatten")
    try:
        assert _held(ready) == {SPY: D(10)}
        with ready.db.read() as u:
            assert day_close.is_closed(u, ready.account_id)
    finally:
        live.stop()


def test_block_and_flatten_all_sells_the_shares_too(ready, feed):
    _two_positions(ready, feed, "block_and_flatten_all")
    try:
        assert _held(ready) == {}
    finally:
        live.stop()


def test_block_entries_touches_nothing_that_is_open(ready, feed):
    _two_positions(ready, feed, "block_entries")
    try:
        assert _held(ready) == {NQ: D(1), SPY: D(10)}
    finally:
        live.stop()


# ---- the backup killswitch reads the data of the broker and stops the account, not the desk -----------------------------------------------

def test_the_killswitch_reads_the_loss_of_the_broker_and_closes_the_account(ready, feed):
    with ready.db.write() as u:
        u.broker.set_limits(ready.desk_id, {"funding": {"daily_loss_usd": 500}})
    feed.loss(ready.account_id, 500)
    assert killswitch.run_once(ready.db) == {ready.desk_id: "halt"}
    with ready.db.read() as u:
        assert day_close.is_closed(u, ready.account_id) and u.desks.find(ready.desk_id).status == "active"


def test_the_killswitch_does_not_measure_without_data_from_the_broker(ready, feed):
    with ready.db.write() as u:
        u.broker.set_limits(ready.desk_id, {"funding": {"daily_loss_usd": 500}})
    level = killswitch._check_account(ready.desk_id, SimpleNamespace(id=ready.account_id), {"funding": {"daily_loss_usd": 500}}, ready.db)
    assert level.measured is False and level.name == "ok"


# ---- the migrations: the table of reservations is not in this chain, the columns of prices are gone ----------------------------------------

def _columns(con, table):
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def test_the_session_table_has_no_columns_of_prices_and_the_way_back_of_0031_restores_them(env, db):
    import sqlite3

    from hokma.db import db_path
    from hokma.db.migrate import current_version

    take_back_the_migrations_after_0033(db_path())
    con = sqlite3.connect(db_path())
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    cols = _columns(con, "account_live_session")
    con.close()
    assert current_version() == 33 and ("risk" + "_reservations") not in tables                            # the table of reservations of the first 0031 is not in this chain, and nothing was dropped anywhere
    assert "pnl_start" not in cols and "desk_start" not in cols and "equity_start" in cols
    assert rollback_0033(db_path()) == 32 and rollback_0032(db_path()) == 31 and rollback_0031(db_path()) == 30
    con = sqlite3.connect(db_path())
    assert {"pnl_start", "desk_start", "equity_start"} <= set(_columns(con, "account_live_session"))
    con.close()


def test_the_way_back_of_0028_drops_its_two_tables(env, db):
    import sqlite3

    from hokma.db import db_path

    take_back_the_migrations_after_0033(db_path())
    assert rollback_0033(db_path()) == 32 and rollback_0032(db_path()) == 31 and rollback_0031(db_path()) == 30
    assert rollback_0030(db_path()) == 29 and rollback_0029(db_path()) == 28
    assert rollback_0028(db_path()) == 27
    con = sqlite3.connect(db_path())
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "account_live_session" not in names and "desk_day_stops" not in names and "account_day_state" in names
    con.close()


# ---- overshoot in simulation ---------------------------------------------------------------------------------------------------------------

def simulate_overshoot(ready, feed, runs=60, seed=7):
    """The loss grows reading after reading by a random jump; the monitor closes the day in the event of the first reading at or over the cap of 500. Returns, for each run,
    the loss of that reading minus the cap (the overshoot) and the jump."""
    rng = random.Random(seed)
    results = []
    for n in range(runs):
        with ready.db.write() as u:
            account = u.broker.create_account(ready.broker_id, f"Run {n}", 5000)
            link_principal(u, ready.desk_id, account.id)
        account_rules.set_rule(ready.desk_id, account.id, "daily_loss", 500, OWNER, mode="restriction", on_trip="block_entries", db=ready.db)
        jump = D(rng.choice(["5", "20", "60", "150", "400"]))
        mon = live.start(ready.db)
        mon.history.clear()
        loss = D(0)
        while not mon.history and loss < 5000:
            loss += jump
            feed.loss(account.id, loss)
            live.on_reading(account.id)
        closed = [h for h in mon.history if h["account_id"] == account.id]
        assert closed, "the monitor never closed"
        results.append({"overshoot": closed[0]["value"] - closed[0]["cap"], "jump": jump, "before": closed[0]["value"] - jump})
        live.stop()
        with ready.db.write() as u:
            u.session.execute("DELETE FROM desk_account_links WHERE account_id = :a", {"a": account.id})
            u.session.execute("DELETE FROM account_rules WHERE account_id = :a", {"a": account.id})
    return results


def test_the_close_happens_in_the_first_reading_at_or_over_the_cap_and_the_overshoot_is_inside_one_jump(ready, feed):
    results = simulate_overshoot(ready, feed)
    assert len(results) == 60
    assert all(r["overshoot"] >= 0 for r in results)                                                        # it never closes before the cap
    assert all(r["before"] < 500 for r in results)                                                          # the reading before it was under the cap: it closed in the first one over
    assert all(r["overshoot"] < r["jump"] for r in results)                                                 # and never later than the jump that crossed it
    print("OVERSHOOT max", max(r["overshoot"] for r in results), "max jump", max(r["jump"] for r in results))


# ---- a fill booked by the sync, the second line wired, and the code of the retired pieces gone ----------------------------------------------

def test_a_fill_booked_by_the_sync_is_looked_at_at_once(ready, monkeypatch):
    from hokma.backoffice import sync

    seen = []
    monkeypatch.setattr(live, "after_fill", lambda account_id: seen.append(account_id))
    sync._fills_booked(ready.account_id, SimpleNamespace(booked=0))
    sync._fills_booked(ready.account_id, SimpleNamespace(booked=2))
    assert seen == [ready.account_id]


def test_a23_the_code_of_the_reservations_the_subscriptions_and_the_prices_of_the_freeze_is_gone():
    """SPEC-026 v2, A23: none of the retired names is left in backend/ (the words are put together here so this file does not count)."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    words = ["risk" + "_reservations", "reserve" + ".hold", "RiskBudget" + "Guard", "LiveSub" + "scriptions", "live" + "_marks", "live" + "_subs", "feed_from" + "_market",
             "hokma.exec." + "reserve", "SOFT" + "_PCT", "HARD" + "_PCT"]
    pattern = re.compile("|".join(re.escape(w) for w in words))
    found = []
    for path in root.rglob("*.py"):
        if path == pathlib.Path(__file__).resolve() or "site-packages" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        found += [f"{path.relative_to(root)}: {m.group(0)}" for m in pattern.finditer(text)]
    assert found == []
