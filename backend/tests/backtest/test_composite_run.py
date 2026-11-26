"""An alpha and a saved draft run through a composite with the parts of the desks (SPEC-020: L-51): no `not_a_strategy`, same bars same result."""
import pytest
from backtest import api_support as api
from backtest.runner_support import NQ, source_of
from backtest.support import requires_engine
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma import backtest
from hokma.backtest.runner import Services
from hokma.factory import spec as saved

pytestmark = requires_engine

DRAFT = """
strategy_id: my_cross
line: DT
alpha: ema_cross
params: {{fast: 5, slow: 20}}
hypothesis: a fast average above a slow one is a long view
parts: {parts}
tests:
  signal:
    - {{name: up, facts: {{ema_fast: [101.0], ema_slow: [100.0]}}, expect: long}}
  position_size:
    - {{name: one, contract_id: "FUT:NQ:2025-03", asset_type: future, multiplier: 20, kind: contracts, value: 1, price: 100, equity: 100000, expect_qty: 1}}
"""


def services(bars, db, tmp_path):
    return Services(db=db, source=source_of({NQ: bars}), curve_dir=tmp_path)


def test_an_alpha_of_the_registry_runs_through_a_composite_with_one_contract_and_its_own_tag(db, tmp_path):
    bars = api.series(600)
    spec = api.spec_of(bars, strategy_id="ema_cross", params={"fast": 5, "slow": 20})
    result = backtest.run(spec, services=services(bars, db, tmp_path))
    assert result.status == "done" and result.trades
    assert {t.qty for t in result.trades} == {1} and {t.entry_tag for t in result.trades} == {"ema_cross"}
    again = backtest.run(spec, services=services(bars, db, tmp_path / "again"))
    assert (again.realized_pnl_usd, again.mtm_pnl_usd, again.data_hash) == (result.realized_pnl_usd, result.mtm_pnl_usd, result.data_hash)


def test_a_saved_draft_runs_through_a_composite_with_the_parts_it_names(db, tmp_path):
    bars = api.series(600)
    saved.save_draft(DRAFT.format(parts="{sizer: {kind: fixed_contracts, n: 3}}"), db=db)
    spec = api.spec_of(bars, strategy_id="my_cross", params={})
    result = backtest.run(spec, services=services(bars, db, tmp_path))
    assert result.status == "done" and result.trades and {t.qty for t in result.trades} == {3}
    assert {t.entry_tag for t in result.trades} == {"ema_cross"}


def test_the_parameters_of_the_spec_move_the_parameters_of_the_alpha_of_the_draft(db, tmp_path):
    bars = api.series(600)
    saved.save_draft(DRAFT.format(parts="{}"), db=db)
    slow = backtest.run(api.spec_of(bars, strategy_id="my_cross", params={"fast": 5, "slow": 60}), services=services(bars, db, tmp_path))
    fast = backtest.run(api.spec_of(bars, strategy_id="my_cross", params={}), services=services(bars, db, tmp_path / "b"))
    assert slow.status == fast.status == "done" and len(slow.trades) != len(fast.trades)


def test_a_draft_with_a_risk_sizer_and_an_atr_exit_places_its_stop_and_sizes_by_risk(db, tmp_path):
    bars = api.series(600)
    parts = "{sizer: {kind: fixed_risk, risk_pct: 1.0}, exit: {kind: atr, length: 14, stop_atr: 3.0, r_multiple: 2.0}}"
    saved.save_draft(DRAFT.format(parts=parts), db=db)
    result = backtest.run(api.spec_of(bars, strategy_id="my_cross", params={}), services=services(bars, db, tmp_path))
    assert result.status == "done" and result.trades
    assert all(t.qty >= 1 for t in result.trades)
    assert any(t.exit_tag in ("stop", "target") for t in result.trades)          # the protection of the exit rule closed some of them


def test_an_unknown_id_and_a_draft_that_does_not_exist_are_the_same_failed_run(db, tmp_path):
    bars = api.series(100)
    gone = backtest.run(api.spec_of(bars, strategy_id="nothing", params={}), services=services(bars, db, tmp_path))
    assert gone.status == "failed" and "failed_code:unknown_strategy" in gone.warnings


def test_a_bad_part_in_a_draft_is_refused_when_it_is_saved(db):
    for parts in ("{sizer: {kind: kelly}}", "{sizer: {kind: fixed_risk}}", "{stops: {}}"):
        with pytest.raises(saved.BadSpec):
            saved.save_draft(DRAFT.format(parts=parts), db=db)


def test_a_draft_with_a_volatility_target_and_a_trailing_exit_runs_and_its_trailing_stop_closes_trades(db, tmp_path):
    bars = api.series(600)
    parts = "{sizer: {kind: vol_target, target_vol: 0.10, decay_pct: 94, periods_per_year: 252}, exit: {kind: atr_trailing, length: 14, atr_multiple: 3.0}}"
    saved.save_draft(DRAFT.format(parts=parts), db=db)
    result = backtest.run(api.spec_of(bars, strategy_id="my_cross", params={}), services=services(bars, db, tmp_path))
    assert result.status == "done" and result.trades, result.warnings
    assert all(t.qty >= 1 for t in result.trades)
    assert any(t.exit_tag == "trail" for t in result.trades) or any(t.exit_tag in ("stop", "exit") for t in result.trades)


def test_a_twap_draft_adds_slices_only_when_the_spec_accumulates(db, tmp_path):
    bars = api.series(600)
    saved.save_draft(DRAFT.format(parts="{sizer: {kind: fixed_contracts, n: 4}, execution: {kind: twap, n_slices: 2}}"), db=db)
    on = backtest.run(api.spec_of(bars, strategy_id="my_cross", params={}, accumulate=True), services=services(bars, db, tmp_path))
    off = backtest.run(api.spec_of(bars, strategy_id="my_cross", params={}), services=services(bars, db, tmp_path / "off"))
    assert on.status == off.status == "done" and on.trades and off.trades
    assert {t.qty for t in on.trades} <= {4} and {t.qty for t in off.trades} <= {2}              # both slices of 2 add up to 4 only when accumulating
    assert "entry_ignored_no_accumulation" in off.warnings and "entry_ignored_no_accumulation" not in on.warnings


def entry_times(db, run_id):
    from datetime import datetime, timezone

    from hokma.market import tz as marketzone

    with db.read() as u:
        rows = u.session.query("SELECT entry_at FROM backtest_trades WHERE run_id = :r ORDER BY id", {"r": run_id})
    out = []
    for row in rows:
        at = row["entry_at"] if isinstance(row["entry_at"], datetime) else datetime.fromisoformat(str(row["entry_at"]))
        out.append((at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(marketzone.NEW_YORK))
    return out


def test_a_draft_with_a_session_window_and_a_daily_maximum_never_enters_outside_the_window_or_past_the_maximum(db, tmp_path):
    """Spec 030, A1-3: the filters are part of the composite that the runner already executes. The bars start at 09:00 in New York and last 600
    minutes. The fill is on the open of the bar after the decision, so an entry decided on the last minute of the window is filled at 11:30."""
    bars = api.series(600)
    saved.save_draft(DRAFT.format(parts="{}"), db=db)
    free = backtest.run(api.spec_of(bars, strategy_id="my_cross", params={}), services=services(bars, db, tmp_path / "free"))
    assert free.status == "done" and len(free.trades) > 4
    outside = [t for t in entry_times(db, free.run_id) if not (t.hour, t.minute) >= (9, 45) or (t.hour, t.minute) > (11, 30)]
    assert outside                                                                   # the same strategy without filters does trade outside the window

    filters = "{filters: [{kind: session, start: '09:45', end: '11:30'}, {kind: max_trades_per_day, n: 2}]}"
    saved.save_draft(DRAFT.format(parts=filters), db=db)
    kept = backtest.run(api.spec_of(bars, strategy_id="my_cross", params={}, strategy_version=2), services=services(bars, db, tmp_path / "kept"))
    assert kept.status == "done" and kept.trades
    times = entry_times(db, kept.run_id)
    assert times and len(times) <= 2
    assert all((9, 45) <= (t.hour, t.minute) <= (11, 30) for t in times), times
