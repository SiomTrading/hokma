"""The loop of a desk with a broker that keeps its positions between steps (SPEC-020: AH-34). DT steps by session, SWC by day, SWO by event and LP by
rebalance; every order passes the filter of H2 through guards_bridge. The bars are the real daily sessions of NQ 12-25 and one real minute session."""
from datetime import date
from decimal import Decimal as D

import pytest
from backtest.session_support import session_bars
from backtest.support import CostModel

from hokma.backtest import runner
from hokma.backtest.runner import StepContext
from hokma.backtest.spec import SpecError
from hokma.exec.proposal import Leg, OrderProposal

from hokma.contracts import identity

NQ = "FUT:NQ:2025-12"          # the contract of the daily bars: the guards of H2 need a dated future
CAPITAL = D(1_000_000)
MODEL = CostModel("schwab")
DAILY = session_bars("daily")


def proposal(line, side, effect="open", qty="1"):
    # a future needs its stop to enter (SPEC-026); far from any price of the bars, so it never changes a fill: the loop does not fill brackets
    stop = None if effect == "close" else (D(1) if side == "buy" else D(1_000_000))
    return OrderProposal(1, line, "desk", (Leg(NQ, side, effect, D(qty)),), "market", stop_loss=stop, created_by="backtest")


def plan(line, buy_at, sell_at, seen=None):
    """A desk strategy in code: buys one contract at step `buy_at` and sells it at step `sell_at`."""
    def strategy(ctx: StepContext):
        if seen is not None:
            seen.append((ctx.index, ctx.at, tuple((p.contract_id, p.qty) for p in ctx.account.positions)))
        if ctx.index == buy_at:
            return [proposal(line, "buy")]
        if ctx.index == sell_at:
            return [proposal(line, "sell", "close")]
        return []
    return strategy


def loop(line, strategy, bars=None, **kw):
    bars = DAILY if bars is None else bars
    return runner.run_desk_loop(line, desk_id=1, strategy=strategy, bars={NQ: bars}, sheets={NQ: identity.resolve(NQ)}, capital=CAPITAL, model=MODEL, **kw)


def test_swc_steps_every_day_and_the_broker_keeps_the_position_between_steps_AH_34():
    seen = []
    result = loop("SWC", plan("SWC", 5, 20, seen))
    assert len(result.steps) == 70 and [s.at for s in result.steps] == [b.ts.date() for b in DAILY]
    # decided at the close of step 5 and filled at the open of the next bar plus a tick; closed from step 20 at the next open less a tick
    entry, exit_ = DAILY[6].open + D("0.25"), DAILY[21].open - D("0.25")
    assert [(f.side, f.price, f.at) for f in result.fills] == [("buy", entry, DAILY[6].ts), ("sell", exit_, DAILY[21].ts)]
    pnl = (exit_ - entry) * 20 - D("4.50")
    assert result.final.equity == CAPITAL + pnl and result.final.positions == () and result.equity.equity_usd[-1] == CAPITAL + pnl
    held = [(i, p) for i, _d, p in seen if p]
    assert [i for i, _ in held] == list(range(6, 21)) and all(p == ((NQ, D(1)),) for _, p in held)        # steps 6 to 20 see the position the broker holds
    assert result.steps[5].filled == 1 and result.steps[20].filled == 1 and result.blocked == {}


def test_dt_steps_by_session_AH_34():
    sessions = session_bars("day")                       # one regular session of minute bars: one step
    result = loop("DT", plan("DT", 0, None), bars=sessions)
    assert len(result.steps) == 1 and result.steps[0].at == date(2025, 11, 10) and result.steps[0].proposals == 1
    assert result.fills == () and result.blocked == {"no_next_bar": 1}                # there is no bar after the last session: nothing is invented
    two = loop("DT", plan("DT", 0, 3), bars=DAILY[:5])                                # five sessions
    assert len(two.steps) == 5 and [f.side for f in two.fills] == ["buy", "sell"]


def test_swo_steps_only_on_the_events_AH_34():
    events = [date(2025, 10, 24), date(2025, 11, 12), date(2025, 11, 15)]            # the last one is a Saturday: its next bar is Monday 2025-11-17
    result = loop("SWO", plan("SWO", 0, 1), events=events)
    assert [s.at for s in result.steps] == [date(2025, 10, 24), date(2025, 11, 12), date(2025, 11, 17)]
    by_date = {b.ts.date(): b for b in DAILY}
    entry = DAILY[[b.ts.date() for b in DAILY].index(date(2025, 10, 27))].open + D("0.25")
    assert [f.price for f in result.fills][0] == entry and [f.at.date() for f in result.fills] == [date(2025, 10, 27), date(2025, 11, 13)]
    assert by_date[date(2025, 11, 13)].open - D("0.25") == result.fills[1].price


def test_lp_steps_on_the_monthly_rebalance_AH_34():
    result = loop("LP", plan("LP", 0, 2))
    assert [s.at for s in result.steps] == [date(2025, 10, 1), date(2025, 11, 3), date(2025, 12, 1)]
    assert [f.at.date() for f in result.fills] == [date(2025, 10, 2), date(2025, 12, 2)]


def test_every_order_of_every_step_passes_the_filter_of_h2_through_the_bridge_AH_34(monkeypatch):
    from hokma.backtest import guards_bridge

    seen = []
    real = guards_bridge.check

    def spy(p, account, **kw):
        seen.append((p.legs[0].effect, account.equity, kw["limits"]))
        return real(p, account, **kw)

    monkeypatch.setattr(runner.guards_bridge, "check", spy)
    result = loop("SWC", plan("SWC", 5, 20), desk_limits={"capital_floor_usd": 1})
    assert [e for e, _eq, _lim in seen] == ["open", "close"] and all(lim == {"capital_floor_usd": 1} for _e, _eq, lim in seen)
    assert len(result.fills) == 2
    # an account under the floor of the desk: the guard of H2 blocks the opening, with its own code, and nothing is filled
    blocked = loop("SWC", plan("SWC", 5, 20), desk_limits={"capital_floor_usd": 2_000_000})
    assert blocked.fills == () and blocked.blocked["capital_floor"] >= 1


def test_the_number_of_steps_is_capped_by_quant_AH_34(monkeypatch):
    from hokma.quant import limits
    from hokma.quant.errors import BadInput

    monkeypatch.setitem(limits.PARAM_CAPS, "desk_steps", 10)
    with pytest.raises(BadInput):
        loop("SWC", plan("SWC", 0, 1))                         # 70 daily steps
    assert len(loop("SWC", plan("SWC", 0, 1), as_of=DAILY[8].ts).steps) == 9


def test_the_loop_refuses_what_it_cannot_run_AH_34():
    with pytest.raises(SpecError):
        loop("RF", plan("RF", 0, 1))
    with pytest.raises(SpecError):
        runner.run_desk_loop("SWC", desk_id=1, strategy=plan("SWC", 0, 1), bars={}, sheets={NQ: identity.resolve(NQ)}, capital=CAPITAL, model=MODEL)
    with pytest.raises(SpecError):
        runner.run_desk_loop("SWC", desk_id=1, strategy=plan("SWC", 0, 1), bars={NQ: DAILY}, sheets={}, capital=CAPITAL, model=MODEL)

    def other_desk(ctx):
        return [OrderProposal(2, "SWC", "desk", (Leg(NQ, "buy", "open", D(1)),), "market", stop_loss=D(1), created_by="x")] if ctx.index == 0 else []
    with pytest.raises(SpecError):
        loop("SWC", other_desk)
