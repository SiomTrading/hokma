"""The versioned table of limits (SPEC-032, OPT-05): the 13 measures, the seed of DT, SWC and SWO, saving, restoring and copying a line, and the
AuditConfig that each set produces."""
import inspect

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.factory import audit as au
from hokma.factory import thresholds as th
from hokma.factory.thresholds import ThresholdError

# Section 6 of the spec: the start values shared by the three lines, and the two that change by line (M11 years, M12 trades).
SHARED = {"pbo_warn": 0.20, "pbo_reject": 0.50, "min_dsr": 0.95, "min_oos_to_is": 0.50, "max_cost_share": 0.33, "edge_warns": True,
          "neighbor_spread": 0.10, "max_neighbor_drop": 0.30, "red_flag_sharpe": 3.0, "max_top_trade_share": 0.25, "min_regime_share": 0.50}
BY_LINE = {"DT": (1.0, 200), "SWC": (3.0, 100), "SWO": (3.0, 50)}
ORDER = ["pbo_warn", "pbo_reject", "min_dsr", "min_oos_to_is", "max_cost_share", "edge_warns", "neighbor_spread", "max_neighbor_drop",
         "red_flag_sharpe", "max_top_trade_share", "min_years", "min_trades", "min_regime_share"]


def rows(db, line=None):
    with db.read() as u:
        return u.session.query("SELECT * FROM audit_threshold_sets" + (" WHERE line = :l" if line else "") + " ORDER BY id", {"l": line} if line else {})


def seed_values(line="SWC"):
    return {**SHARED, "min_years": BY_LINE[line][0], "min_trades": BY_LINE[line][1]}


def bad(values, **kw):
    with pytest.raises(ThresholdError) as caught:
        th.save("SWC", values, reason=None, actor="owner", **kw)
    assert caught.value.code == "bad_input" and str(caught.value).startswith("bad_input: ")
    return str(caught.value)


# ---- the table ---------------------------------------------------------------------------------------------------------------------------

def test_the_table_has_the_13_measures_of_section_6_in_order_and_each_one_decides_its_checks():
    assert [m.key for m in th.MEASURES] == ORDER and th.LINES == ("DT", "SWC", "SWO", "LP", "RF")
    decided = {m.key: m.checks for m in th.MEASURES}
    assert decided == {"pbo_warn": (19,), "pbo_reject": (19,), "min_dsr": (18,), "min_oos_to_is": (22,), "max_cost_share": (10,), "edge_warns": (20,),
                       "neighbor_spread": (20,), "max_neighbor_drop": (20,), "red_flag_sharpe": (22,), "max_top_trade_share": (22,), "min_years": (9,),
                       "min_trades": (9,), "min_regime_share": (21,)}
    numbers = {c.number for c in au.CHECKLIST}
    assert all(set(m.checks) <= numbers for m in th.MEASURES)
    kinds = {m.key: (m.kind, m.low, m.high, m.step, m.effect) for m in th.MEASURES}
    assert kinds["pbo_warn"] == ("share", 0.05, 0.95, 0.05, "yellow") and kinds["pbo_reject"] == ("share", 0.05, 0.95, 0.05, "red")
    assert kinds["min_dsr"] == ("number", 0.50, 0.99, 0.01, "red") and kinds["edge_warns"] == ("toggle", None, None, None, "yellow")
    assert kinds["neighbor_spread"][4] == "setting" and kinds["min_years"] == ("years", 0.5, 30.0, 0.5, "red")
    assert kinds["min_trades"] == ("count", 10, 10_000, 10, "red") and kinds["red_flag_sharpe"] == ("number", 1.0, 10.0, 0.1, "red")


def test_the_seed_of_each_line_holds_the_values_of_section_6(db):
    for line, (years, trades) in BY_LINE.items():
        found = th.current(line, db=db)
        assert (found.line, found.version, found.changed_by, found.reason, found.restored_from, found.unchanged) == (line, 1, "system:seed", None, None, False)
        assert dict(found.values) == {**SHARED, "min_years": years, "min_trades": trades} and list(found.values) == sorted(ORDER)
        assert found.created_at.tzinfo is not None
        assert [h.id for h in th.history(line, db=db)] == [found.id]
    assert th.current("DT", db=db).values["min_trades"] == 200 and th.current("SWO", db=db).values["min_trades"] == 50


def test_lines_without_a_set_say_no_thresholds_and_a_wrong_line_is_bad_input(db):
    for line in ("LP", "RF"):
        with pytest.raises(ThresholdError) as caught:
            th.current(line, db=db)
        assert caught.value.code == "no_thresholds" and str(caught.value).startswith("no_thresholds: ")
        assert th.history(line, db=db) == []
    for wrong in ("dt", "XX", "", None, 3):
        with pytest.raises(ThresholdError) as caught:
            th.current(wrong, db=db)
        assert caught.value.code == "bad_input"


# ---- to_config ---------------------------------------------------------------------------------------------------------------------------

def test_the_seed_of_each_line_builds_a_valid_audit_config(db):
    for line, (years, trades) in BY_LINE.items():
        config = th.to_config(th.current(line, db=db).values)
        assert isinstance(config, au.AuditConfig)
        assert config.pbo == au.PboThresholds(0.20, 0.50) and config.min_years == years and config.min_trades == trades and isinstance(config.min_trades, int)
        assert (config.min_dsr, config.max_neighbor_drop, config.neighbor_spread, config.min_regime_share) == (0.95, 0.30, 0.10, 0.50)
        assert (config.red_flag_sharpe, config.max_top_trade_share, config.min_oos_to_is, config.max_cost_share) == (3.0, 0.25, 0.50, 0.33)


def test_to_config_refuses_what_audit_config_refuses_and_what_the_table_refuses():
    for broken in ({**seed_values(), "pbo_warn": 0.6}, {**seed_values(), "pbo_reject": 0.1}, {**seed_values(), "min_dsr": 1.5},
                   {k: v for k, v in seed_values().items() if k != "min_dsr"}, {**seed_values(), "extra": 1}):
        with pytest.raises(ThresholdError) as caught:
            th.to_config(broken)
        assert caught.value.code == "bad_input"
    assert th.to_config({**seed_values(), "edge_warns": False}).max_cost_share == 0.33          # the toggle is for the bridge, not for the config


# ---- save --------------------------------------------------------------------------------------------------------------------------------

def test_saving_a_changed_value_writes_the_next_version_with_the_actor_and_the_reason(db):
    before = rows(db, "SWC")
    saved = th.save("SWC", {**seed_values(), "min_dsr": 0.9, "edge_warns": False, "min_trades": 120}, reason="  looser on purpose ", actor="owner", db=db)
    assert (saved.line, saved.version, saved.changed_by, saved.reason, saved.restored_from, saved.unchanged) == ("SWC", 2, "owner", "looser on purpose", None, False)
    assert saved.values["min_dsr"] == 0.9 and saved.values["edge_warns"] is False and saved.values["min_trades"] == 120
    assert th.current("SWC", db=db) == saved and [h.version for h in th.history("SWC", db=db)] == [2, 1]
    assert rows(db, "SWC")[:1] == before and len(rows(db, "SWC")) == 2                       # the seed row is exactly as it was
    assert th.current("DT", db=db).version == 1                                                 # another line is not touched
    again = th.save("SWC", {**seed_values(), "min_dsr": 0.9, "edge_warns": False, "min_trades": 120}, reason=None, actor="owner", db=db)
    assert again.unchanged and again.version == 2 and again.id == saved.id and len(rows(db, "SWC")) == 2


def test_the_first_save_of_a_line_without_a_set_is_version_1(db):
    saved = th.save("LP", seed_values("DT"), reason=None, actor="owner", db=db)
    assert (saved.version, saved.reason, saved.changed_by) == (1, None, "owner") and th.current("LP", db=db).id == saved.id


def test_an_identical_version_is_not_written_even_with_other_number_types(db):
    count = len(rows(db))
    same = th.save("SWC", {**seed_values(), "min_trades": 100.0, "min_years": 3, "pbo_warn": 0.2}, reason="no change", actor="owner", db=db)
    assert same.unchanged and same.version == 1 and same.changed_by == "system:seed" and len(rows(db)) == count


def test_every_wrong_set_is_bad_input_and_nothing_is_written(db):
    count = len(rows(db))
    base = seed_values()
    wrong = [
        {**base, "pbo_warn": 0.99}, {**base, "pbo_warn": 0.0}, {**base, "min_dsr": 0.49}, {**base, "min_dsr": 1.0}, {**base, "min_oos_to_is": 0.05},
        {**base, "max_cost_share": 1.01}, {**base, "neighbor_spread": 0.55}, {**base, "red_flag_sharpe": 10.1}, {**base, "min_years": 0.0},
        {**base, "min_years": 31}, {**base, "min_trades": 5}, {**base, "min_trades": 10_010}, {**base, "min_regime_share": 0.05},     # out of range
        {**base, "pbo_warn": 0.33}, {**base, "min_trades": 105}, {**base, "min_dsr": 0.955}, {**base, "min_years": 1.2},               # off the step
        {**base, "min_trades": 100.5}, {**base, "pbo_warn": True}, {**base, "pbo_warn": "0.2"}, {**base, "pbo_warn": None},
        {**base, "pbo_warn": float("nan")}, {**base, "pbo_warn": float("inf")}, {**base, "edge_warns": 1}, {**base, "edge_warns": "yes"},   # wrong type
        {**base, "unknown_measure": 1}, {k: v for k, v in base.items() if k != "min_dsr"}, {}, None, [("pbo_warn", 0.2)],                  # keys
        {**base, "pbo_warn": 0.60},                                                                            # warn above reject: AuditConfig refuses
    ]
    for values in wrong:
        with pytest.raises(ThresholdError) as caught:
            th.save("SWC", values, reason=None, actor="owner", db=db)
        assert caught.value.code == "bad_input", values
    assert len(rows(db)) == count
    for kwargs in ({"line": "XX"}, {"actor": ""}, {"actor": "Owner"}, {"actor": None}, {"reason": "x" * 501}, {"reason": 5}):
        call = {"line": "SWC", "reason": None, "actor": "owner", **kwargs}
        with pytest.raises(ThresholdError):
            th.save(call["line"], {**base, "min_dsr": 0.9}, reason=call["reason"], actor=call["actor"], db=db)
    assert len(rows(db)) == count


def test_every_measure_accepts_its_two_ends_and_refuses_one_step_beyond(db):
    base = seed_values()
    for measure in th.MEASURES:
        if measure.kind == "toggle":
            continue
        for end, beyond in ((measure.low, measure.low - measure.step), (measure.high, measure.high + measure.step)):
            values = {**base, measure.key: end}
            if measure.key == "pbo_warn":
                values["pbo_reject"] = 0.95 if end > 0.5 else 0.50
            if measure.key == "pbo_reject" and end < 0.2:
                values["pbo_warn"] = 0.05
            th.to_config(values)                                                           # the end is a valid set
            outside = {**base, measure.key: beyond}
            with pytest.raises(ThresholdError):
                th.to_config(outside)
    assert th.to_config({**base, "min_trades": 10_000, "min_years": 30.0}).min_trades == 10_000


def test_the_actor_is_a_parameter_of_the_code_and_never_a_value_of_the_set(db):
    assert inspect.signature(th.save).parameters["actor"].kind is inspect.Parameter.KEYWORD_ONLY
    with pytest.raises(ThresholdError):
        th.save("SWC", {**seed_values(), "actor": "owner", "min_dsr": 0.9}, reason=None, actor="owner", db=db)
    assert th.save("SWC", {**seed_values(), "min_dsr": 0.9}, reason=None, actor="agent_ian", db=db).changed_by == "agent_ian"


# ---- restore -----------------------------------------------------------------------------------------------------------------------------

def test_restoring_creates_a_new_version_that_points_at_the_old_one(db):
    first = th.current("SWC", db=db)
    th.save("SWC", {**seed_values(), "min_dsr": 0.9}, reason="v2", actor="owner", db=db)
    third = th.save("SWC", {**seed_values(), "min_dsr": 0.8, "max_cost_share": 0.5}, reason="v3", actor="owner", db=db)
    before = rows(db, "SWC")
    restored = th.restore("SWC", first.id, actor="owner", db=db)
    assert (restored.version, restored.restored_from, restored.changed_by, restored.unchanged) == (4, first.id, "owner", False)
    assert dict(restored.values) == dict(first.values) and restored.reason == "restored from version 1"
    assert rows(db, "SWC")[:3] == before and [h.version for h in th.history("SWC", db=db)] == [4, 3, 2, 1]
    assert th.current("SWC", db=db).id == restored.id and third.version == 3
    now = th.restore("SWC", first.id, actor="owner", db=db)                                  # the current set already has these values
    assert now.unchanged and now.id == restored.id and len(rows(db, "SWC")) == 4


def test_restoring_something_that_is_not_a_version_of_the_line_is_bad_input(db):
    other = th.current("DT", db=db)
    for line, set_id in (("SWC", other.id), ("SWC", 9999), ("SWC", True), ("SWC", "1"), ("XX", other.id)):
        with pytest.raises(ThresholdError) as caught:
            th.restore(line, set_id, actor="owner", db=db)
        assert caught.value.code == "bad_input"
    assert len(rows(db, "SWC")) == 1


# ---- copy_line ---------------------------------------------------------------------------------------------------------------------------

def test_copy_line_saves_the_current_set_of_the_source_as_the_next_version_of_the_target(db):
    th.save("SWO", {**seed_values("SWO"), "red_flag_sharpe": 4.0}, reason=None, actor="owner", db=db)
    copied = th.copy_line("LP", "SWO", actor="owner", db=db)
    assert (copied.line, copied.version, copied.changed_by, copied.reason) == ("LP", 1, "owner", "copied from SWO version 2")
    assert dict(copied.values) == dict(th.current("SWO", db=db).values) and copied.values["red_flag_sharpe"] == 4.0
    second = th.copy_line("DT", "SWO", actor="owner", db=db)
    assert second.version == 2 and second.values["min_trades"] == 50 and th.current("DT", db=db).id == second.id
    assert th.copy_line("DT", "SWO", actor="owner", db=db).unchanged
    assert len(rows(db, "SWO")) == 2                                                          # the source is only read


def test_copy_line_from_a_line_without_a_set_or_onto_itself_is_refused(db):
    with pytest.raises(ThresholdError) as caught:
        th.copy_line("DT", "RF", actor="owner", db=db)
    assert caught.value.code == "no_thresholds"
    for target, source in (("DT", "DT"), ("XX", "DT"), ("DT", "XX")):
        with pytest.raises(ThresholdError) as caught:
            th.copy_line(target, source, actor="owner", db=db)
        assert caught.value.code == "bad_input"
    assert len(rows(db, "DT")) == 1


# ---- the guards of the project ----------------------------------------------------------------------------------------------------------

def test_the_module_has_no_broker_path_no_own_statistic_and_no_long_dash():
    source = inspect.getsource(th)
    for banned in ("hokma.exec", "hokma.broker", "hokma.backtest", "hokma.quant", "numpy", "scipy", "pandas", "subprocess", "\u2014"):
        assert banned not in source.replace("from hokma.quant.errors import BadInput", ""), banned
