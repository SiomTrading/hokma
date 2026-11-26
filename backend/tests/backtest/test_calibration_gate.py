"""The gate of the calibration of the premium selling threshold (SPEC-020, section 4.9): its defaults, what tightening and loosening mean."""
import dataclasses
from pathlib import Path

import pytest

from hokma.backtest.calibration import gate as gate_mod
from hokma.backtest.calibration.gate import GateError, GateLoosened, PremiumGate
from hokma.desks.swo.premium_switch import SELLING_STRUCTURES

ROOT = Path(__file__).resolve().parents[3]
YAML = ROOT / "kit" / "template" / "service" / "calibration" / "premium-gate.yaml"


def base():
    return gate_mod.load_default()


def test_the_yaml_holds_the_defaults_of_the_spec():
    gate = base()
    assert (gate.min_oos_trades, gate.min_dsr, gate.max_pbo, gate.min_wf_efficiency, gate.folds) == (30, 0.95, 0.50, 0.50, 5)
    assert gate.dte == (21, 60) and gate.max_drawdown_pct is None and gate.metric == "iv_rv_ratio"
    assert dict(gate.exits) == {"profit_take_pct": 50.0, "stop_loss_multiple": 2.0, "close_at_dte": 14}
    assert gate_mod.gate_path() == YAML and PremiumGate() == gate


def test_every_structure_of_the_default_gate_sells_premium_and_is_one_of_the_desk():
    assert set(base().structures) <= set(SELLING_STRUCTURES)
    assert "covered_call" in SELLING_STRUCTURES and "covered_call" not in gate_mod.SUPPORTED_STRUCTURES      # it needs shares that a chain history lacks


@pytest.mark.parametrize("change", [
    {"metric": "close"}, {"structures": ()}, {"structures": ("long_call",)}, {"structures": ("covered_call",)}, {"structures": ("short_put", "short_put")},
    {"dte": (60, 21)}, {"dte": (21,)}, {"dte": (0, 60)}, {"min_oos_trades": 0}, {"min_oos_trades": True}, {"min_dsr": 1.5}, {"max_pbo": -0.1},
    {"min_wf_efficiency": float("nan")}, {"folds": 1}, {"folds": 2.5}, {"max_drawdown_pct": 0}, {"max_drawdown_pct": 101}, {"margin_use_pct": 0},
    {"fee_table": "no_such_table"}, {"fee_table": "../../etc/passwd"}, {"exits": {"profit_take_pct": 50}}, {"exits": {"profit_take_pct": 50, "stop_loss_multiple": 2, "close_at_dte": 21}},
    {"exits": {"profit_take_pct": 0, "stop_loss_multiple": 2, "close_at_dte": 14}}, {"legs": {"short_otm_pct": 5}}, {"min_history_days": 10}])
def test_a_value_outside_its_domain_is_refused_with_a_stable_code(change):
    with pytest.raises(GateError) as caught:
        dataclasses.replace(base(), **change)
    assert caught.value.code == "bad_gate"


def test_an_unknown_field_of_the_file_is_refused():
    with pytest.raises(GateError, match="unknown field 'open_switch'"):
        gate_mod.from_dict({**base().to_dict(), "open_switch": True})
    with pytest.raises(GateError):
        gate_mod.from_dict(["not", "a", "mapping"])


def test_a_file_that_cannot_be_read_is_refused(tmp_path):
    bad = tmp_path / "gate.yaml"
    bad.write_text("metric: [unclosed", encoding="utf-8")
    with pytest.raises(GateError):
        gate_mod.load_default(bad)
    with pytest.raises(GateError):
        gate_mod.load_default(tmp_path / "missing.yaml")


def test_the_gate_round_trips_through_its_plain_form():
    gate = base()
    assert gate_mod.from_dict(gate.to_dict()) == gate


def test_tightening_every_field_is_allowed_and_loosening_names_the_field():
    gate = base()
    tight = dataclasses.replace(gate, min_oos_trades=60, min_dsr=0.99, max_pbo=0.3, min_wf_efficiency=0.7, folds=6, min_history_days=700, structures=("cash_secured_put",),
                                dte=(25, 50), max_drawdown_pct=8.0)
    assert gate_mod.loosened_fields(tight, gate) == [] and gate_mod.require_not_looser(tight, gate) is tight
    loose = {"min_oos_trades": 10, "min_dsr": 0.8, "max_pbo": 0.9, "min_wf_efficiency": 0.1, "folds": 3, "min_history_days": 100,
             "structures": ("cash_secured_put", "credit_put_spread", "short_strangle"), "dte": (15, 60), "fee_table": "zero"}
    for field, value in loose.items():
        looser = dataclasses.replace(gate, **{field: value})
        assert gate_mod.loosened_fields(looser, gate) == [field], field
        with pytest.raises(GateLoosened, match=field) as caught:
            gate_mod.require_not_looser(looser, gate)
        assert caught.value.code == "gate_loosened"
    assert set(loose) <= set(gate_mod.LOOSENING_RULES)


def test_a_wider_window_of_days_to_expiry_on_the_high_side_is_looser_too():
    gate = base()
    assert gate_mod.loosened_fields(dataclasses.replace(gate, dte=(21, 90)), gate) == ["dte"]


def test_tightened_refuses_a_change_that_loosens():
    gate = base()
    assert gate_mod.tightened(gate, min_dsr=0.97).min_dsr == 0.97
    with pytest.raises(GateLoosened):
        gate_mod.tightened(gate, min_dsr=0.5)


def test_a_drawdown_limit_of_the_gate_is_never_a_loosening_because_the_lower_of_it_and_the_desk_rules():
    gate = base()
    assert gate_mod.loosened_fields(dataclasses.replace(gate, max_drawdown_pct=99.0), gate) == []      # calibrate() takes the lower of this and the desk limit
