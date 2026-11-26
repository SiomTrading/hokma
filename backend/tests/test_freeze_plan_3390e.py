"""The plan of tests of 10.1 of SPEC-026 v2 (P1 to P9), run on fixtures: the answers of Schwab and Alpaca are the PROVISIONAL ones (schwab_fixtures/pnl and alpaca_fixtures/pnl:
the structure of the documentation with made-up numbers) and the simulated broker is the real one of Hokma. The real steps, on the real account of the owner, are in the guide
docs/test/3390e/GUIA-PRUEBA-REAL.md: when the real captures are copied next to the provisional ones, the same tests read them (test_broker_pnl.py picks them up).

P1 mapping of the account of Schwab    P2 notice at the cap, Close confirmed by the broker    P3 restriction blocks the next buy    P4 ACCT_ACTIVITY reads at once
P5 the broker stops answering          P6 simulated 5000: flatten on the reading, confirmed on the next    P7 the three stop rejections    P8 partial fill    P9 NinjaTrader"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db  # noqa: F401  (fixtures)
from exec_support import NQ, ready  # noqa: F401  (fixtures)
from test_desk_rules import OWNER
from test_live_blockers import open_qty
from test_live_close_no_double import wired  # noqa: F401  (fixtures)
from test_live_freeze import check, code_of, feed, fill, quote, rule  # noqa: F401  (fixtures)

from hokma.broker import account_rules, day_close, pnl
from hokma.broker.pnl import PnlReader, latest_pnl, schwab_pnl
from hokma.exec import live
from hokma.exec.guards.stop import StopRefused, check_stop
from hokma.i18n import t

D = Decimal
FIXTURE = Path(__file__).parent / "schwab_fixtures" / "pnl" / "accounts_positions.provisional.json"


def _told(monkeypatch) -> list[str]:
    told: list[str] = []
    monkeypatch.setattr(live, "_notify", lambda database, text: told.append(text))
    monkeypatch.setattr(live, "notify_reached", lambda database, text: told.append(text) or True)
    return told


def _schwab_securities() -> list[dict]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [item["securitiesAccount"] for item in (data if isinstance(data, list) else data["accounts_fields_positions"])]


def test_p1_the_account_of_schwab_shows_equity_loss_of_the_day_and_open_pl_of_each_position_with_the_hour_of_the_reading():
    for account_id, securities in enumerate(_schwab_securities(), 1):
        reading = schwab_pnl(account_id, securities, "2026-11-25T15:00:00Z")
        assert reading.source == "schwab" and reading.as_of == "2026-11-25T15:00:00Z"                  # "fuente: Schwab" and the hour come from this
        assert reading.usable and reading.day_pnl == pytest.approx(reading.equity_now - reading.equity_start)
        assert reading.positions and any(p.open_pl is not None for p in reading.positions)               # the P&L of each position is the broker's, never the book's
        assert all(p.open_pl is None or p.open_pl != 0.0 or p.day_pl == 0.0 for p in reading.positions)   # what the broker did not give is None, not a made-up zero


def test_p2_a_notice_tells_at_the_cap_and_a_close_is_not_done_until_the_broker_stops_reporting_the_position(ready, feed, wired, monkeypatch):
    told = _told(monkeypatch)
    account_rules.set_rule(ready.desk_id, ready.account_id, "daily_loss", 1, OWNER, mode="notice", timeout_min=5, db=ready.db)
    fill(ready, ready.account_id, ready.desk_id, "EQ:SPY", "buy", 1, 600, "p2", mult=1)
    mon = live.start(ready.db)
    try:
        feed.loss(ready.account_id, 1)                                                                   # the account moved 1 USD against
        live.on_reading(ready.account_id)
        assert len(told) == 1 and "1.00" in told[0]
    finally:
        live.stop()
    broker = wired({"EQ:SPY": "1"})
    broker.place_order = lambda account_id, order: (broker.sent.append(order), {"order_id": "1", "status": "WORKING"})[1]        # the broker takes the sale and has not filled it yet
    text, remaining = live.close_positions(ready.db, ready.account_id, None, include_long_shares=True)    # the owner pressed Close and confirmed
    assert remaining == ["EQ:SPY"]                                                                      # the broker still reports it: it is not closed
    broker.pos["EQ:SPY"] = D(0)                                                                         # the next reading of the broker no longer carries it
    broker.live.clear()
    _text, remaining = live.close_positions(ready.db, ready.account_id, None, include_long_shares=True)
    assert remaining == []


def test_p3_a_restriction_at_the_cap_closes_the_day_and_the_next_buy_is_refused_with_value_hour_and_cap(ready, feed):
    rule(ready, usd=1)
    mon = live.start(ready.db)
    try:
        feed.loss(ready.account_id, 1)
        live.on_reading(ready.account_id)
        with ready.db.read() as u:
            assert day_close.is_closed(u, ready.account_id)
    finally:
        live.stop()
    refused = check(ready)
    assert refused.blocked_by is not None and refused.blocked_by.code == "day_closed" or code_of(refused) == ("live_loss", "day_loss_reached")


def test_p4_a_message_of_the_account_stream_asks_for_a_read_at_once_and_two_in_a_row_make_only_one(ready):
    calls: list[str] = []
    reader = PnlReader(ready.db, None, notify=lambda text: None, spawn=lambda fn: fn(), later=lambda delay, fn: calls.append("later"))
    reader.read_kind = lambda kind, periodic=True: calls.append(kind)                                    # what is read is not the point here: when it is
    assert reader.request_extra("schwab") == "now"
    assert reader.request_extra("schwab") in ("later", "queued") and calls.count("schwab") == 1          # inside the second: one read, not two
    pnl.start(ready.db)
    try:
        before = pnl.reader().reads["schwab"]
        pnl.on_account_activity("schwab", "ERROR")                                                       # an ERROR is not a signal
        assert pnl.reader().reads["schwab"] == before
    finally:
        pnl.stop()


def test_p5_when_the_broker_stops_answering_three_reads_say_so_once_block_the_openings_close_nothing_and_say_when_it_is_back(ready, feed, monkeypatch):
    rule(ready, usd=500)
    notes: list[str] = []
    state = {"ok": True}
    good = lambda *a, **k: {ready.account_id: pnl.BrokerPnl(ready.account_id, 100_000.0, 100_000.0, 0.0, (), pnl.iso_now(), "schwab")}      # noqa: E731
    bad = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network down"))                              # noqa: E731
    monkeypatch.setattr(pnl, "schwab_accounts_of", lambda database: {"acct": ready.account_id})
    reader = PnlReader(ready.db, None, notify=notes.append, schwab=lambda *a, **k: good() if state["ok"] else bad(), spawn=lambda fn: fn(), later=lambda d, fn: None)
    reader.read_kind("schwab")
    assert reader.board.view(ready.account_id, "schwab")["state"] == "ok" and notes == []
    state["ok"] = False
    for _ in range(3):
        reader.read_kind("schwab")
    assert reader.board.view(ready.account_id, "schwab")["state"] == "no_data" and len(notes) == 1       # one notice, at the third read
    feed.drop(ready.account_id)                                                                          # the freeze sees what the reader says: no data
    assert code_of(check(ready)) == ("live_loss", "broker_data_missing")                                 # an opening in that lapse is blocked
    with ready.db.read() as u:
        assert not day_close.is_closed(u, ready.account_id)                                              # and nothing was closed
    state["ok"] = True
    reader.read_kind("schwab")
    assert len(notes) == 2 and reader.board.view(ready.account_id, "schwab")["state"] == "ok"           # it came back: told


def test_p6_the_simulated_account_flattens_on_the_reading_that_crosses_and_the_next_reading_confirms_it(ready, monkeypatch):
    told = _told(monkeypatch)
    rule(ready, usd=50, on_trip="block_and_flatten")
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "p6")
    quote(ready, NQ, "21000")
    pnl.board().clear()
    live.start(ready.db)
    reader = PnlReader(ready.db, None, notify=lambda text: None, spawn=lambda fn: fn(), later=lambda d, fn: None)
    try:
        reader.read_kind("sim")
        assert latest_pnl(ready.account_id).day_pnl == 0.0
        quote(ready, NQ, "20997.5")                                                                      # 2.5 points of NQ = 50 USD
        reader.read_kind("sim")                                                                          # the reading that crosses: the monitor acts in this very event
        with ready.db.read() as u:
            assert day_close.is_closed(u, ready.account_id)
        assert open_qty(ready) == {}                                                                     # the sale was sent and filled
        reader.read_kind("sim")
        assert latest_pnl(ready.account_id).positions == ()                                              # the next reading of the broker no longer carries it
    finally:
        live.stop()
        pnl.board().clear()
    assert told and "50.00" in told[0]


def test_p7_the_three_stop_rejections_no_stop_a_stop_on_the_wrong_side_and_a_stop_exit_for_less(ready):
    from exec_support import proposal as make

    sheets = {NQ: SimpleNamespace(asset_type="future", multiplier=20, tick_size=0.25)}
    marks = {NQ: D(21000)}

    def code(**kw):
        with pytest.raises(StopRefused) as e:
            check_stop(make(ready, **kw), sheets, marks)
        return e.value.code

    assert code(stop_loss=None) == "stop_required"
    assert code(stop_loss=D(21010)) == "stop_invalid"                                                    # a buy with its stop above the entry
    # the third one, a STOP exit for 1 of an entry of 10, is refused by the filter of the native orders: exec/test_native_filter.py::test_the_stop_of_a_trigger_entry_has_to_cover_the_whole_quantity_of_the_entry


def test_p8_a_partial_fill_shows_the_quantity_and_the_open_pl_the_simulator_reports_not_the_one_of_the_order(ready):
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "p8")                              # an order of 3 filled 1
    quote(ready, NQ, "21002")
    found = pnl.read_sim(ready.db, [ready.account_id])[ready.account_id]
    (position,) = found.positions
    assert (position.qty, position.open_pl) == (1.0, 40.0)                                               # 1 contract, 2 points of NQ at 20 USD


def test_p9_ninjatrader_gives_no_data_and_a_loss_restriction_is_not_saved(ready):
    from test_live_freeze import test_a12_a_loss_restriction_is_refused_where_the_freeze_has_no_data_from_the_broker as a12

    a12(ready)
