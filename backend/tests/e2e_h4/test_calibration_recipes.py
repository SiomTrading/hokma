"""AS-006 and AS-020e of SPEC-020: the recipes that sell premium declare the switch of SPEC-019, run in a backtest, and a desk cannot send them while the switch is
closed. The switch opens only by a calibration that passes and closes again when a recalibration fails (4.9). The chain history is synthetic, see
`backtest/calibration_support.py`."""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from backtest.calibration_support import fair_history, rich_history
from exec_support import AGENT, db, desks_db, env, ready  # noqa: F401  (fixtures)

from hokma import exec as hexec
from hokma.backtest.calibration import gate as gate_mod
from hokma.backtest.calibration import premium
from hokma.exec.guards.premium_selling import switch_state
from hokma.exec.proposal import Leg, OrderProposal
from hokma.factory import ideas
from hokma.factory.alphas.premium_wheel import PremiumWheel
from hokma.factory.registry import Registry

ROOT = Path(__file__).resolve().parents[3]
STRATEGIES = ROOT / "kit" / "template" / "service" / "strategies"
AS_OF = datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc)
GATE = gate_mod.tightened(gate_mod.load_default(), max_drawdown_pct=15.0)          # the desk has no limit of drawdown here (one would arm the monitor of the filter), so the gate carries it
PUT, CALL, PROTECTION = "OPT:SPY:2026-12-18:P:600", "OPT:SPY:2026-12-18:C:600", "OPT:SPY:2026-12-18:P:510"
QQQ_PUT = "OPT:QQQ:2026-12-18:P:500"
MARKS = {PUT: "7.00", CALL: "8.00", PROTECTION: "0.80", QQQ_PUT: "6.00"}


@pytest.fixture
def swo(ready):
    with ready.db.write() as u:
        u.session.update("desk_profile", {"desk_id": ready.desk_id}, {"line": "SWO"})
    for cid, mark in MARKS.items():
        value = Decimal(mark)
        ready.board.feed(cid, bid=str(value - Decimal("0.05")), ask=str(value + Decimal("0.05")), last=mark)
    return ready


def send(swo, *legs, stop=None):
    """What a desk of the line SWO sends to the filter before an order: only the proposal of the agent, no owner. `stop` is the stop that a naked sale needs (SPEC-026)."""
    return hexec.submit(OrderProposal(swo.desk_id, "SWO", "desk", tuple(Leg(c, side, "open", Decimal(1)) for c, side in legs), "market", created_by="desk-chief",
                                      thesis="the recipe", stop_loss=stop), who=AGENT, db=swo.db)


WHEEL_PUT = ((PUT, "sell"),)                                                       # phase one of the wheel: a cash secured put
PROTECTED_STRADDLE = ((CALL, "sell"), (PUT, "sell"), (PROTECTION, "buy"))        # the straddle of the volatility premium recipe, with its protection


def refused(out):
    return (out.status, out.code) == ("refused", "premium_selling_closed")


# ---- the files ---------------------------------------------------------------------------------------------------------------

def test_both_recipes_declare_the_switch_and_cannot_drop_it():
    wheel = yaml.safe_load((STRATEGIES / "premium-wheel.yaml").read_text(encoding="utf-8"))
    (entry,) = [e for e in wheel if e["id"] == "premium_wheel_swo"]
    assert entry["line"] == "SWO" and entry["params"]["requires_premium_switch"] is True
    registry = Registry.with_builtins()
    registry.load_file(STRATEGIES / "premium-wheel.yaml")
    assert registry.build("premium_wheel_swo").requires_premium_switch is True and PremiumWheel.REQUIRES_PREMIUM_SWITCH is True
    with pytest.raises(ValueError, match="premium switch"):
        registry.build("premium_wheel_swo", {"requires_premium_switch": False})
    (idea,) = [i for i in ideas.load_ideas() if i.id == "volatility_risk_premium"]
    assert idea.requires_premium_switch is True and idea.lines == ("SWO",)
    assert any(v.params and v.params.get("requires_premium_switch") is True for v in idea.variants if v.alpha == "premium_wheel_swo")


def test_the_volatility_premium_recipe_is_the_base_of_the_calibration_it_reads_the_metric_the_gate_tests():
    (idea,) = [i for i in ideas.load_ideas() if i.id == "volatility_risk_premium"]
    assert "ratio of implied to realized volatility" in idea.summary and "calibration of the premium threshold" in idea.summary
    assert {"quant.options", "quant.stats"} <= set(idea.needs) and "option_chain" in idea.data
    gate = gate_mod.load_default()
    assert gate.metric == "iv_rv_ratio" and "cash_secured_put" in gate.structures      # the first phase of the wheel is one of the structures that the backtest tries


# ---- a desk cannot send them while the switch is closed ------------------------------------------------------------------------

def test_while_the_switch_is_closed_the_desk_cannot_send_the_wheel_or_the_straddle(swo):
    assert not switch_state(swo.desk_id, "SPY", db=swo.db).is_open
    for legs in (WHEEL_PUT, PROTECTED_STRADDLE):
        out = send(swo, *legs)
        assert refused(out), out
        assert "SPY" in out.why and "closed" in out.why


def test_the_switch_opens_for_the_calibrated_underlying_only_and_closes_when_a_recalibration_fails(swo):
    found = premium.calibrate(swo.desk_id, "SPY", as_of=AS_OF, source=lambda n, a: rich_history(), db=swo.db, gate=GATE)
    assert found.status == "calibrated"
    opened = send(swo, *WHEEL_PUT, stop=Decimal("14.00"))                              # a naked put sold needs its stop above the entry (SPEC-026)
    assert (opened.status, opened.code) == ("analysis_only", "level_1")                # past the premium switch: the level of the desk is what holds it now
    straddle = send(swo, *PROTECTED_STRADDLE)
    assert (straddle.status, straddle.code) == ("refused", "stop_shape")               # past the premium switch too: 1.1.0 refuses the shape of a straddle by its stop guard, not by the switch
    assert refused(send(swo, (QQQ_PUT, "sell")))                                         # another underlying has no calibration
    (again,) = premium.recalibrate(swo.desk_id, as_of=AS_OF, source=lambda n, a: fair_history(), db=swo.db, gate=GATE)
    assert again.retired_id == found.threshold_id
    for legs in (WHEEL_PUT, PROTECTED_STRADDLE):
        assert refused(send(swo, *legs))


def test_a_calibration_that_does_not_pass_leaves_the_recipe_refused(swo):
    assert premium.calibrate(swo.desk_id, "SPY", as_of=AS_OF, source=lambda n, a: fair_history(), db=swo.db, gate=GATE).status == "not_passed"
    assert premium.calibrate(swo.desk_id, "SPY", as_of=AS_OF, source=lambda n, a: None, db=swo.db, gate=GATE).status == "data_missing"
    assert refused(send(swo, *WHEEL_PUT))
