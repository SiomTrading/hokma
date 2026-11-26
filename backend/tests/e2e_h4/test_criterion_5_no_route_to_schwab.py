"""Criterion 5 of SPEC-020, second half: no route reaches the Schwab connector without passing the pre-trade filter. With the filter replaced by one that
blocks, neither the native road of the service nor `exec.submit` nor the slices of an algo send anything to Schwab, and the backtest tools never touch it at
all; with the real filter the same calls do send, so the blocked result is the filter's and not an accident of the setup. Schwab is the fake server of H2."""
from decimal import Decimal as D

import pytest
from backtest import support
from backtest.runner_support import NQ, Fixed, source_of, spec
from backtest.support import requires_engine
from broker_orders_support import calls, connections, db, desks_db, env, fake, keys, limit, ready, sent, server  # noqa: F401
from e2e_h4.support import SPACE, bars, caller, ok, run_args, use_services

from hokma import exec as hexec
from hokma.backtest import guards_bridge, runner
from hokma.broker import service
from hokma.broker.service import BrokerError, Who, owner_who
from hokma.exec import algos, prefilter
from hokma.exec.guards import blocked
from hokma.exec.guards.pipeline import PrefilterResult
from hokma.exec.proposal import Leg, OrderProposal

AGENT = Who(agent="desk-chief")
AAPL = "EQ:AAPL"


def proposal(desk_id, qty="1", origin="chat"):
    return OrderProposal(desk_id, "DT", origin, (Leg(AAPL, "buy", "open", D(qty)),), "limit", limit_price=D("10.00"), thesis="proof", created_by="owner")


@pytest.fixture
def desk(ready):
    with ready.db.write() as u:
        u.session.insert_row("desk_profile", {"desk_id": ready.desk_id, "line": "DT"})
    return ready


def close_the_filter(monkeypatch, *, when=lambda p: True):
    """The filter of the door answers BLOCK for the proposals that `when` accepts and is the real one for the rest. Returns the proposals it saw."""
    seen = []

    def closed(desk_id, p, **kw):
        seen.append(p)
        if not when(p):
            return real(desk_id, p, **kw)
        return PrefilterResult("BLOCK", p, (blocked("proof", "closed_for_the_proof", "the filter is closed"),), (), False, True)

    real = prefilter.run
    monkeypatch.setattr(prefilter, "run", closed)
    return seen


def test_with_the_real_filter_the_same_calls_do_reach_schwab_so_the_blocked_result_below_is_the_filters(desk):
    assert service.place(desk.desk_id, limit("BUY", 1, "10.00"), "control", AGENT, db=desk.db)["status"] == "sent"
    assert len(sent(desk.fake)) == 1
    out = hexec.submit(proposal(desk.desk_id), who=owner_who(), db=desk.db)
    assert out.status in ("sent", "filled", "awaiting_owner") and out.code != "closed_for_the_proof"
    assert len(sent(desk.fake)) == 2


def test_a_closed_filter_stops_the_native_road_and_the_door_and_nothing_goes_to_schwab(desk, monkeypatch):
    seen = close_the_filter(monkeypatch)
    with pytest.raises(BrokerError) as refused:
        service.place(desk.desk_id, limit("BUY", 1, "10.00"), "attack", AGENT, db=desk.db)
    assert refused.value.code == "closed_for_the_proof" and seen
    out = hexec.submit(proposal(desk.desk_id), who=owner_who(), db=desk.db)
    assert (out.status, out.code) == ("refused", "closed_for_the_proof")
    assert hexec.submit(proposal(desk.desk_id, origin="desk"), who=AGENT, db=desk.db).status == "refused"
    assert sent(desk.fake) == [] and not [c for c in calls(desk.fake) if c.method in ("PUT", "DELETE")]
    with desk.db.read() as u:
        assert u.session.scalar("SELECT COUNT(*) FROM exec_proposals WHERE status IN ('sent', 'filled')") == 0


def test_the_slices_of_an_algo_stop_after_a_blocked_slice_and_only_the_slices_before_it_went_out(desk, monkeypatch):
    seen = close_the_filter(monkeypatch, when=lambda p: " 2/3 " in p.thesis)               # the first slice passes the real filter, the second is blocked
    out = algos.execute(proposal(desk.desk_id, qty="3"), "twap", who=owner_who(), db=desk.db, n_slices=3)
    assert (out.emitted, out.halted_at, out.planned) == (2, 2, 3) and out.halt_code == "closed_for_the_proof"
    assert not any(" 3/3 " in p.thesis for p in seen)                                        # the third slice never reached the filter
    assert len(sent(desk.fake)) == 1                                                        # the first slice only; the third never reached the door
    with desk.db.read() as u:
        assert [r["status"] for r in u.session.query("SELECT status FROM exec_proposals ORDER BY id")][-1] == "refused"


def test_a_backtest_whose_guards_block_fills_nothing_and_the_connector_is_never_touched(desk, monkeypatch, tmp_path):
    scenario = support.bars_of("T1")
    monkeypatch.setattr(guards_bridge, "check", lambda p, account, **kw: PrefilterResult("BLOCK", p, (blocked("proof", "closed_for_the_proof", "closed"),), (), False, True))
    monkeypatch.setattr(runner.guards_bridge, "check", guards_bridge.check)
    if not runner._lib.available():
        pytest.skip("the backtest engine has no wheel on this platform")
    result = runner.run(spec(scenario, desk_id=desk.desk_id), strategy=Fixed(), source=source_of({NQ: scenario}), db=desk.db, curve_dir=tmp_path)
    assert result.trades == () and result.rejected["closed_for_the_proof"] == 1
    assert calls(desk.fake) == []                                                           # not a single call to Schwab, not even a read


@requires_engine
def test_the_backtest_tools_make_no_call_to_schwab_whatever_the_filter_says(desk, monkeypatch, tmp_path):
    close_the_filter(monkeypatch)
    data = bars(1500)
    use_services(monkeypatch, data, desk.db, tmp_path)
    who = caller("hokma")
    done = ok("backtest_run", run_args(data), who)
    ok("backtest_optimize", run_args(data, space=SPACE, method="grid", n_trials=50, objective="net_pnl"), who)
    ok("backtest_montecarlo", {"run_id": done["run_id"], "n": 100, "method": "placebo", "seed": 1}, who)
    assert calls(desk.fake) == [] and sent(desk.fake) == []
