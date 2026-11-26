"""The P&L of the broker for Alpaca (SPEC-026 v2, piece 3390e1b): the mapping of `GET /v2/account` and `GET /v2/positions`, the two calls of a read inside a
budget of their own, a 429 that makes the reader rest, the identity of the account, A12 (a loss rule in restriction is accepted for Alpaca and still refused for IBKR),
and the anonymous sample.

The Alpaca answers here are PROVISIONAL: they have the exact structure of the documentation (the numbers of its examples), and
alpaca_fixtures/pnl/README.md says how the real capture replaces them. The same checks run on `account_positions.real.json` when that file exists.
Hokma has no stream of trade_updates, so the only trigger of a read is the clock of 5 seconds: there is no extra read on a fill and nothing here tests one."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from alpaca.trading.client import TradingClient
from broker import harnesses_connectors as hc
from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db  # noqa: F401  (fixture)
from exec_support import ready  # noqa: F401  (fixture)
from test_broker_schwab_accounts import connections, keys  # noqa: F401  (fixtures)
from test_desk_rules import OWNER

from hokma.broker import account_rules, alpaca as alpaca_module, alpaca_sample, pnl
from hokma.broker.pnl import BrokerPnl, PnlBoard, PnlReader, alpaca_pnl, latest_pnl
from hokma.broker.service import BrokerError
from hokma.i18n import t, use_language
from hokma.kit.read_budget import ReadBudget

FIXTURES = Path(__file__).parent / "alpaca_fixtures" / "pnl"
PROVISIONAL = FIXTURES / "account_positions.provisional.json"
REAL = FIXTURES / "account_positions.real.json"
CASES = [pytest.param(PROVISIONAL, id="provisional")] + ([pytest.param(REAL, id="real")] if REAL.exists() else [])
AS_OF = "2026-11-25T15:00:00Z"
ACCOUNT_UUID = "e6fe16f3-64a4-4921-8928-cadf02f92f98"


def wire(path: Path) -> tuple[dict, list]:
    """The account and the positions as Alpaca sends them. The sample writes the account number and id as XXXX; the SDK needs an id that is a UUID and the
    connector needs the number it linked, so the test puts those two back (nothing else is touched)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return {**data["account"], "account_number": hc.ALPACA_SENTINEL, "id": ACCOUNT_UUID}, data["positions"]


def models(path: Path):
    from alpaca.trading.models import Position, TradeAccount

    account, positions = wire(path)
    return TradeAccount(**account), [Position(**p) for p in positions], account, positions


def number(value) -> float | None:
    return None if value is None else float(value)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(pnl, "_board", PnlBoard())
    monkeypatch.setattr(pnl, "_budgets", {})
    pnl.stop()
    yield
    pnl.stop()
    alpaca_module.set_client_factory(None)


@pytest.fixture
def linked(desks_db, connections):
    """Alpaca connected with the recorded answers: the broker row and its account exist and are linked."""
    session = hc.alpaca_install(routes_of(PROVISIONAL))
    alpaca_module.sync_accounts(db=desks_db)
    return desks_db, session


def routes_of(path: Path) -> dict:
    account, positions = wire(path)
    return {("GET", "/account"): (200, account), ("GET", "/positions"): (200, positions)}


# ---------------------------------------------------------------------------------------------------- the mapping


@pytest.mark.parametrize("path", CASES)
def test_each_field_comes_from_the_broker_and_nothing_is_calculated_by_hokma(path):
    account, positions, raw_account, raw_positions = models(path)
    out = alpaca_pnl(7, account, positions, AS_OF)
    assert (out.equity_now, out.equity_start) == (number(raw_account["equity"]), number(raw_account["last_equity"]))
    assert out.day_pnl == pytest.approx(float(raw_account["equity"]) - float(raw_account["last_equity"]))
    assert out.source == "alpaca" and out.as_of == AS_OF and out.usable
    assert len(out.positions) == len(raw_positions)
    for seen, raw in zip(out.positions, raw_positions):
        assert seen.symbol == raw["symbol"]
        assert seen.day_pl == number(raw.get("unrealized_intraday_pl"))
        assert seen.open_pl == number(raw.get("unrealized_pl"))
        assert seen.qty == (-abs(float(raw["qty"])) if raw["side"] == "short" else abs(float(raw["qty"])))


def test_the_provisional_numbers_are_the_ones_of_the_documentation():
    account, positions, *_ = models(PROVISIONAL)
    out = alpaca_pnl(7, account, positions, AS_OF)
    assert (out.equity_now, out.equity_start) == (103820.56, 103529.24) and out.day_pnl == pytest.approx(291.32)
    aapl, msft, tsla = out.positions
    assert (aapl.qty, aapl.day_pl, aapl.open_pl) == (5.0, 10.0, 100.0)
    assert (msft.qty, msft.day_pl, msft.open_pl) == (2.0, -0.98, -0.98)
    assert (tsla.qty, tsla.day_pl, tsla.open_pl) == (-5.0, None, 50.0)                     # a short; what Alpaca does not give is None, never 0


@pytest.mark.parametrize("last_equity", [None, "0", "-5", "", "n/a"])
def test_a_start_that_alpaca_does_not_give_is_not_a_start_unless_the_owner_declared_one(last_equity):
    account, positions, *_ = models(PROVISIONAL)
    from alpaca.trading.models import TradeAccount

    account = TradeAccount.model_construct(**{**account.model_dump(), "last_equity": last_equity})
    unknown = alpaca_pnl(7, account, positions, AS_OF)
    assert unknown.equity_now == 103820.56 and unknown.equity_start is None and unknown.day_pnl is None and not unknown.usable
    declared = alpaca_pnl(7, account, positions, AS_OF, declared=lambda: 103500.0)
    assert declared.equity_start == 103500.0 and declared.day_pnl == pytest.approx(320.56) and declared.usable


def test_an_account_with_no_equity_has_no_day_pnl():
    from alpaca.trading.models import TradeAccount

    account, positions, *_ = models(PROVISIONAL)
    broken = TradeAccount.model_construct(**{**account.model_dump(), "equity": None})
    out = alpaca_pnl(7, broken, positions, AS_OF)
    assert out.equity_now is None and out.day_pnl is None and not out.usable


def test_the_reading_never_carries_the_account_number():
    account, positions, *_ = models(PROVISIONAL)
    out = alpaca_pnl(7, account, positions, AS_OF)
    assert hc.ALPACA_SENTINEL not in json.dumps(out.as_dict()) and ACCOUNT_UUID not in json.dumps(out.as_dict())


# ---------------------------------------------------------------------------------------------------- the read: two calls, a budget of its own


def test_a_read_is_two_get_calls_and_nothing_else_and_nothing_comes_from_the_book(linked):
    db, session = linked
    before = len(session.calls)
    found = pnl.read_alpaca(db)
    calls = session.calls[before:]
    assert [(m, p) for m, p, _ in calls] == [("GET", "/account"), ("GET", "/positions")]
    (account_id, reading), = found.items()
    assert reading.source == "alpaca" and reading.day_pnl == pytest.approx(291.32) and reading.as_of.endswith("Z")
    assert pnl.alpaca_accounts_of(db) == [account_id]
    with db.read() as u:
        assert u.session.one("SELECT COUNT(*) AS n FROM book_entries")["n"] == 0


def test_the_client_is_built_once_and_not_on_every_read(desks_db, connections):
    built = []
    session = hc.RecordedSession(routes_of(PROVISIONAL))

    def factory(broker) -> TradingClient:
        built.append(1)
        client = TradingClient(api_key="test-key-id", secret_key="test-secret", paper=True)
        client._session = session
        return client

    alpaca_module.set_client_factory(factory)
    alpaca_module.sync_accounts(db=desks_db)
    built.clear()
    for _ in range(3):
        assert pnl.read_alpaca(desks_db)
    assert len(built) == 1


def test_the_budget_counts_a_sliding_minute_and_pauses_only_the_reads_after_a_429():
    clock = {"now": 100.0}
    budget = ReadBudget(lambda: clock["now"], per_minute=2, reserve=0)
    assert budget.admit(0, 1, False) and budget.admit(0, 1, False) and not budget.admit(0, 1, False)      # 2 reads fit, the third does not
    clock["now"] += 61
    assert budget.admit(0, 1, False)                                                       # the minute slid
    budget.pause(30)
    clock["now"] += 10
    assert not budget.admit(0, 1, False)                                                   # paused
    clock["now"] += 21
    assert budget.admit(0, 1, False)


def test_the_cadence_of_the_reader_fits_the_budget_with_room_for_the_orders():
    per_minute = 60.0 / pnl.READ_SECONDS
    assert per_minute <= pnl.ALPACA_READS_PER_MIN and pnl.ALPACA_READS_PER_MIN * 2 <= 40       # the rest of the quota of the account is left to the orders


def test_a_read_without_budget_calls_nobody_and_is_skipped_not_failed(linked):
    db, session = linked
    spent = ReadBudget(time.monotonic, per_minute=0, reserve=0)
    before, skipped = len(session.calls), set()
    assert pnl.read_alpaca(db, skipped=skipped, budgets=lambda row_id: spent) == {}
    assert len(session.calls) == before and skipped == set(pnl.alpaca_accounts_of(db))


def test_budget_and_429_never_count_toward_no_data_only_real_failures_do(linked):
    db, session = linked
    notes: list[str] = []
    (account_id,) = pnl.alpaca_accounts_of(db)
    clock = {"now": 100.0}
    budget = ReadBudget(lambda: clock["now"], per_minute=20, reserve=0)
    reader = PnlReader(db, None, notify=notes.append, schwab=lambda *a, **k: {}, sim=lambda *a, **k: {},
                       alpaca=lambda database, declared=None, skipped=None: pnl.read_alpaca(database, declared, skipped, budgets=lambda row_id: budget))
    reader.read_kind("alpaca")
    assert latest_pnl(account_id) is not None
    budget.pause(300)                                                                          # no quota: the reader does not read
    for _ in range(5):
        reader.read_kind("alpaca")
    assert reader.board.view(account_id)["state"] == "ok" and notes == []                      # five skipped reads are not five failures
    session.routes["down"] = True
    clock["now"] += 400
    for _ in range(3):
        reader.read_kind("alpaca")
    assert reader.board.view(account_id)["state"] == "no_data" and len(notes) == 1             # real failures do


def test_a_429_makes_the_reader_rest_and_the_next_read_does_not_call(linked):
    db, session = linked

    class TooMany(Exception):
        status_code = 429

    asked = []

    class Client:
        def get_account(self):
            asked.append(1)
            raise TooMany()

        def get_all_positions(self):
            raise AssertionError("not asked after the account failed")

    alpaca_module.set_client_factory(lambda broker: Client())
    before = len(session.calls)
    clock = {"now": 100.0}
    budget = ReadBudget(lambda: clock["now"], per_minute=20, reserve=0)
    skipped: set[int] = set()
    assert pnl.read_alpaca(db, skipped=skipped, budgets=lambda row_id: budget) == {}
    assert len(asked) == 1 and skipped == set(pnl.alpaca_accounts_of(db))                    # a 429 is a rest, not a failure
    clock["now"] += 5
    assert pnl.read_alpaca(db, budgets=lambda row_id: budget) == {}                          # resting: it did not even try
    assert len(asked) == 1
    clock["now"] += pnl.ALPACA_REST + 1
    assert pnl.read_alpaca(db, budgets=lambda row_id: budget) == {} and len(asked) == 2     # the rest is over: it tries again
    assert len(session.calls) == before                                                      # nothing else was called


def test_an_account_that_is_not_the_linked_one_is_not_read_as_it(linked):
    db, session = linked
    other = {**wire(PROVISIONAL)[0], "account_number": "ANOTHER0001"}
    session.routes[("GET", "/account")] = (200, other)
    assert pnl.read_alpaca(db) == {}


def test_a_network_failure_is_a_failed_read_and_a_new_client_is_built(linked):
    db, session = linked
    session.routes["down"] = True
    assert pnl.read_alpaca(db) == {}
    del session.routes["down"]
    assert pnl.read_alpaca(db)                                                                # it reads again


# ---------------------------------------------------------------------------------------------------- the reader: the same state of no data


def test_the_reader_reads_alpaca_with_the_others_and_three_failed_reads_are_no_data(linked):
    db, _ = linked
    ids = pnl.alpaca_accounts_of(db)
    notes: list[str] = []
    state = {"up": True}

    def source(database, declared=None, skipped=None):
        if not state["up"]:
            raise RuntimeError("429")
        return {i: BrokerPnl(i, 990.0, 1000.0, -10.0, (), pnl.iso_now(), "alpaca") for i in ids}

    reader = PnlReader(db, None, notify=notes.append, schwab=lambda *a, **k: {}, alpaca=source, spawn=lambda fn: fn(), later=lambda delay, fn: None)
    reader.read_all()
    assert reader.reads["alpaca"] == 1 and latest_pnl(ids[0]).source == "alpaca"
    assert reader.board.view(ids[0], "alpaca")["state"] == "ok" and reader.board.view(ids[0], "alpaca")["source"] == "alpaca"
    state["up"] = False
    for _ in range(3):
        reader.read_kind("alpaca")
    assert latest_pnl(ids[0]) is None and reader.board.view(ids[0], "alpaca")["state"] == "no_data" and len(notes) == 1


def test_there_is_one_lock_and_one_counter_for_each_kind_and_alpaca_has_no_extra_read():
    reader = PnlReader(None, None)
    assert set(reader.reads) == set(pnl.READERS) and "alpaca" in pnl.READERS
    pnl.on_account_activity("alpaca", "OrderFill")                                          # no reader running: ignored, and nothing in Hokma sends it for Alpaca


# ---------------------------------------------------------------------------------------------------- A12


def test_a12_alpaca_accepts_a_loss_rule_in_restriction_and_ibkr_still_refuses_it_with_the_same_sentence(ready):
    from account_links_support import link_principal

    assert pnl.has_reader("alpaca") and not pnl.has_reader("ibkr") and not pnl.has_reader("crypto")
    with ready.db.write() as u:
        alpaca = u.broker.create_broker("alpaca", "Alpaca")
        a = u.broker.create_account(alpaca.id, "Alpaca paper", 25000)
        ibkr = u.broker.create_broker("ibkr", "IBKR")
        b = u.broker.create_account(ibkr.id, "IBKR paper", 25000)
        link_principal(u, ready.desk_id, a.id)
        link_principal(u, ready.desk_id, b.id)
    account_rules.set_rule(ready.desk_id, a.id, "daily_loss", 100, OWNER, mode="restriction", on_trip="block_entries", db=ready.db)
    for lang in ("es", "en"):
        with use_language(lang):
            with pytest.raises(BrokerError) as e:
                account_rules.set_rule(ready.desk_id, b.id, "daily_loss", 100, OWNER, mode="restriction", on_trip="block_entries", db=ready.db)
            assert e.value.code == "live_freeze_unavailable" and e.value.status == 409
            assert e.value.message == t("pnl.err.unavailable", account=str(b.id), kind="ibkr")


def test_the_screen_view_of_an_alpaca_account_says_pending_then_the_source_alpaca(linked):
    db, _ = linked
    (account_id,) = pnl.alpaca_accounts_of(db)
    assert pnl.views(db)[account_id]["state"] == "pending"
    reader = PnlReader(db, None, notify=lambda text: None, schwab=lambda *a, **k: {}, sim=lambda *a, **k: {})
    reader.read_all()
    view = pnl.views(db)[account_id]
    assert view["state"] == "ok" and view["source"] == "alpaca" and view["last"]["day_pnl"] == pytest.approx(291.32)
    assert hc.ALPACA_SENTINEL not in json.dumps(view)


# ---------------------------------------------------------------------------------------------------- the anonymous sample


def test_the_sample_keeps_the_amounts_and_loses_the_account_number_and_id(linked, tmp_path):
    db, session = linked
    session.routes[("GET", "/account")] = (200, {**wire(PROVISIONAL)[0], "account_number": "PA3KX7782QZ", "id": ACCOUNT_UUID})
    before = len(session.calls)
    result = alpaca_sample.capture(db, folder=tmp_path)
    assert result["ok"] and result["positions"] == 3 and result["file"].startswith(str(tmp_path))
    assert [(m, p) for m, p, _ in session.calls[before:]] == [("GET", "/account"), ("GET", "/positions")]
    text = (tmp_path / result["file"].split("/")[-1]).read_text(encoding="utf-8")
    assert "PA3KX7782QZ" not in text and ACCOUNT_UUID not in text
    document = json.loads(text)
    assert document["provisional"] is False and document["account"]["account_number"] == "XXXX" and document["account"]["id"] == "XXXX"
    assert document["account"]["equity"] == "103820.56" and document["account"]["last_equity"] == "103529.24"
    assert document["positions"][0]["asset_id"] == "904837e3-3b76-47ec-b432-046db621571b"         # the id of an ASSET is public and stays
    assert document["positions"][0]["unrealized_intraday_pl"] == "10.0"


def test_a_sample_that_would_keep_a_personal_value_is_not_written():
    with pytest.raises(RuntimeError):
        alpaca_sample.build({"account_number": "PA3KX7782QZ", "note": "x"}, [{"PA3KX7782QZ": 1}], ["PA3KX7782QZ"])    # a number as the NAME of a field is refused


def test_the_sample_without_an_alpaca_account_says_so_in_both_languages(desks_db, connections, tmp_path):
    target = tmp_path / "out"
    for lang in ("es", "en"):
        with use_language(lang):
            with pytest.raises(BrokerError) as e:
                alpaca_sample.capture(desks_db, folder=target)
            assert e.value.code == "alpaca_not_connected" and e.value.message == t("pnl.err.alpaca_sample_none")
    assert not target.exists()                                                                # nothing was written
