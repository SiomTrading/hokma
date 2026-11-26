"""Execution by slices (SPEC-020: L-54): TWAP, VWAP, standard deviation and spread; every slice carries the parent fingerprint and goes through the filter."""
import json
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest
from exec_support import NQ, SPY, db, desks_db, env, proposal, ready  # noqa: F401  (fixtures)

from hokma.backtest.guards_bridge import SimAccount, SimPosition, blocked_code, check
from hokma.contracts import identity
from hokma.exec import algos
from hokma.exec.algos import AlgoError, allocate, execute, parent_of, run_slices, slice_order
from hokma.exec.proposal import Leg, OrderProposal

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
AAPL = "EQ:AAPL"
CALL_A, CALL_B = "OPT:SPY:2026-12-18:C:600", "OPT:SPY:2026-12-18:C:610"


def parent(contract=NQ, side="buy", qty="10", line="SWO", order_type="market", **kw):
    return OrderProposal(1, line, "desk", (Leg(contract, side, "open", D(qty)),), order_type, created_by="x", thesis=kw.pop("thesis", "why"), **kw)


def qtys(children):
    return [c.legs[0].qty for c in children]


def test_an_order_of_10_contracts_in_twap_of_5_slices_gives_5_children_of_2_that_add_up_to_10_with_the_parent_fingerprint():
    p = parent()
    children = slice_order(p, "twap", n_slices=5)
    assert qtys(children) == [D(2)] * 5 and sum(qtys(children)) == 10
    assert {parent_of(c) for c in children} == {p.fingerprint()}
    assert all(c.fingerprint() != p.fingerprint() for c in children) and parent_of(p) is None
    assert [c.thesis.split("]")[0].split(" ")[:2] for c in children][0] == ["[twap", "1/5"]
    assert all(c.thesis.endswith(" why") and c.legs[0].contract_id == NQ and c.order_type == "market" and c.line == "SWO" for c in children)


def test_the_units_are_shared_whole_and_the_largest_remainders_take_what_is_left():
    assert allocate(D(10), [D(1)] * 3) == (D(4), D(3), D(3))
    assert allocate(D(7), [D(1)] * 5) == (D(2), D(2), D(1), D(1), D(1))
    assert allocate(D(9), [D(10), D(30), D(40), D(20)]) == (D(1), D(3), D(3), D(2))        # 0.9 2.7 3.6 1.8: floors 0 2 3 1, the three left go to .9 .8 .7
    assert allocate(D("2.5"), [D(1), D(1)]) == (D("1.3"), D("1.2")) and sum(allocate(D("2.5"), [D(1)] * 3)) == D("2.5")
    with pytest.raises(AlgoError):
        allocate(D(5), [D(0), D(0)])
    assert qtys(slice_order(parent(qty="2"), "twap", n_slices=5)) == [D(1), D(1)]         # a slice with nothing is left out: 2 children, not 5
    assert qtys(slice_order(parent(qty="10"), "twap", n_slices=3)) == [D(4), D(3), D(3)]


def test_vwap_follows_the_volume_profile_it_is_given():
    children = slice_order(parent(qty="10"), "vwap", profile=[10, 30, 40, 20])
    assert qtys(children) == [D(1), D(3), D(4), D(2)]
    for bad in ([], None, [0, 0], [-1, 5], [1, "x"], "abc"):
        with pytest.raises(AlgoError):
            slice_order(parent(), "vwap", profile=bad)


def test_stdev_places_limit_slices_on_the_bands_around_the_mean_on_the_tick_grid():
    p = parent(AAPL, qty="900", line="LP")
    buys = slice_order(p, "stdev", mean=200, std=5, bands=[D("0.5"), 1, D("1.5")], tick="0.01")
    assert [(c.order_type, c.limit_price, c.legs[0].qty) for c in buys] == [("limit", D("197.5"), D(300)), ("limit", D(195), D(300)), ("limit", D("192.5"), D(300))]
    sells = slice_order(parent(AAPL, "sell", "900", "LP"), "stdev", mean=200, std=5, bands=[D("0.5"), 1, D("1.5")], tick="0.01")
    assert [c.limit_price for c in sells] == [D("202.5"), D(205), D("207.5")]
    rounded = slice_order(p, "stdev", mean=200, std="3.3", bands=[1], tick="0.25")           # 196.7 on a grid of 0.25: a buy goes down to 196.5
    assert rounded[0].limit_price == D("196.5") and slice_order(parent(AAPL, "sell", "9", "LP"), "stdev", mean=200, std="3.3", bands=[1], tick="0.25")[0].limit_price == D("203.5")
    for bad in (dict(mean=200, std=0, bands=[1]), dict(mean=200, std=5, bands=[]), dict(mean=200, std=5, bands=[-1]), dict(mean=1, std=5, bands=[1])):
        with pytest.raises(AlgoError):
            slice_order(p, "stdev", **bad)


def test_spread_walks_from_the_passive_side_to_the_far_side_of_the_quote():
    p = parent(AAPL, qty="400", line="LP")
    buys = slice_order(p, "spread", n_slices=4, bid="99.90", ask="100.10", tick="0.01")
    assert [c.limit_price for c in buys] == [D("99.90"), D("99.96"), D("100.03"), D("100.10")]       # 99.9667 and 100.0333 round down: a buy never pays more
    sells = slice_order(parent(AAPL, "sell", "400", "LP"), "spread", n_slices=4, bid="99.90", ask="100.10", tick="0.01")
    assert [c.limit_price for c in sells] == [D("100.10"), D("100.04"), D("99.97"), D("99.90")]
    assert slice_order(p, "spread", n_slices=1, bid="99.90", ask="100.10")[0].limit_price == D("100.10")    # one slice crosses
    with pytest.raises(AlgoError):
        slice_order(p, "spread", n_slices=2, bid=101, ask=100)
    with pytest.raises(AlgoError):
        slice_order(parent(AAPL, order_type="stop", stop_price=D(150), line="LP"), "spread", n_slices=2, bid=99, ask=100)


def test_the_legs_of_an_options_order_keep_their_ratio_and_a_naked_leg_is_never_made():
    two = OrderProposal(1, "SWO", "desk", (Leg(CALL_A, "buy", "open", D(10)), Leg(CALL_B, "sell", "open", D(10))), "limit", limit_price=D("2.5"), created_by="x")
    children = slice_order(two, "twap", n_slices=5)
    assert [[g.qty for g in c.legs] for c in children] == [[D(2), D(2)]] * 5 and all(c.limit_price == D("2.5") for c in children)
    uneven = OrderProposal(1, "SWO", "desk", (Leg(CALL_A, "buy", "open", D(10)), Leg(CALL_B, "sell", "open", D(3))), "market", created_by="x")
    with pytest.raises(AlgoError) as err:
        slice_order(uneven, "twap", n_slices=5)
    assert err.value.code == "bad_algo" and "no quantity" in str(err.value)


def test_the_risk_of_the_parent_is_shared_with_the_slices_and_never_understated():
    p = parent(risk_usd=D(1000), stop_loss=D(20950))
    children = slice_order(p, "twap", n_slices=5)
    assert [c.risk_usd for c in children] == [D("200.00")] * 5 and all(c.stop_loss == D(20950) for c in children)
    thirds = slice_order(parent(risk_usd=D(1000)), "twap", n_slices=3)                        # 4, 3 and 3 contracts of 10
    assert [c.risk_usd for c in thirds] == [D("400.00"), D("300.00"), D("300.00")]


def test_what_the_caller_asks_is_checked():
    p = parent()
    for bad in (0, 101, True, 2.5, "3", None):
        with pytest.raises(AlgoError):
            slice_order(p, "twap", n_slices=bad)
    assert qtys(slice_order(p, "twap", n_slices=2.0)) == [D(5), D(5)]
    with pytest.raises(AlgoError):
        slice_order(p, "iceberg", n_slices=2)
    with pytest.raises(AlgoError):
        slice_order("buy ten", "twap", n_slices=2)
    assert algos.ALGOS == ("twap", "vwap", "stdev", "spread") and AlgoError.code == "bad_algo"


# --- fail closed: a slice that is stopped stops the line ------------------------------------------------------------------------------

def test_a_slice_that_the_filter_blocks_stops_the_next_ones_from_leaving():
    children = slice_order(parent(), "twap", n_slices=5)
    sent = []

    def gate(child):
        sent.append(child)
        return ("refused", "max_position_pct") if len(sent) == 3 else ("filled", "ok")

    out = run_slices("fp", "twap", children, gate)
    assert len(sent) == 3 and out.emitted == 3 and out.halted_at == 3 and out.halt_code == "max_position_pct" and not out.complete
    assert [o.status for o in out.outcomes] == ["filled", "filled", "refused"] and out.planned == 5
    assert run_slices("fp", "twap", children, lambda c: ("awaiting_owner", "awaiting_owner")).complete
    for ended in ("rejected_by_broker", "expired", "canceled"):
        assert run_slices("fp", "twap", children, lambda c, s=ended: (s, "x")).halted_at == 1
    waits = []
    run_slices("fp", "twap", children, lambda c: ("sent", "ok"), wait=waits.append)
    assert waits == [2, 3, 4, 5]                                                                # the caller schedules: before every slice but the first


def test_the_backtest_simulates_the_same_slices_judged_by_the_guards_of_the_filter():
    # 5000 shares in 5 slices of 1000 (200000, 20 percent of 1000000 each); the ceiling is 50 percent: the third slice would make 60
    sheets = {AAPL: identity.resolve(AAPL)}
    p = parent(AAPL, qty="5000", line="LP")
    state = {"account": SimAccount(D(1_000_000), D(1_000_000), D(1_000_000))}

    def gate(child):
        acct = state["account"]
        result = check(child, acct, limits={"max_position_pct": 50}, sheets=sheets, marks={AAPL: D(200)}, now=NOW, capital=D(1_000_000))
        if result.verdict == "BLOCK":
            return "refused", blocked_code(result)
        qty = child.legs[0].qty
        held = sum((x.qty for x in acct.positions), D(0)) + qty                       # the simulated fill grows the position
        state["account"] = SimAccount(acct.cash, acct.equity, acct.peak_equity, positions=(SimPosition(AAPL, held, D(1), held * 200),))
        return "filled", "ok"

    out = run_slices(p.fingerprint(), "twap", slice_order(p, "twap", n_slices=5), gate)
    assert (out.emitted, out.halted_at, out.halt_code) == (3, 3, "max_position_pct")
    assert [o.child.legs[0].qty for o in out.outcomes] == [D(1000)] * 3 and {parent_of(o.child) for o in out.outcomes} == {p.fingerprint()}


# --- the submitting part: every slice goes through the door -------------------------------------------------------------------------------

def test_every_slice_goes_through_the_door_with_the_parent_fingerprint_and_a_blocked_slice_stops_the_rest(ready):
    with ready.db.write() as u:
        u.broker.set_limits(ready.desk_id, {"max_position_pct": 50})
    p = proposal(ready, contract=SPY, qty="1500", origin="chat", thesis="buy SPY in three")      # 500 x 600.01 is 30 percent of 1000000; two slices are 60
    out = execute(p, "twap", who=ready.owner, db=ready.db, n_slices=3)
    assert (out.emitted, out.halted_at, out.halt_code, out.planned) == (2, 2, "max_position_pct", 3)
    assert [o.status for o in out.outcomes] == ["filled", "refused"]
    with ready.db.read() as u:
        rows = u.session.query("SELECT proposal_json, status, code FROM exec_proposals ORDER BY id")
    assert [r["status"] for r in rows] == ["filled", "refused"] and rows[1]["code"] == "max_position_pct"
    assert all(json.loads(r["proposal_json"])["thesis"].startswith(f"[twap {i}/3 of {p.fingerprint()}]") for i, r in enumerate(rows, start=1))
    assert len(rows) == 2                                                                      # the third slice never reached the door


def test_the_pure_part_does_not_load_the_door():
    import subprocess
    import sys

    code = ("from hokma.core import processguard; processguard._role = processguard.SERVER\n"
            "import sys, hokma.exec.algos\n"
            "assert 'hokma.exec.submit' not in sys.modules, 'the door was loaded by the pure part'\n"
            "assert 'hokma.broker.service' not in sys.modules, 'the broker service was loaded by the pure part'\n")
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-800:]
