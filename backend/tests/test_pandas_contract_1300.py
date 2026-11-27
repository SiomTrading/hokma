"""1.3.0 (task 3433), C: the format of a strategy in pandas. PARAMS, the columns of session and of the table of signals, the stop as an absolute price in the engine and the adapter
that replays the signals bar by bar in the backtest."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import numpy as np
import pytest
from backtest.runner_support import NQ, source_of, spec
from backtest.support import requires_engine
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.backtest import runner
from hokma.backtest.fills import Decision
from hokma.backtest.spec import SpecError
from hokma.backtest.ticks import Bar
from hokma.factory.pandas_strategy import adapter, contract, sessions
from hokma.factory.pandas_strategy.contract import ContractError, Problem

GOOD = {"fast": {"default": 9, "min": 3, "max": 30, "step": 1, "type": "int"}, "mult": {"default": 2.0, "min": 0.5, "max": 4.0, "step": 0.25, "type": "float"}}


# ----------------------------------------------------------------------------------------------------------------------------- PARAMS
def test_params_become_the_schema_the_space_and_the_corners():
    defs = contract.parse_params(GOOD)
    assert [d.name for d in defs] == ["fast", "mult"]
    assert contract.schema_of(defs).defaults() == {"fast": 9, "mult": 2.0}
    space = contract.space_of(defs)
    assert space.names == ("fast", "mult") and space.axes[0].values[0] == 3 and space.axes[0].values[-1] == 30 and space.axes[1].values == tuple(0.5 + 0.25 * i for i in range(15))
    assert contract.space_of(defs, ("mult",)).names == ("mult",)
    assert contract.corner(defs, "min") == {"fast": 3, "mult": 0.5} and contract.corner(defs, "max") == {"fast": 30, "mult": 4.0}


@pytest.mark.parametrize("raw, rule, field", [
    ({}, "params_shape", "PARAMS"),
    ({"Fast": GOOD["fast"]}, "params_name", "PARAMS.Fast"),
    ({"fast": {"default": 9}}, "params_keys", "PARAMS.fast"),
    ({"fast": {**GOOD["fast"], "type": "double"}}, "params_type", "PARAMS.fast.type"),
    ({"fast": {**GOOD["fast"], "step": 0}}, "params_step", "PARAMS.fast.step"),
    ({"fast": {**GOOD["fast"], "min": 40}}, "params_range", "PARAMS.fast"),
    ({"fast": {**GOOD["fast"], "default": 99}}, "params_default", "PARAMS.fast.default"),
    ({"fast": {**GOOD["fast"], "step": 0.5}}, "params_int", "PARAMS.fast"),
    ({"fast": {**GOOD["fast"], "max": float("nan")}}, "params_number", "PARAMS.fast.max"),
])
def test_a_bad_params_names_the_rule_the_field_and_the_change(raw, rule, field):
    with pytest.raises(ContractError) as err:
        contract.parse_params(raw)
    (problem,) = err.value.problems
    assert (problem.rule, problem.field) == (rule, field) and problem.message and problem.fix


def test_the_values_of_a_run_are_checked_against_params():
    defs = contract.parse_params(GOOD)
    assert contract.check_params(defs, {"fast": 12}) == {"fast": 12, "mult": 2.0}
    with pytest.raises(ContractError) as err:
        contract.check_params(defs, {"fast": 99})
    assert err.value.problems[0].rule == "params_range" and err.value.problems[0].field == "fast"
    with pytest.raises(ContractError) as err:
        contract.check_params(defs, {"slow": 4})
    assert err.value.problems[0].rule == "params_unknown"


# ----------------------------------------------------------------------------------------------------------------------------- session
def ns(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()) * 10**9


def test_session_id_starts_at_18_new_york_and_is_rth_is_930_to_1600():
    # winter (EST, UTC-5): 2025-11-09 is a Sunday; 18:00 New York = 23:00 UTC starts the session of Monday 10
    times = np.array([ns("2025-11-09T22:55:00"), ns("2025-11-09T23:00:00"), ns("2025-11-10T14:29:00"), ns("2025-11-10T14:30:00"), ns("2025-11-10T20:55:00"), ns("2025-11-10T21:00:00"),
                      ns("2025-11-10T22:59:00"), ns("2025-11-10T23:00:00")], dtype="int64")
    session, rth = sessions.session_columns(times, 300)
    assert session.tolist() == [20251109, 20251110, 20251110, 20251110, 20251110, 20251110, 20251110, 20251111]
    assert rth.tolist() == [False, False, False, True, True, False, False, False]          # 9:30 is in, 15:55 is in, 16:00 is out


def test_the_summer_clock_of_new_york_moves_the_session_and_the_regular_hours():
    # summer (EDT, UTC-4): 18:00 New York = 22:00 UTC; 9:30 = 13:30 UTC
    times = np.array([ns("2025-07-14T21:55:00"), ns("2025-07-14T22:00:00"), ns("2025-07-15T13:29:00"), ns("2025-07-15T13:30:00")], dtype="int64")
    session, rth = sessions.session_columns(times, 300)
    assert session.tolist() == [20250714, 20250715, 20250715, 20250715] and rth.tolist() == [False, False, False, True]


def test_a_weekend_day_is_not_regular_hours_and_a_daily_bar_always_is():
    saturday = np.array([ns("2025-11-08T15:00:00")], dtype="int64")
    assert sessions.session_columns(saturday, 300)[1].tolist() == [False]
    assert sessions.session_columns(saturday, 86_400)[1].tolist() == [True]


def test_the_frame_has_the_columns_and_the_utc_index_a_strategy_expects():
    times = np.array([ns("2025-11-10T15:00:00"), ns("2025-11-10T15:05:00")], dtype="int64")
    cols = {k: np.array([1.0, 2.0]) for k in ("open", "high", "low", "close", "volume")}
    frame = sessions.frame_of(times, cols, 300)
    assert list(frame.columns) == list(contract.DATA_COLUMNS) and str(frame.index.tz) == "UTC" and frame["session_id"].dtype == "int64" and frame["is_rth"].dtype == bool


# ----------------------------------------------------------------------------------------------------------------------------- the table of signals
def columns(n=6, **over):
    base = {c: np.zeros(n, dtype=bool) for c in contract.FLAGS}
    base["stop"] = np.full(n, np.nan)
    base.update(over)
    return base


CLOSE = np.full(6, 100.0)


def test_a_clean_answer_has_no_problem_and_a_nan_flag_is_no_signal():
    entry = np.array([np.nan, 1.0, 0, 0, 0, 0])
    stop = np.array([np.nan, 99.0, np.nan, np.nan, np.nan, np.nan])
    signals, problems = contract.build_signals(columns(entry_long=entry, stop=stop), CLOSE)
    assert problems == [] and signals.entry_long.tolist() == [False, True, False, False, False, False]


def rules(cols, close=CLOSE):
    return [p.rule for p in contract.build_signals(cols, close)[1]]


def test_each_break_of_the_format_has_its_own_rule_with_a_fix():
    flag = np.array([0, 1, 0, 0, 0, 0], dtype=bool)
    assert rules({k: v for k, v in columns().items() if k != "stop"}) == ["signals_columns"]
    assert rules({**columns(), "extra": flag}) == ["signals_columns"]
    assert rules(columns(entry_long=flag)) == ["signals_stop_missing"]                                                  # an entry with no stop
    assert rules(columns(entry_long=flag, stop=np.array([np.nan, 101.0, 0, 0, 0, 0]))) == ["signals_stop_side"]          # long stop above the close
    assert rules(columns(entry_short=flag, stop=np.array([np.nan, 99.0, 0, 0, 0, 0]))) == ["signals_stop_side"]          # short stop below the close
    assert rules(columns(entry_long=flag, entry_short=flag, stop=np.array([np.nan, 99.0, 0, 0, 0, 0]))) == ["signals_both_sides", "signals_stop_side"]
    assert rules(columns(entry_long=np.array([0, 2, 0, 0, 0, 0]), stop=np.array([np.nan, 99, 0, 0, 0, 0]))) == ["signals_flag"]
    assert rules(columns(entry_long=flag[:3])) == ["signals_length"]
    good = columns(entry_long=flag, stop=np.array([np.nan, 99.0, 0, 0, 0, 0]))
    assert rules({**good, "target": np.array([np.nan, 98.0, 0, 0, 0, 0])}) == ["signals_target_side"]
    assert rules({**good, "limit_price": np.array([np.nan, -1.0, 0, 0, 0, 0])}) == ["signals_limit"]
    for problem in contract.build_signals(columns(entry_long=flag), CLOSE)[1]:
        assert isinstance(problem, Problem) and problem.field and problem.fix


def test_the_same_signals_compare_equal_with_nan_and_a_missing_level_counts_as_nan():
    a, _ = contract.build_signals(columns(), CLOSE)
    b, _ = contract.build_signals({**columns(), "target": np.full(6, np.nan)}, CLOSE)
    assert a.same_as(b) and b.same_as(a)
    c, _ = contract.build_signals(columns(entry_long=np.array([0, 1, 0, 0, 0, 0], dtype=bool), stop=np.array([np.nan, 99, 0, 0, 0, 0])), CLOSE)
    assert not a.same_as(c) and a.same_as(c, upto=1)


# ----------------------------------------------------------------------------------------------------------------------------- the engine: the stop is an absolute price
def test_a_decision_with_an_absolute_stop_needs_its_distance_and_an_entry():
    Decision("enter", "long", D(1), D(10), None, stop_at=D(19990))
    with pytest.raises(SpecError):
        Decision("enter", "long", D(1), None, None, stop_at=D(19990))
    with pytest.raises(SpecError):
        Decision("exit", stop_at=D(19990))
    with pytest.raises(SpecError):
        Decision("enter", "long", D(1), D(10), None, stop_at=D(0))


START = datetime(2025, 11, 10, 15, 0, tzinfo=timezone.utc)


def bars_of(rows):
    return [Bar(START + timedelta(minutes=i), D(str(o)), D(str(h)), D(str(l)), D(str(c)), D(100)) for i, (o, h, l, c) in enumerate(rows)]


def flat(n, price=20000):
    return [(price, price + 1, price - 1, price)] * n


def provider_of(entry_long=(), entry_short=(), exit_long=(), exit_short=(), stop=None, target=None, limit=None):
    """A provider of signals that does not look at the data: the bars where each thing happens."""
    def provider(source, params, open_ns, cols, bar_seconds):
        n = len(open_ns)
        out = {name: np.isin(np.arange(n), list(bars)) for name, bars in (("entry_long", entry_long), ("entry_short", entry_short), ("exit_long", exit_long), ("exit_short", exit_short))}
        out["stop"] = np.array([(stop or {}).get(i, np.nan) for i in range(n)], dtype=float)
        if target:
            out["target"] = np.array([target.get(i, np.nan) for i in range(n)], dtype=float)
        if limit:
            out["limit_price"] = np.array([limit.get(i, np.nan) for i in range(n)], dtype=float)
        return out
    return provider


def go(rows, provider, db, tmp_path, **params):
    bars = bars_of(rows)
    cls = adapter.build_class("u_str001_v1", contract.parse_params(GOOD), "source", provider)
    strategy = cls(**{**cls.SCHEMA.defaults(), **params})
    return runner.run(spec(bars, params={**cls.SCHEMA.defaults(), **params}), strategy=strategy, db=db, curve_dir=tmp_path, source=source_of({NQ: bars}))


@requires_engine
def test_the_stop_stays_where_the_strategy_fixed_it_on_the_signal_bar_even_if_the_next_bar_gaps(db, tmp_path):
    # bar 3 closes at 20000 and decides: long, stop 19990. Bar 4 opens with a gap at 20010 and trades down to 19995.
    # Measured again from the fill (20010) the stop would be 20000 and it would be hit; fixed at 19990 it is not.
    rows = flat(3) + [(20000, 20001, 19999, 20000), (20010, 20012, 19995, 20000)] + flat(3, 20000)
    result = go(rows, provider_of(entry_long=[3], exit_long=[6], stop={3: 19990.0}), db, tmp_path)
    assert result.status == "done", result.warnings
    (trade,) = result.trades
    assert trade.exit_tag != "stop" and trade.entry_price >= D("20010") and trade.side == "long"


@requires_engine
def test_a_stop_that_the_price_crosses_closes_the_trade_at_the_absolute_price(db, tmp_path):
    rows = flat(3) + [(20000, 20001, 19999, 20000), (20000, 20001, 19999, 20000), (20000, 20000, 19980, 19985)] + flat(2, 19985)
    result = go(rows, provider_of(entry_long=[3], stop={3: 19990.0}), db, tmp_path)
    (trade,) = result.trades
    assert trade.exit_tag == "stop" and D("19989.5") <= trade.exit_price <= D("19990")


@requires_engine
def test_the_signal_is_made_at_the_close_and_the_order_goes_at_the_next_open_and_an_exit_signal_closes_it(db, tmp_path):
    rows = flat(3) + [(20000, 20001, 19999, 20000), (20004, 20005, 20003, 20004), (20004, 20005, 20003, 20004), (20006, 20007, 20005, 20006), (20008, 20009, 20007, 20008)]
    result = go(rows, provider_of(entry_long=[3], exit_long=[5], stop={3: 19990.0}), db, tmp_path)
    (trade,) = result.trades
    assert trade.entry_at == START + timedelta(minutes=4) and trade.entry_price == D("20004.25")          # bar 4 opens at 20004: one tick against
    assert trade.exit_at == START + timedelta(minutes=6) and trade.exit_tag != "stop"                       # the exit signal of bar 5 goes at the open of bar 6


@requires_engine
def test_a_short_the_target_and_the_flip_work_and_a_bad_answer_stops_the_run_with_the_rule(db, tmp_path):
    rows = flat(3) + [(20000, 20001, 19999, 20000), (20000, 20001, 19999, 20000), (20000, 20000, 19975, 19980)] + flat(3, 19980)
    result = go(rows, provider_of(entry_short=[3], stop={3: 20010.0}, target={3: 19990.0}), db, tmp_path)
    (trade,) = result.trades
    assert trade.side == "short" and trade.exit_tag == "target"
    bad = go(flat(8), provider_of(entry_long=[3]), db, tmp_path)                                    # an entry with no stop
    assert bad.status == "failed" and "failed_stage:signal" in bad.warnings          # the run stops in the stage of the signal; the dry run is what says the rule
