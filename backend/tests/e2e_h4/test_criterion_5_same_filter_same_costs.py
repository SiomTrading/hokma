"""Criterion 5 of SPEC-020, first half: a strategy that emits `OrderIntent` runs in backtest with the pre-trade filter of H2 applied by `guards_bridge`
and, converted with `to_proposal`, enters `exec.submit` of the simulated broker of H2 without changing the strategy. The two fills use the same table of
costs and the same guard gives the same code in both."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

from backtest import support
from backtest.runner_support import Fixed, NQ, Ticks, source_of, spec
from backtest.support import requires_engine
from exec_support import NQ as LIVE_NQ, db, desks_db, env, ready  # noqa: F401  (fixtures)

from hokma import backtest, exec as hexec
from hokma.backtest import costs, runner
from hokma.backtest.broker_bridge import to_proposal
from hokma.backtest.runner import Services
from hokma.backtest.spec import CostModel
from hokma.broker import fees, service
from hokma.contracts import identity
from hokma.exec import levels
from hokma.factory.strategy import replay_intents

pytestmark = requires_engine
SHEET = identity.resolve(LIVE_NQ)


class Recorded(Fixed):
    """The strategy of the scenario T2: one contract with a stop and a target 5 points away. The contract is its only parameter: the same class runs
    on the continuous series of the backtest and on the contract of the desk, and `emitted` keeps what it asked for."""

    def __init__(self, contract):
        super().__init__(contract=contract, enter_at=2, exit_at=None, sl=Ticks(20), tp=Ticks(20), tag="t2", protective=True)
        self.emitted = []

    def on_bar(self, ctx):
        out = super().on_bar(ctx)
        self.emitted += out
        return out


def services_of(bars, db, tmp_path, strategy):
    return Services(db=db, source=source_of({NQ: bars}), curve_dir=tmp_path, strategy=lambda params: strategy)


def test_the_filter_of_the_desk_blocks_the_backtest_and_the_live_door_with_the_same_code(ready, tmp_path):
    bars = support.bars_of("T1")
    # one NQ at 25637.25 is 51 percent of 1,000,000 (and 42 percent at the 21000.25 of the live board): over a ceiling of 40 percent in both
    free = backtest.run(spec(bars, desk_id=ready.desk_id), services=services_of(bars, ready.db, tmp_path / "a", Fixed(sl=Ticks(2000), protective=True)))
    assert free.realized_pnl_usd == D("100.50") and "max_position_pct" not in free.rejected                  # a desk with no limit lets the trade through
    held = runner.run(spec(bars, desk_id=ready.desk_id), strategy=Fixed(sl=Ticks(2000), protective=True), source=source_of({NQ: bars}), db=ready.db, curve_dir=tmp_path / "b",
                      desk_limits={"max_position_pct": 40})
    assert held.trades == () and held.rejected["max_position_pct"] == 1                                       # the guards of H2, through guards_bridge
    with ready.db.write() as u:
        u.broker.set_limits(ready.desk_id, {"max_position_pct": 40})
    levels.set_level(ready.desk_id, 2, who=ready.owner, db=ready.db)
    proposal = to_proposal(replay_one(LIVE_NQ), desk_id=ready.desk_id, line="DT", broker_kind="sim", sheet=SHEET, price=D("21000.25"))
    live = hexec.submit(proposal, who=ready.owner, db=ready.db)
    assert live.status == "refused" and live.code == "max_position_pct"                                      # the same code, from the same guard
    with ready.db.read() as u:
        assert u.session.scalar("SELECT COUNT(*) FROM sim_fills") == 0


def replay_one(contract):
    """The first intent that the strategy emits on the bars of T2 when it is asked for the contract of the desk, through the dry run of the factory."""
    bars = support.bars_of("T2")
    strategy = Recorded(contract)
    done = replay_intents(strategy, {"open": [float(b.open) for b in bars], "high": [float(b.high) for b in bars], "low": [float(b.low) for b in bars],
                                     "close": [float(b.close) for b in bars], "volume": [float(b.volume) for b in bars]},
                          times=[b.ts + timedelta(minutes=1) for b in bars], contract_id=contract, causal_check=False)
    return done.intents[0][1][0]


def test_the_strategy_is_not_changed_between_the_backtest_and_the_simulated_broker_and_both_fills_use_one_table_of_costs(ready, tmp_path):
    bars = support.bars_of("T2")
    in_backtest = Recorded(NQ)
    result = backtest.run(spec(bars), services=services_of(bars, ready.db, tmp_path, in_backtest))
    (trade,) = result.trades
    assert trade.pnl_usd == D("-114.50") and trade.costs_usd == D("4.50")                                   # the fill of the backtest, 2.25 a side
    asked = in_backtest.emitted[0]
    on_the_desk = replay_one(LIVE_NQ)
    assert type(asked) is type(on_the_desk) and replace(asked, contract_id=LIVE_NQ) == on_the_desk            # the same strategy asks for the same order

    rule = fees.load("schwab").rules["future"]                                                              # the table of the backtest
    service.update_costs(ready.broker_id, commission={"type": "per_share", "value": float(rule.value), "min": 0}, slippage_bps=0, db=ready.db)
    proposal = to_proposal(on_the_desk, desk_id=ready.desk_id, line="DT", broker_kind="sim", sheet=SHEET, price=D("21000.25"))
    assert (proposal.stop_loss, proposal.take_profit, proposal.risk_usd) == (D("20995.25"), D("21005.25"), D(100))
    levels.set_level(ready.desk_id, 2, who=ready.owner, db=ready.db)
    waiting = hexec.submit(proposal, who=ready.owner, db=ready.db)
    assert waiting.status == "awaiting_owner" and waiting.fingerprint == proposal.fingerprint()
    done = hexec.approve(waiting.proposal_id, who=ready.owner, db=ready.db)
    assert done.status == "filled" and done.fingerprint == proposal.fingerprint()
    with ready.db.read() as u:
        (fill,) = u.session.query("SELECT price, commission, multiplier FROM sim_fills")
    per_side = costs.commission_usd(costs.fee_model(CostModel("schwab")), qty=D(1), price=D(fill["price"]), multiplier=D(fill["multiplier"]))
    assert D(fill["commission"]) == per_side == D("2.25") and trade.costs_usd == 2 * per_side                  # the same table, side by side
