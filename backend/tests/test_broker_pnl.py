"""The P&L of the broker (SPEC-026 v2, piece 3390e1): the contract `BrokerPnl` and `latest_pnl`, the Schwab and simulator mapping, the state
"no data from the broker" after 3 failed reads, a loss restriction refused where there is no reader, and the extra read that ACCT_ACTIVITY asks for.

The Schwab answers here are PROVISIONAL: they have the exact structure of the guide, with made-up numbers (schwab_fixtures/pnl/README.md says how the
real capture replaces them). The same checks run on `accounts_positions.real.json` when that file exists."""
from __future__ import annotations

import inspect
import json
import time
from pathlib import Path

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db, make_desk  # noqa: F401  (fixtures and helper)
from exec_support import NQ, ready  # noqa: F401  (fixtures)
from schwab_support import FakeSchwab, clear_fake  # noqa: F401
from test_broker_schwab_accounts import TR, connections, fake, keys, linked, server  # noqa: F401  (fixtures)
from test_desk_rules import OWNER
from test_live_freeze import fill, quote

from hokma.broker import account_rules, pnl, service
from hokma.broker.pnl import BrokerPnl, PnlBoard, PnlReader, latest_pnl, schwab_pnl
from hokma.broker.service import BrokerError
from hokma.i18n import t, use_language

FIXTURES = Path(__file__).parent / "schwab_fixtures" / "pnl"
PROVISIONAL = FIXTURES / "accounts_positions.provisional.json"
REAL = FIXTURES / "accounts_positions.real.json"
CASES = [pytest.param(PROVISIONAL, id="provisional")] + ([pytest.param(REAL, id="real")] if REAL.exists() else [])
AS_OF = "2026-11-25T15:00:00Z"


def answer(path: Path) -> list:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["accounts_fields_positions"] if isinstance(data, dict) else data


@pytest.fixture(autouse=True)
def clean_board(monkeypatch):
    monkeypatch.setattr(pnl, "_board", PnlBoard())               # a board of its own: §latest_pnl§ and the readers of the test see the same one
    pnl.stop()
    yield
    pnl.stop()


# ---------------------------------------------------------------------------------------------------- the contract


def test_the_contract_has_the_names_the_guard_of_3390e3_calls():
    assert list(inspect.signature(latest_pnl).parameters) == ["account_id"]
    assert [f for f in BrokerPnl.__dataclass_fields__] == ["account_id", "equity_now", "equity_start", "day_pnl", "positions", "as_of", "source"]
    assert latest_pnl(987654) is None                                           # never read: no data, not a made-up zero


# ---------------------------------------------------------------------------------------------------- A13: the mapping of Schwab


@pytest.mark.parametrize("path", CASES)
def test_a13_each_account_maps_by_the_broker_fields_and_nothing_is_calculated_by_hokma(path):
    for index, item in enumerate(answer(path)):
        securities = item["securitiesAccount"]
        out = schwab_pnl(index + 1, securities, AS_OF)
        current, initial = securities.get("currentBalances", {}), securities.get("initialBalances", {})
        if securities["type"] == "CASH":
            now, start = current["totalCash"] + current.get("longNonMarginableMarketValue", 0.0), initial["liquidationValue"]
        else:
            now, start = current["equity"], initial["equity"]
        assert (out.equity_now, out.equity_start) == (now, start)
        assert out.day_pnl == pytest.approx(now - start)
        assert out.source == "schwab" and out.as_of == AS_OF and out.usable
        for position, raw in zip(out.positions, securities.get("positions", [])):
            assert position.symbol == raw["instrument"]["symbol"]
            assert position.day_pl == raw.get("currentDayProfitLoss")
            opens = [raw[k] for k in ("longOpenProfitLoss", "shortOpenProfitLoss") if k in raw]
            assert position.open_pl == (sum(opens) if opens else None)


def test_a13_the_provisional_numbers_are_the_ones_of_the_guide():
    margin, cash = (schwab_pnl(i, item["securitiesAccount"], AS_OF) for i, item in enumerate(answer(PROVISIONAL), 1))
    assert (margin.equity_now, margin.equity_start, margin.day_pnl) == (24966.0, 25000.0, -34.0)
    assert (cash.equity_now, cash.equity_start, cash.day_pnl) == (9962.5, 10000.0, -37.5)
    spy, qqq, iwm = margin.positions
    assert (spy.qty, spy.day_pl, spy.open_pl) == (10.0, -34.0, -34.0)
    assert (qqq.qty, qqq.day_pl, qqq.open_pl) == (-5.0, 12.5, 20.25)                         # a short: the open P&L is the short side
    assert (iwm.qty, iwm.day_pl, iwm.open_pl) == (3.0, None, None)                           # what Schwab does not give is None, never 0


def test_a13_without_the_start_of_the_day_the_day_pnl_is_unknown_unless_the_owner_declared_it():
    securities = json.loads(json.dumps(answer(PROVISIONAL)[0]["securitiesAccount"]))
    del securities["initialBalances"]
    unknown = schwab_pnl(1, securities, AS_OF)
    assert unknown.equity_now == 24966.0 and unknown.day_pnl is None and not unknown.usable
    declared = schwab_pnl(1, securities, AS_OF, declared=lambda: 25100.0)
    assert declared.equity_start == 25100.0 and declared.day_pnl == -134.0 and declared.usable


def test_a13_a_whole_read_is_one_call_for_all_the_accounts_and_no_number_comes_from_the_book(linked, fake):
    fake.on("GET", f"{TR}/accounts", answer(PROVISIONAL))
    before = len(fake.calls)
    found = pnl.read_schwab(linked)
    calls = [c for c in fake.calls[before:] if c.path == f"{TR}/accounts"]
    assert len(calls) == 1 and calls[0].query == {"fields": "positions"}                    # ONE call for every account
    assert sorted(round(p.day_pnl, 2) for p in found.values()) == [-37.5, -34.0]
    with linked.read() as u:
        assert u.session.one("SELECT COUNT(*) AS n FROM book_entries")["n"] == 0            # nothing in the book, and the numbers are there anyway
    assert all(p.source == "schwab" and p.as_of.endswith("Z") for p in found.values())


# ---------------------------------------------------------------------------------------------------- A6: no data from the broker


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def schwab_ids(db):
    return sorted(pnl.schwab_accounts_of(db).values())


def reader_of(db, source, notes):
    return PnlReader(db, None, notify=notes.append, schwab=source, spawn=lambda fn: fn(), later=lambda delay, fn: None)


def good(account_id, day=-10.0):
    return BrokerPnl(account_id, 990.0, 1000.0, day, (), pnl.iso_now(), "schwab")


def test_a6_three_failed_reads_in_a_row_are_no_data_one_notice_and_the_return_is_told_once(linked):
    with use_language("es"):
        check_three_failed_reads(linked)


def check_three_failed_reads(linked):
    ids = schwab_ids(linked)
    state = {"up": True}
    notes: list[str] = []

    def source(db, declared=None):
        if not state["up"]:
            raise RuntimeError("429")
        return {i: good(i) for i in ids}

    reader = reader_of(linked, source, notes)
    reader.read_kind("schwab")
    first = ids[0]
    assert latest_pnl(first) is not None and reader.board.view(first)["state"] == "ok"
    state["up"] = False
    reader.read_kind("schwab")
    reader.read_kind("schwab")
    assert latest_pnl(first) is not None and notes == []                                      # two failures are not yet "no data"
    reader.read_kind("schwab")
    view = reader.board.view(first)
    assert latest_pnl(first) is None and view["state"] == "no_data"
    assert view["since"] == view["last"]["as_of"]                                           # since the last good reading, with its hour
    assert len(notes) == len(ids) and all("desde las " + view["since"][11:19] in n for n in notes)               # one notice per account, never a close
    reader.read_kind("schwab")
    assert len(notes) == len(ids)                                                           # the fourth failure does not tell again
    state["up"] = True
    reader.read_kind("schwab")
    assert latest_pnl(first) is not None and reader.board.view(first)["state"] == "ok"
    assert len(notes) == 2 * len(ids) and notes[-1] == t("pnl.back", account=reader._name(ids[-1]))


def test_a6_an_account_missing_from_the_answer_or_with_no_start_counts_as_a_failed_read(linked):
    ids = schwab_ids(linked)
    notes: list[str] = []
    no_start = BrokerPnl(ids[1], 990.0, None, None, (), pnl.iso_now(), "schwab")
    reader = reader_of(linked, lambda db, declared=None: {ids[0]: good(ids[0]), ids[1]: no_start}, notes)
    for _ in range(3):
        reader.read_kind("schwab")
    assert latest_pnl(ids[0]) is not None and latest_pnl(ids[1]) is None
    assert len(notes) == 1


def test_a6_a_429_from_schwab_is_a_failed_read(linked, fake):
    fake.on("GET", f"{TR}/accounts", (429, {"errors": [{"status": "429", "title": "Too Many Requests"}]}))
    notes: list[str] = []
    reader = PnlReader(linked, None, notify=notes.append)
    before = len([c for c in fake.calls if c.path == f"{TR}/accounts"])
    for _ in range(3):
        reader.read_kind("schwab")
    # the 429 is one failed read; the reads that follow give way (the pause of the reads) and neither call Schwab nor count as failures
    assert all(latest_pnl(i) is None for i in schwab_ids(linked)) and not notes
    assert len([c for c in fake.calls if c.path == f"{TR}/accounts"]) - before == 1


def test_a6_a_reader_that_stopped_is_no_data_too():
    clock = Clock()
    board = PnlBoard(clock=clock)
    board.good(good(5))
    assert board.latest(5) is not None
    clock.now += pnl.MAX_AGE + 1
    assert board.latest(5) is None and board.view(5, "schwab")["state"] == "no_data"


def test_a6_the_screen_view_says_pending_before_the_first_read_and_unavailable_without_a_reader(desks_db):
    with desks_db.write() as u:
        sim = next(b for b in u.broker.list_brokers() if b.kind == "sim")
        a = u.broker.create_account(sim.id, "Sim A", 5000)
        outside = u.broker.create_broker("external", "Statement only")
        b = u.broker.create_account(outside.id, "Outside", 1000)
    found = pnl.views(desks_db)
    assert found[a.id]["state"] == "pending" and found[b.id]["state"] == "unavailable" and found[b.id]["last"] is None


# ---------------------------------------------------------------------------------------------------- A12: no reader, no restriction


def test_a12_a_loss_restriction_is_refused_where_the_broker_gives_no_pnl(ready):
    from account_links_support import link_principal

    with ready.db.write() as u:
        outside = u.broker.create_broker("external", "NinjaTrader later")
        account = u.broker.create_account(outside.id, "Outside", 1000)
        link_principal(u, ready.desk_id, account.id)
    sentences = set()
    for lang in ("es", "en"):
        with use_language(lang):
            with pytest.raises(BrokerError) as e:
                account_rules.set_rule(ready.desk_id, account.id, "daily_loss", 100, OWNER, mode="restriction", on_trip="block_entries", db=ready.db)
            assert e.value.code == "live_freeze_unavailable" and e.value.status == 409
            assert e.value.message == t("pnl.err.unavailable", account=str(account.id), kind="external")         # the sentence of the catalog, in the language of the panel
            sentences.add(e.value.message)
    assert len(sentences) == 2                                                                                  # one per language
    account_rules.set_rule(ready.desk_id, account.id, "daily_loss", 100, OWNER, mode="notice", db=ready.db)         # a notice needs no reader
    account_rules.set_rule(ready.desk_id, ready.account_id, "daily_loss", 100, OWNER, mode="restriction", on_trip="block_entries", db=ready.db)   # the simulator has one
    assert pnl.has_reader("schwab") and pnl.has_reader("sim") and not pnl.has_reader("ninjatrader")


# ---------------------------------------------------------------------------------------------------- A22: ACCT_ACTIVITY asks for one more read


def test_a22_a_message_reads_at_once_and_a_second_one_inside_the_second_does_not_read_again():
    clock, ran, delayed = Clock(), [], []
    reader = PnlReader(None, PnlBoard(clock=clock), clock=clock, spawn=lambda fn: fn(), later=lambda d, fn: delayed.append((d, fn)),
                       notify=lambda text: None)
    reader.read_kind = lambda kind, periodic=True: ran.append((kind, clock.now))                          # type: ignore[method-assign]
    assert reader.request_extra("schwab") == "now" and ran == [("schwab", 1000.0)]
    clock.now += 0.25
    assert reader.request_extra("schwab") == "later"                                        # inside the second: asked for when the second ends
    assert len(ran) == 1 and delayed[0][0] == pytest.approx(0.75)
    assert reader.request_extra("schwab") == "queued" and len(delayed) == 1                 # a third one rides on the same read
    clock.now += 0.75
    delayed[0][1]()
    assert len(ran) == 2
    clock.now += 5
    assert reader.request_extra("schwab") == "now" and len(ran) == 3


def test_a22_in_the_running_app_an_activity_message_reads_the_accounts_in_less_than_a_second(linked, fake):
    fake.on("GET", f"{TR}/accounts", answer(PROVISIONAL))
    pnl.start(linked)

    def reads() -> int:
        return len([c for c in fake.calls if c.path == f"{TR}/accounts"])

    base = reads()
    started = time.monotonic()
    pnl.on_account_activity("schwab", "SUBSCRIBED")
    for _ in range(100):
        if reads() > base:
            break
        time.sleep(0.01)
    assert reads() == base + 1 and time.monotonic() - started < 1.0                         # no waiting for the 5 second pass
    pnl.on_account_activity("schwab", "ORDER_FILL")                                         # inside the second of the first one
    time.sleep(0.3)
    assert reads() == base + 1                                                              # not in the same second
    for _ in range(150):
        if reads() > base + 1:
            break
        time.sleep(0.01)
    assert reads() == base + 2                                                              # one read when the second ends, not two
    pnl.on_account_activity("schwab", "ERROR")
    time.sleep(1.2)
    assert reads() == base + 2                                                              # an ERROR is not a signal


# ---------------------------------------------------------------------------------------------------- the simulated broker


def test_the_simulator_reports_its_equity_minus_the_equity_of_the_start_of_its_session(ready):
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "c1")
    quote(ready, NQ, "20990")
    found = pnl.read_sim(ready.db, [ready.account_id])[ready.account_id]
    assert found.source == "sim" and found.usable
    assert found.equity_start == 1_000_000.0 and found.equity_now == 999_800.0 and found.day_pnl == -200.0
    (position,) = found.positions
    assert (position.qty, position.open_pl, position.day_pl) == (1.0, -200.0, -200.0)
    notes: list[str] = []
    reader = PnlReader(ready.db, None, notify=notes.append)
    reader.read_kind("sim")
    assert latest_pnl(ready.account_id).day_pnl == -200.0


def test_the_simulator_without_a_price_is_a_failed_read_never_a_zero(ready):
    fill(ready, ready.account_id, ready.desk_id, "FUT:ES:2026-12", "buy", 1, 5000, "c1", mult=50)           # a contract the board has no quote for
    found = pnl.read_sim(ready.db, [ready.account_id])[ready.account_id]
    assert found.equity_now is None and found.day_pnl is None and not found.usable


def test_a_fill_asks_for_one_more_read_of_the_simulator(ready):
    pnl.start(ready.db)
    from hokma.exec import live

    live.after_fill(ready.account_id)
    for _ in range(100):
        if pnl.reader().reads["sim"]:
            break
        time.sleep(0.01)
    assert pnl.reader().reads["sim"] == 1


# ---------------------------------------------------------------------------------------------------- what the screen and the sample read


def test_the_screen_endpoint_reads_the_last_reads_without_asking_a_broker(desks_db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hokma.broker.api import router
    from hokma.rpc.ws import SESSION_TOKEN

    with desks_db.write() as u:
        sim = next(b for b in u.broker.list_brokers() if b.kind == "sim")
        a = u.broker.create_account(sim.id, "Sim A", 5000)
    pnl.board().good(BrokerPnl(a.id, 4950.0, 5000.0, -50.0, (), pnl.iso_now(), "sim"))
    app = FastAPI()
    app.include_router(router)
    answer = TestClient(app).get("/api/accounts/pnl", headers={"x-hokma-token": SESSION_TOKEN})
    assert answer.status_code == 200
    one = answer.json()["accounts"][str(a.id)]
    assert one["state"] == "ok" and one["last"]["day_pnl"] == -50.0 and one["last"]["as_of"].endswith("Z") and one["source"] == "sim"
    assert TestClient(app).get("/api/accounts/pnl").status_code == 401


def test_the_streamer_keeps_the_last_activity_message_in_memory_for_the_sample_only():
    from hokma.schwab.stream import SchwabStreamer

    streamer = object.__new__(SchwabStreamer)
    streamer._last_activity, streamer._on_activity = None, None
    assert streamer.last_activity() is None
    streamer._activity([{"seq": 1, "key": "Account Activity", "1": "12345678", "2": "SUBSCRIBED", "3": ""}, {"seq": 2, "1": "12345678", "2": "ORDER_FILL", "3": "x"}])
    kept = streamer.last_activity()
    assert kept["entry"]["2"] == "ORDER_FILL" and kept["received_at"].endswith("Z")
    kept["entry"]["2"] = "changed"
    assert streamer.last_activity()["entry"]["2"] == "ORDER_FILL"                         # a copy, not the object inside


# ---------------------------------------------------------------------------------------------------- Vera 3390e1: blocking fixes


def test_a_cash_account_with_positions_and_no_value_held_has_no_equity_never_a_zero():
    securities = {"type": "CASH", "currentBalances": {"totalCash": 1000.0}, "initialBalances": {"liquidationValue": 1000.0},
                  "positions": [{"instrument": {"symbol": "SPY"}, "longQuantity": 2.0}]}
    unknown = schwab_pnl(1, securities, AS_OF)
    assert unknown.equity_now is None and unknown.day_pnl is None and not unknown.usable
    flat = schwab_pnl(1, {**securities, "positions": []}, AS_OF)                             # nothing held: the cash is all there is
    assert flat.equity_now == 1000.0 and flat.day_pnl == 0.0
    held = {**securities, "currentBalances": {"totalCash": 1000.0, "longNonMarginableMarketValue": 50.0}}
    assert schwab_pnl(1, held, AS_OF).equity_now == 1050.0


def test_a_read_that_yielded_to_the_orders_is_not_a_failed_read_and_an_extra_read_never_counts_failures(linked):
    from hokma.kit.read_budget import ReadYielded

    ids = schwab_ids(linked)
    notes: list[str] = []

    def yields(*_):
        raise ReadYielded()

    def broken(*_):
        raise RuntimeError("boom")

    reader = reader_of(linked, yields, notes)
    for _ in range(5):
        reader.read_kind("schwab")
    assert reader.reads["schwab"] == 0 and not notes
    assert reader.board.view(ids[0], "schwab")["state"] == "pending"                          # nothing failed
    reader = reader_of(linked, broken, notes)
    for _ in range(5):
        reader.read_kind("schwab", periodic=False)
    assert not notes and reader.board.view(ids[0], "schwab")["state"] == "pending"
    for _ in range(3):
        reader.read_kind("schwab")                                                            # the periodic read does count
    assert len(notes) == 2


def test_the_simulator_ledger_is_walked_again_only_when_it_changed(ready, monkeypatch):
    from hokma.backoffice import book as book_module

    walks = []
    real = book_module.entries_in
    import sys

    def spy(*a, **k):
        if sys._getframe(1).f_code.co_name == "_sim_book":                       # the walk of the reader (the simulator walks its own ledger too)
            walks.append(1)
        return real(*a, **k)

    monkeypatch.setattr(book_module, "entries_in", spy)
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "c1")
    quote(ready, NQ, "20990")
    first = pnl.read_sim(ready.db, [ready.account_id])[ready.account_id]
    walks.clear()
    again = pnl.read_sim(ready.db, [ready.account_id])[ready.account_id]
    assert not walks and (again.equity_start, again.day_pnl) == (first.equity_start, first.day_pnl)
    fill(ready, ready.account_id, ready.desk_id, NQ, "sell", 1, 21010, "c2")
    walks.clear()
    pnl.read_sim(ready.db, [ready.account_id])
    assert len(walks) == 1


def test_the_stream_is_asked_for_once_and_the_reader_of_the_pnl_is_its_only_owner(linked):
    import asyncio

    calls = []

    class Hub:
        async def watch_activity(self, kind, on):
            calls.append((kind, on))
            return True

    from hokma.connections import runtime

    runtime_hub = runtime.get_hub
    runtime.get_hub = lambda: Hub()
    try:
        mine = pnl.start(linked)

        async def passes():
            for _ in range(4):
                await pnl._follow_stream(mine)

        asyncio.run(passes())
    finally:
        runtime.get_hub = runtime_hub
    assert calls == [("schwab", True)] and pnl.wants_activity("schwab")
    pnl.stop()
    assert not pnl.wants_activity("schwab")


def test_reads_that_keep_giving_way_after_their_pause_tell_the_owner_once_after_60_seconds(linked, monkeypatch):
    from hokma.kit.read_budget import ReadYielded

    clock, notes = Clock(), []
    state = {"yield": True, "paused": True}
    monkeypatch.setattr(pnl, "reads_paused", lambda kind: state["paused"])

    def source(database, declared=None):
        if state["yield"]:
            raise ReadYielded()
        return pnl.read_schwab(database, declared)

    reader = PnlReader(linked, None, notify=notes.append, schwab=source, clock=clock, spawn=lambda fn: fn(), later=lambda delay, fn: None)
    for _ in range(20):                                                                    # a pause (a 429) is the normal rest: 100 seconds of it say nothing
        reader.read_kind("schwab")
        clock.now += 5
    assert notes == []
    state["paused"] = False                                                                # the pause is over and the reads still give way
    for _ in range(11):                                                                    # 50 seconds: not yet
        reader.read_kind("schwab")
        clock.now += 5
    assert notes == []
    for _ in range(4):                                                                     # past 60 seconds: one notice, however long it lasts
        reader.read_kind("schwab")
        clock.now += 5
    for _ in range(10):
        reader.read_kind("schwab")
        clock.now += 5
    assert len(notes) == 1 and "60" in notes[0]
    state["yield"] = False                                                                 # a read goes through: the count starts again
    reader.read_kind("schwab")
    state["yield"] = True
    for _ in range(16):
        reader.read_kind("schwab")
        clock.now += 5
    assert len(notes) == 2
