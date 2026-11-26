"""The back office of a shared account (SPEC-027 piece 027d, task 3335): the result of each desk, the sync, the importers, the reconciliation,
the simulator fills and the risk context when one account serves two desks, and a desk that has no capital. C9 and the rules of section 6."""
import json
from decimal import Decimal as D

import pytest
from backoffice_support import NQ, db, desks_db, env, ready  # noqa: F401
from desks_support import make_desk
from exec_support import AGENT, proposal

from hokma import exec as hexec
from hokma.backoffice import desk_owner, importers, pnl, recon, sync
from hokma.backoffice import book as book_module
from hokma.broker.types import Order
from hokma.desks.agents import risk
from hokma.exec import levels
from account_links_support import unlink_all

START, END = "2000-01-01 00:00:00", "2100-01-01 00:00:00"


def second_desk(r, *, capital=None, share=True):
    """Desk B, born with no capital, in the account of desk A (or in an account of its own)."""
    desk = make_desk(r.db, name="Second desk", capital=capital)
    r.desk_b = desk["id"] if isinstance(desk, dict) else desk.id
    with r.db.write() as u:
        u.session.insert_row("desk_profile", {"desk_id": r.desk_b, "line": "DT"})
        if share:
            account_id = r.account_id
        else:
            account_id = u.broker.create_account(r.broker_id, "Sim B", 500_000).id
        u.broker.link(r.desk_b, account_id)
        u.broker.bind_strategy(r.desk_b, "principal", account_id)
    return r.desk_b


def trade(r, desk_id, side, qty, effect="open", ask=None):
    """One order of a desk through the real path (propose, owner approves, simulated broker fills)."""
    levels.set_level(desk_id, 2, who=r.owner, db=r.db)
    p = proposal(r, side=side, qty=str(qty), effect=effect)
    p = type(p)(**{**p.__dict__, "desk_id": desk_id}) if hasattr(p, "__dict__") else p
    out = hexec.submit(p, who=AGENT, db=r.db)
    assert out.status == "awaiting_owner", (out.status, out.code, out.why)
    done = hexec.approve(out.proposal_id, who=r.owner, db=r.db)
    assert done.status == "filled", (done.status, done.code)


def fills(r, desk_id):
    return [e for e in book_module.entries(r.account_id, db=r.db, kinds=["fill"]) if e.desk_id == desk_id]


@pytest.fixture
def shared(ready):
    second_desk(ready)
    return ready


def test_C9_two_desks_buy_the_same_symbol_in_one_account_and_each_sees_only_its_lots(shared):
    r = shared
    trade(r, r.desk_id, "buy", 1)
    trade(r, r.desk_b, "buy", 2)
    r.board.feed(NQ, bid="21010", ask="21010.25", last="21010.25")
    trade(r, r.desk_id, "sell", 1, effect="close")
    trade(r, r.desk_b, "sell", 1, effect="close")
    a_buy, a_sell = fills(r, r.desk_id)
    b_buys, b_sell = fills(r, r.desk_b)[:1], fills(r, r.desk_b)[1]
    assert (a_buy.qty, a_sell.qty) == (D(1), D(-1)) and (b_buys[0].qty, b_sell.qty) == (D(2), D(-1))
    a, b = pnl.pnl(r.desk_id, start=START, end=END, db=r.db), pnl.pnl(r.desk_b, start=START, end=END, db=r.db)
    assert a.realized == (a_sell.price - a_buy.price) * 20                          # only its own pair of fills, at the multiplier of the contract
    assert b.realized == (b_sell.price - b_buys[0].price) * 20                      # the one it closed; the other lot is still open
    whole = pnl.for_account(r.account_id, start=START, end=END, db=r.db)
    assert a.realized + b.realized == whole.realized and a.commissions + b.commissions == whole.commissions      # the two desks add up to the account
    assert a.account_id == r.account_id and set(a.by_account) == {r.account_id}
    # the open lot is B's alone: valued at the mark, A has nothing open
    marks = {NQ: D("21020")}
    a_open = pnl.pnl(r.desk_id, start=START, end=END, marks=marks, db=r.db)
    b_open = pnl.pnl(r.desk_b, start=START, end=END, marks=marks, db=r.db)
    assert a_open.unrealized == 0 and b_open.unrealized == (D("21020") - b_buys[0].price) * 1 * 20
    assert pnl.pnl(r.desk_b, start=START, end=END, db=r.db).unrealized is None       # no mark given: not a made up zero


def test_the_result_of_one_strategy_is_its_own(shared):
    r = shared
    with r.db.write() as u:
        u.broker.bind_strategy(r.desk_id, "carry", r.account_id)
    trade(r, r.desk_id, "buy", 1)                                                   # principal
    levels.set_level(r.desk_id, 2, who=r.owner, db=r.db)
    out = hexec.submit(proposal(r, strategy_key="carry"), who=AGENT, db=r.db)
    hexec.approve(out.proposal_id, who=r.owner, db=r.db)
    both = pnl.pnl(r.desk_id, start=START, end=END, marks={NQ: D("21000")}, db=r.db)
    carry = pnl.pnl(r.desk_id, start=START, end=END, strategy_key="carry", marks={NQ: D("21000")}, db=r.db)
    main = pnl.pnl(r.desk_id, start=START, end=END, strategy_key="principal", marks={NQ: D("21000")}, db=r.db)
    assert carry.strategy_key == "carry" and len([e for e in fills(r, r.desk_id)]) == 2
    assert carry.commissions != 0 and carry.commissions + main.commissions == both.commissions
    assert carry.unrealized + main.unrealized == both.unrealized


def test_fills_with_no_desk_belong_to_a_desk_only_when_it_is_the_only_one_of_the_account(ready):
    r = ready
    book_module.append(r.account_id, db=r.db, kind="dividend", amount=D("40"), source="import", source_ref="DIV1", occurred_at="2026-03-02 10:00:00")
    only = pnl.pnl(r.desk_id, start=START, end=END, db=r.db)
    assert only.other == D("40")                                                    # an old row with no desk, in an account with one desk: as before
    second_desk(r)
    shared_now = pnl.pnl(r.desk_id, start=START, end=END, db=r.db)
    assert shared_now.other == 0 and pnl.pnl(r.desk_b, start=START, end=END, db=r.db).other == 0      # two desks: it stays without desk
    assert pnl.for_account(r.account_id, start=START, end=END, db=r.db).other == D("40")             # the account still has it


def test_a_desk_with_two_accounts_reports_each_one_and_the_total(shared):
    r = shared
    other = second_desk(r, share=False)
    with r.db.write() as u:
        u.broker.link(r.desk_id, u.broker.accounts_of_desk(other)[0].id)
    trade(r, r.desk_id, "buy", 1)
    report = pnl.pnl(r.desk_id, start=START, end=END, db=r.db)
    assert report.account_id is None and set(report.by_account) == {r.account_id, u_account(r, other)}
    narrow = pnl.pnl(r.desk_id, start=START, end=END, account_id=r.account_id, db=r.db)
    assert narrow.commissions == report.commissions and narrow.account_id == r.account_id
    with pytest.raises(ValueError):
        pnl.pnl(r.desk_b, start=START, end=END, account_id=r.account_id, db=r.db)    # desk B is not in that account in this test


def u_account(r, desk_id):
    with r.db.read() as u:
        return u.broker.accounts_of_desk(desk_id)[0].id


def test_a_desk_without_account_has_an_empty_result_not_an_error(ready):
    with ready.db.write() as u:
        unlink_all(u, ready.desk_id)
    report = pnl.pnl(ready.desk_id, start=START, end=END, db=ready.db)
    assert report.account_id is None and report.net == 0 and report.by_account == {}


# ---------------------------------------------------------------------------------------------------------- the simulator and the importers

def test_the_simulator_books_a_fill_with_the_desk_of_the_order_even_when_the_account_is_shared(shared):
    r = shared
    trade(r, r.desk_b, "buy", 1)
    entries = book_module.entries(r.account_id, db=r.db, kinds=["fill", "commission"])
    assert entries and {e.desk_id for e in entries} == {r.desk_b}                    # not "the first desk of the account"
    with r.db.read() as u:
        stored = json.loads(u.session.one("SELECT request_json FROM sim_orders")["request_json"])
    assert stored["_owner"]["desk_id"] == r.desk_b


def test_a_working_order_that_fills_later_keeps_the_desk_that_sent_it(shared):
    from hokma.broker import sim

    r = shared
    levels.set_level(r.desk_b, 3, rules={"id": "r", "max_qty": 5, "order_types": ["limit"]}, who=r.owner, db=r.db)
    p = proposal(r, order_type="limit", limit="20990", stop_loss=D("20980"))
    out = hexec.submit(type(p)(**{**p.__dict__, "desk_id": r.desk_b}), who=AGENT, db=r.db)
    assert out.status == "sent" and fills(r, r.desk_b) == []
    r.board.listeners.append(sim.on_quote)
    r.board.feed(NQ, bid="20989", ask="20989.5", last="20989.5")                     # another thread fills it later
    assert len(fills(r, r.desk_b)) == 1 and fills(r, r.desk_id) == []


STUDENT_ROW = "date,type,symbol,quantity,price,commission,amount,currency,reference,note\n2026-03-02,buy,AAPL,10,200.00,1.00,,USD,%s,\n"


def test_an_import_goes_to_the_only_desk_of_the_account_and_to_nobody_when_it_is_shared(ready):
    r = ready
    importers.import_into(r.account_id, "student", STUDENT_ROW % "ALONE", db=r.db)
    assert {e.desk_id for e in book_module.entries(r.account_id, db=r.db, kinds=["fill"])} == {r.desk_id}      # as before: the desk of the account
    second_desk(r)
    importers.import_into(r.account_id, "student", STUDENT_ROW % "SHARED", db=r.db)
    by_ref = {e.source_ref: e.desk_id for e in book_module.entries(r.account_id, db=r.db, kinds=["fill"])}
    assert by_ref["import:ALONE"] == r.desk_id and by_ref["import:SHARED"] is None                      # shared: the line has no desk
    importers.import_into(r.account_id, "student", STUDENT_ROW % "NAMED", desk_id=r.desk_b, db=r.db)    # the caller may name the desk
    assert {e.source_ref: e.desk_id for e in book_module.entries(r.account_id, db=r.db, kinds=["fill"])}["import:NAMED"] == r.desk_b
    with r.db.write() as u:
        u.broker.unbind_strategy(r.desk_b, "principal")
        u.broker.unlink(r.desk_b, r.account_id)
        assert desk_owner.sole_desk(u.session, r.account_id) == r.desk_id


# ---------------------------------------------------------------------------------------------------------- sync

def test_the_sync_books_the_fills_of_each_order_to_the_desk_that_sent_it_in_a_shared_account(shared):
    r = shared
    with r.db.write() as u:
        broker = u.broker.create_broker("schwab", "Schwab shared", {"orders_enabled": False})
        account = u.broker.create_account(broker.id, "Real shared", 1_000_000)
        u.broker.link(r.desk_id, account.id)
        u.broker.link(r.desk_b, account.id)
        for oid, desk in (("2001", r.desk_id), ("2002", r.desk_b)):
            record = u.broker.record_order(broker_kind="schwab", action="place", desk_id=desk, account_id=account.id, request={"o": oid}, rule_result="auto",
                                           approved_by="owner", broker_order_id=oid, strategy_key="principal")
            u.session.insert("exec_proposals", {"desk_id": desk, "line": "DT", "origin": "desk", "level": 3, "proposal_json": json.dumps({"o": oid}),
                                                "fingerprint": oid.rjust(64, "0"), "status": "sent", "approved_fingerprint": oid.rjust(64, "0"),
                                                "approved_by": "rule:r", "created_by": "desk-chief", "broker_order_record_id": record.id})

    def filled(oid, qty):
        legs = ({"symbol": "AAPL", "asset_type": "EQUITY", "instruction": "BUY", "position_effect": None, "quantity": qty},)
        act = [{"executionType": "FILL", "executionLegs": [{"legId": 1, "quantity": qty, "price": 200.0, "time": "2026-03-02T14:35:00+0000"}]}]
        return Order(1, "AAPL", "BUY", qty, order_id=oid, status="FILLED", filled_qty=qty, legs=legs, data={"orderActivityCollection": act})

    class Fake:
        def orders(self, account_id, **f): return [filled("2001", 10.0), filled("2002", 30.0)]
        def positions(self, account_id): return []
        def get_order(self, account_id, order_id): return None

    sync.sync(account.id, broker=Fake(), db=r.db)
    by_desk = {e.desk_id: e.qty for e in book_module.entries(account.id, db=r.db, kinds=["fill"])}
    assert by_desk == {r.desk_id: D(10), r.desk_b: D(30)}
    with r.db.read() as u:
        mine = desk_owner.owners(u.session, account.id, book_module.entries_in(u.session, account.id))
    assert {v for v in mine.values()} == {(r.desk_id, "principal"), (r.desk_b, "principal")}
    with pytest.raises(sync.SyncError) as e:                                        # two accounts: it never picks the first
        with r.db.write() as u:
            u.broker.link(r.desk_id, u.broker.create_account(r.broker_id, "Sim C", 500_000).id)
        sync.sync_desk(r.desk_id, db=r.db)
    assert "say which" in str(e.value)


# ---------------------------------------------------------------------------------------------------------- reconciliation of the capital

def test_the_capital_of_a_shared_account_is_counted_once_and_a_desk_without_capital_does_not_break_it(shared):
    r = shared
    with r.db.read() as u:
        assert recon._account_capital(u.session, r.account_id) == D(1_000_000)        # the account's opening amount, not 1,000,000 times two desks
    with r.db.write() as u:
        u.session.execute("UPDATE desks SET capital = NULL")                          # no desk has a number
    same = recon.Statement.from_rows([], as_of="2026-03-10", account_id=r.account_id, account_value="1000000")
    ok = recon.reconcile(r.account_id, statement=same, db=r.db, persist=False)
    assert ok.status == "clean" and ok.summary["nav_tieout"]["status"] == "ok" and ok.summary["nav_tieout"]["account_capital"] == "1000000"
    off = recon.Statement.from_rows([], as_of="2026-03-10", account_id=r.account_id, account_value="1000010")
    bad = recon.reconcile(r.account_id, statement=off, db=r.db, persist=False)
    assert [b.key for b in bad.breaks] == [recon.NAV_KEY] and bad.summary["nav_tieout"]["difference"] == "10"


# ---------------------------------------------------------------------------------------------------------- risk context

def test_the_risk_context_reads_the_lots_of_this_desk_and_the_equity_of_the_account(shared):
    r = shared
    trade(r, r.desk_id, "buy", 1)
    trade(r, r.desk_b, "buy", 2)
    prices = {NQ: D("21000")}
    a = risk.load_portfolio_context(r.desk_id, db=r.db, prices=prices)
    b = risk.load_portfolio_context(r.desk_b, db=r.db, prices=prices)
    assert [p.qty for p in a.positions] == [D(1)] and [p.qty for p in b.positions] == [D(2)]       # nobody reads the other desk's lots as its own
    assert a.equity == b.equity and a.equity is not None                                           # the money is the whole account's
    assert a.shared_with == (r.desk_b,) and b.shared_with == (r.desk_id,) and a.account_id == r.account_id
    facts = a.to_facts()
    assert facts["account_shared"] is True and facts["accounts"][0]["shared"] is True and facts["accounts"][0]["shared_with_desks"] == [r.desk_b]


def test_the_risk_context_of_a_desk_alone_in_its_account_is_what_it_was(ready):
    r = ready
    trade(r, r.desk_id, "buy", 1)
    ctx = risk.load_portfolio_context(r.desk_id, db=r.db, prices={NQ: D("21000")})
    assert [p.qty for p in ctx.positions] == [D(1)] and ctx.shared_with == () and ctx.state is risk.PortfolioState.WITH_POSITIONS


def test_the_risk_context_without_account_is_not_provided_and_with_several_it_asks_for_a_choice(shared):
    r = shared
    with r.db.write() as u:
        unlink_all(u, r.desk_b)
    assert risk.load_portfolio_context(r.desk_b, db=r.db).state is risk.PortfolioState.NOT_PROVIDED
    with r.db.write() as u:
        second = u.broker.create_account(r.broker_id, "Sim X", 500_000).id
        u.broker.link(r.desk_b, second)
        u.broker.link(r.desk_b, r.account_id)
    unbound = risk.load_portfolio_context(r.desk_b, db=r.db)
    assert unbound.state is risk.PortfolioState.NOT_PROVIDED and "several accounts" in unbound.missing["portfolio"] and len(unbound.sections) == 2
    named = risk.load_portfolio_context(r.desk_b, db=r.db, account_id=second)
    assert named.account_id == second and named.state is not risk.PortfolioState.NOT_PROVIDED
    with pytest.raises(risk.ToolFailure):
        risk._read_snapshot(r.desk_b, r.db)                                          # the tool needs account_id when the desk has more than one
    assert risk._read_snapshot(r.desk_b, r.db, second)["shared_with"] == []


# ---------------------------------------------------------------------------------------------------------- the tools of the back office

def tool(name, args):
    from hokma.kit import hokma_tools
    from hokma.kit.hokma_tools import Caller

    text, err = hokma_tools.call(name, args, Caller({"HOKMA_AGENT": "hugo"}))
    return json.loads(text), err


def test_the_tools_name_the_account_when_the_desk_has_two_and_say_who_shares_it(shared):
    r = shared
    trade(r, r.desk_b, "buy", 1)
    snap, err = tool("book_snapshot", {"desk_id": r.desk_id})
    assert not err and snap["shared_with"] == [r.desk_b]                              # one account: no account_id needed; the figures are the whole account's
    mine, err = tool("book_pnl", {"desk_id": r.desk_id, "start": "2020-01-01", "end": "2030-01-01"})
    assert not err and mine["commissions"] == "0" and mine["by_account"][str(r.account_id)]["net"] == "0"      # desk A did not trade: B's fill is not A's result
    theirs, err = tool("book_pnl", {"desk_id": r.desk_b, "start": "2020-01-01", "end": "2030-01-01"})
    assert not err and theirs["commissions"] != "0" and theirs["unrealized"] is not None
    with r.db.write() as u:
        u.broker.link(r.desk_id, u.broker.create_account(r.broker_id, "Sim Y", 300_000).id)
    out, err = tool("book_positions", {"desk_id": r.desk_id})
    assert err and out["error_code"] == "bad_input" and "account_id" in out["error"] if "error" in out else err
    ok, err = tool("book_positions", {"desk_id": r.desk_id, "account_id": r.account_id})
    assert not err and ok["account_id"] == r.account_id
    wrong, err = tool("book_positions", {"desk_id": r.desk_id, "account_id": 9999})
    assert err and wrong["error_code"] == "bad_input"
    with r.db.write() as u:
        unlink_all(u, r.desk_b)
    none, err = tool("book_positions", {"desk_id": r.desk_b})
    assert err and none["error_code"] == "no_data"
