"""Result of a backtest run (SPEC-020, 5.4.4): B-07, B-14, B-18, B-20, B-25, V-15, V-16, V-19, V-21, V-33, V-34, L-33, L-40, N-21, N-26, P-11.

Every expected value is worked out by hand in the comments.
"""
import ast
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pandas as pd
import pytest
from dbsupport import env  # noqa: F401  (fixture)
from dbsupport import db  # noqa: F401  (fixture)
from pgsupport import pg_admin_url, pg_template, pg_url_migrated  # noqa: F401  (fixtures)

from hokma.backtest import results
from hokma.backtest.results import (BarRange, BacktestResult, EquityCurve, Fill, Holding, ResultError, Trade, ambiguous_bars, cash_mismatches,
                                    exposure, group_trades)
from hokma.quant import metrics, risk, validation
from hokma.quant.metrics import trades as trade_metrics

UTC = timezone.utc
T0 = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
NQ = "FUT:NQ:2026-03"
FIXTURE = Path(__file__).resolve().parents[1] / "quant" / "fixtures" / "returns_daily.csv"


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


# Four one minute bars of NQ (multiplier 20): high and low of each
BARS = [BarRange(at(0), D("20005"), D("19995")), BarRange(at(1), D("20015"), D("20000")),
        BarRange(at(2), D("20025"), D("20008")), BarRange(at(3), D("20040"), D("20020"))]
# 3 entries (1 NQ each at 20000, 20010, 20020; commission 2.5 each) and 1 exit of all 3 at 20030 (commission 7.5)
FILLS = [Fill(NQ, "buy", D(1), D("20000"), at(0), D("2.5"), "orb_long", {"rsi": 61.5}), Fill(NQ, "buy", D(1), D("20010"), at(1), D("2.5"), "add1"),
         Fill(NQ, "buy", D(1), D("20020"), at(2), D("2.5"), "add2"), Fill(NQ, "sell", D(3), D("20030"), at(3), D("7.5"), "target")]


def grouped():
    return group_trades(FILLS, {NQ: BARS}, {NQ: D(20)})


# ---------------------------------------------------------------------------------------------------------------- B-07
def test_ambiguous_bars_are_counted_with_their_date_B_07():
    bars = [BarRange(at(0), D("20005"), D("19995")),          # touches neither: stop 19990, target 20010
            BarRange(at(1), D("20012"), D("19988")),          # touches both: high 20012 >= 20010 and low 19988 <= 19990
            BarRange(at(2), D("20012"), D("19995"))]          # target only
    assert ambiguous_bars(bars, side="long", stop=D("19990"), target=D("20010")) == (at(1),)
    assert ambiguous_bars(bars[:1] + bars[2:], side="long", stop=D("19990"), target=D("20010")) == ()       # no ambiguity: zero, still a value
    short = [BarRange(at(0), D("20012"), D("19988"))]
    assert ambiguous_bars(short, side="short", stop=D("20010"), target=D("19990")) == (at(0),)
    with pytest.raises(ResultError):
        ambiguous_bars(bars, side="long", stop=D("20010"), target=D("19990"))                               # stop above target for a long


def test_the_result_always_carries_ambiguous_bars_B_07():
    result = make_result(ambiguous=(at(1),))
    assert result.ambiguous_bars == 1 and result.ambiguous_bar_dates == (at(1),)
    assert make_result().ambiguous_bars == 0
    assert results.to_dict(make_result())["ambiguous_bars"] == 0 and "ambiguous_bars" in results.to_dict(result)


# ---------------------------------------------------------------------------------------------------------------- B-14, B-25
def test_trades_carry_the_tag_of_their_entry_and_exit_orders_and_a_blank_tag_is_none_B_14():
    t = grouped().positions[0]
    assert t.entry_tag == "orb_long" and t.tag == "orb_long" and t.exit_tag == "target"
    plain = Trade(NQ, "long", D(1), at(0), D("20000"), None, None, None, D(0), entry_tag="   ", exit_tag="")
    assert plain.tag is None and plain.entry_tag is None and plain.exit_tag is None
    with pytest.raises(ResultError):
        Trade(NQ, "long", D(1), at(0), D("20000"), None, None, None, D(0), entry_tag="x" * 81)


def test_a_trade_carries_the_indicator_values_of_the_decision_bar_B_25():
    position = grouped().positions[0]
    assert position.context == {"rsi": 61.5}                       # the value of the first entry fill, which was decided on the previous bar
    odd = Trade(NQ, "long", D(1), at(0), D("20000"), None, None, None, D(0), context={"b": float("nan"), "a": D("1.25"), "c": "up"})
    assert odd.context == {"a": 1.25, "b": None, "c": "up"} and list(odd.context) == ["a", "b", "c"]
    with pytest.raises(ResultError):
        Trade(NQ, "long", D(1), at(0), D("20000"), None, None, None, D(0), context={"x": [1, 2]})


# ---------------------------------------------------------------------------------------------------------------- V-16
def test_three_entries_and_one_total_exit_make_three_entry_trades_one_exit_trade_and_one_position_V_16():
    g = grouped()
    assert (len(g.entry_trades), len(g.exit_trades), len(g.positions)) == (3, 1, 1)
    # entry trade k: (exit 20030 - entry) * 20 minus its commission 2.5 and a third of the exit commission (2.5): 5.0
    #   E1: 30 * 20 - 5 = 595, MAE (19995 - 20000) * 20 = -100, MFE (20040 - 20000) * 20 = 800   (bars 0 to 3)
    #   E2: 20 * 20 - 5 = 395, MAE (20000 - 20010) * 20 = -200, MFE (20040 - 20010) * 20 = 600   (bars 1 to 3)
    #   E3: 10 * 20 - 5 = 195, MAE (20008 - 20020) * 20 = -240, MFE (20040 - 20020) * 20 = 400   (bars 2 to 3)
    assert [(t.pnl_usd, t.mae_usd, t.mfe_usd, t.costs_usd) for t in g.entry_trades] == [
        (D(595), D(-100), D(800), D(5)), (D(395), D(-200), D(600), D(5)), (D(195), D(-240), D(400), D(5))]
    # exit trade: 3 NQ, average entry 20010, 20 * 3 * 20 - (7.5 + 7.5) = 1185, MAE (19995 - 20010) * 60 = -900, MFE (20040 - 20010) * 60 = 1800
    ex = g.exit_trades[0]
    assert (ex.qty, ex.entry_price, ex.exit_price, ex.pnl_usd, ex.mae_usd, ex.mfe_usd, ex.costs_usd) == (D(3), D(20010), D(20030), D(1185), D(-900), D(1800), D(15))
    pos = g.positions[0]
    assert (pos.qty, pos.entry_price, pos.exit_price, pos.pnl_usd, pos.mae_usd, pos.mfe_usd) == (D(3), D(20010), D(20030), D(1185), D(-900), D(1800))
    assert sum(t.pnl_usd for t in g.entry_trades) == pos.pnl_usd
    for group in (g.entry_trades, g.exit_trades, g.positions):
        assert all(t.mae_usd != 0 and t.mfe_usd != 0 for t in group)          # the grouping trap: never zero


def test_a_cross_through_zero_closes_one_position_and_opens_the_next_V_16():
    fills = [Fill(NQ, "buy", D(1), D("20000"), at(0), D(0)), Fill(NQ, "sell", D(3), D("20010"), at(1), D(0)), Fill(NQ, "buy", D(2), D("20000"), at(3), D(0))]
    g = group_trades(fills, {NQ: BARS}, {NQ: D(20)})
    # long 1 closed at 20010: 200. Short 2 opened at 20010, covered at 20000: 10 * 2 * 20 = 400
    assert [(p.side, p.qty, p.pnl_usd) for p in g.positions] == [("long", D(1), D(200)), ("short", D(2), D(400))]
    assert len(g.entry_trades) == 2 and len(g.exit_trades) == 2
    assert [(t.side, t.qty, t.pnl_usd) for t in g.exit_trades] == [("long", D(1), D(200)), ("short", D(2), D(400))]


def test_an_open_lot_has_no_exit_and_missing_bars_are_an_error_V_16():
    g = group_trades(FILLS[:2], {NQ: BARS}, {NQ: D(20)})
    assert [(t.exit_at, t.pnl_usd) for t in g.positions] == [(None, None)]
    # open: 2 NQ at an average 20005 over all four bars: MAE (19995 - 20005) * 40 = -400, MFE (20040 - 20005) * 40 = 1400
    assert (g.positions[0].qty, g.positions[0].entry_price, g.positions[0].mae_usd, g.positions[0].mfe_usd) == (D(2), D(20005), D(-400), D(1400))
    with pytest.raises(ResultError) as e:
        group_trades(FILLS, {NQ: [BarRange(at(60), D(1), D(1))]}, {NQ: D(20)})
    assert e.value.code == "no_bars"
    with pytest.raises(ResultError):
        group_trades([Fill(NQ, "buy", D(0), D("20000"), at(0))], {NQ: BARS}, {NQ: D(20)})


# ---------------------------------------------------------------------------------------------------------------- V-34, N-21
def test_gross_and_net_exposure_with_multipliers_and_cash_that_matches_the_engine_book_V_34_N_21():
    held = [Holding(NQ, D(1), D("20000"), D(20)), Holding("FUT:ES:2026-03", D(-1), D("5000"), D(50))]
    e = exposure(held, cash_usd=D("1000000"), equity_usd=D("1002500"), at=at(0))
    # long NQ 1 * 20000 * 20 = 400000; short ES 1 * 5000 * 50 = 250000: gross 650000, net 150000
    assert (e.gross_usd, e.net_usd, e.cash_usd, e.value_usd) == (D(650000), D(150000), D(1000000), D(1002500))
    assert e.by_contract == {NQ: D(400000), "FUT:ES:2026-03": D(-250000)}
    assert e.leverage == D(650000) / D(1002500)
    assert cash_mismatches([e], {at(0): D("1000000")}) == ()
    assert cash_mismatches([e], {at(0): D("999999")}) == (at(0),) and cash_mismatches([e], {}) == (at(0),)


# ---------------------------------------------------------------------------------------------------------------- results used below
def equity_curve() -> EquityCurve:
    r = pd.read_csv(FIXTURE, parse_dates=["date"], index_col="date")["return"]
    wealth = (1.0 + r).cumprod() * 100000.0
    return EquityCurve(tuple(i.to_pydatetime().replace(hour=21, tzinfo=UTC) for i in wealth.index), tuple(D(repr(round(v, 2))) for v in wealth))


def make_result(*, ambiguous=(), stats=None, trades=None, status="done", run_id=1, curve=None) -> BacktestResult:
    return BacktestResult(run_id, status, "spec-hash-1", "data-hash-1", "engine-1.0", 7, tuple(trades if trades is not None else grouped().positions),
                          curve or EquityCurve((at(0), at(1)), (D(100000), D("100050.5"))), D(1185), D(0), {"commission": D(15), "slippage": D(0)},
                          len(ambiguous), {"margin": 0}, stats or {}, ("costs_kill_edge",), tuple(ambiguous))


# ---------------------------------------------------------------------------------------------------------------- B-18, V-19, V-21, V-33, B-20, L-33
def test_summary_is_the_table_of_quant_metrics_with_piece_ids_B_18_V_33():
    result = make_result(curve=equity_curve(), trades=grouped().entry_trades)
    table = results.summary(result, multipliers={NQ: D(20)})
    returns = results.equity_returns(result.equity)
    reference = metrics.summary(returns)
    for block in ("header", "returns", "ratios", "psr", "drawdown", "volatility", "operations", "quality"):
        assert table[block].value == reference[block].value and table[block].piece_ids == reference[block].piece_ids and table[block].piece_ids
    assert table["ratios"].piece_ids == ("QS-040a",)
    assert table["psr"].value is not None and table["psr"].value == reference["psr"].value
    assert {"sharpe", "sortino", "calmar", "var", "drawdown_duration", "trade_stats", "sqn", "streaks", "kelly"} <= set(table)
    assert table["drawdown_duration"].piece_ids == ("B-20", "L-31")


def test_summary_calls_compute_and_var_and_the_equity_is_one_return_series_V_19(monkeypatch):
    calls = []
    real_compute, real_var = metrics.compute, risk.var
    monkeypatch.setattr(results.metrics, "compute", lambda *a, **k: calls.append("compute") or real_compute(*a, **k))
    monkeypatch.setattr(results.risk, "var", lambda *a, **k: calls.append("var") or real_var(*a, **k))
    table = results.summary(make_result(curve=equity_curve()))
    assert calls == ["compute", "var"]
    returns = results.equity_returns(equity_curve())
    assert table["sharpe"].value == metrics.compute(returns, ["sharpe"])["sharpe"].value
    assert table["var"].value == risk.var(returns, level=0.95).value
    ppy365 = results.summary(make_result(curve=equity_curve()), periods_per_year=365)
    assert ppy365["sharpe"].value != table["sharpe"].value                  # the annualization reaches the ratios


def test_dsr_of_the_backtest_is_the_one_of_validation_with_the_trial_count_V_21():
    result = make_result(curve=equity_curve())
    table = results.summary(result, n_trials=50, sr_std=0.01)
    expected = validation.dsr(results.equity_returns(result.equity), n_trials=50, sr_std=0.01)
    assert table["dsr"].value == expected.value and table["dsr"].piece_ids == expected.piece_ids
    assert "dsr" not in results.summary(result)
    with pytest.raises(ResultError):
        results.summary(result, n_trials=50)
    with pytest.raises(Exception):
        results.summary(result, n_trials=10_000_000, sr_std=0.01)             # above the cap of limits.PARAM_CAPS


def test_expectancy_of_ten_trades_follows_win_rate_times_payoff_minus_loss_rate_L_33():
    pnl = [100, -50, 100, 100, -50, 100, -50, 100, 100, -50]                      # 6 wins of 100, 4 losses of 50
    # win rate 0.6, payoff 100 / 50 = 2: expectancy 0.6 * 2 - 0.4 = 0.8 R; in USD (600 - 200) / 10 = 40
    t0 = datetime(2026, 1, 5, tzinfo=UTC)
    trades = [Trade(NQ, "long", D(1), t0 + timedelta(hours=i), D("20000"), t0 + timedelta(hours=i, minutes=30), D("20010"), D(p), D(0)) for i, p in enumerate(pnl)]
    table = results.summary(make_result(curve=equity_curve(), trades=trades), multipliers={NQ: D(20)})
    stats = table["trade_stats"].value
    assert stats["expectancy_r"] == pytest.approx(0.8, abs=1e-12) and stats["expectancy_usd"] == pytest.approx(40, abs=1e-12)
    assert stats["win_rate"] == pytest.approx(0.6) and stats["payoff_ratio"] == pytest.approx(2.0)
    assert stats["expectancy_r"] == trade_metrics.expectancy_r(stats["win_rate"], stats["payoff_ratio"])


def test_results_module_imports_nothing_from_the_engine_B_18():
    tree = ast.parse(Path(results.__file__).read_text(encoding="utf-8"))
    names = {n.module.split(".")[0] if isinstance(n, ast.ImportFrom) else a.name.split(".")[0]
             for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in (n.names if isinstance(n, ast.Import) else [n])
             if not (isinstance(n, ast.ImportFrom) and n.level)}
    allowed = {"__future__", "hashlib", "json", "math", "os", "collections", "dataclasses", "datetime", "decimal", "pathlib", "typing", "pandas", "hokma"}
    assert names <= allowed, names - allowed          # the standard library, pandas and hokma itself: no statistics or engine library
    assert "hokma" in names


def test_drawdown_duration_comes_from_the_drawdown_module_B_20():
    equity = EquityCurve(tuple(datetime(2025, 1, 1 + i, 21, tzinfo=UTC) for i in range(5)), tuple(D(v) for v in (100, 90, 95, 90, 100)))
    table = results.summary(make_result(curve=equity))
    # peak on day 1, back to 100 on day 5: 4 calendar days
    assert table["drawdown_duration"].value["max_recovery_days"] == 4


def test_the_full_report_is_the_html_of_quant_metrics_report_html_without_external_resources_V_35():
    result = make_result(curve=equity_curve())
    html = results.report_html(result, title="Run <1>")
    assert html == metrics.report_html(results.equity_returns(result.equity), title="Run <1>")
    assert "Run <1>" not in html and "&lt;1&gt;" in html
    assert not any(marker in html for marker in ("http://", "https://", "<script src", "<link ", "@import"))


# ---------------------------------------------------------------------------------------------------------------- N-26
def test_the_result_goes_to_json_and_comes_back_equal_N_26():
    result = results.with_stats(make_result(curve=equity_curve(), ambiguous=(at(1),)), multipliers={NQ: D(20)})
    text = results.to_json(result)
    back = results.from_json(text)
    assert back == result and results.to_json(back) == text
    data = json.loads(text)
    assert data["spec_hash"] == "spec-hash-1" and data["data_hash"] == "data-hash-1" and data["engine_version"] == "engine-1.0" and data["seed"] == 7
    assert data["ambiguous_bars"] == 1 and "NaN" not in text


# ---------------------------------------------------------------------------------------------------------------- L-40, V-15, P-11
def new_run(db, *, spec_hash="spec-hash-1", seed=7) -> int:
    with db.write() as u:
        return u.session.insert("backtest_runs", {"kind": "run", "spec_json": "{}", "spec_hash": spec_hash, "engine_version": "engine-1.0", "seed": seed,
                                                  "status": "running"})


def check_finished_run_is_saved_with_one_row_per_trade_and_a_hashed_curve(database, tmp_path):
    run_id = new_run(database)
    result = results.with_stats(make_result(curve=equity_curve(), run_id=run_id, trades=grouped().entry_trades + grouped().positions), multipliers={NQ: D(20)})
    digest = results.record_result(database, result, curve_dir=tmp_path / "curves")
    with database.read() as u:
        run = u.session.one("SELECT * FROM backtest_runs WHERE id = :i", {"i": run_id})
        rows = u.session.query("SELECT * FROM backtest_trades WHERE run_id = :i ORDER BY id", {"i": run_id})
    assert run["status"] == "done" and run["finished_at"] and run["error"] is None and run["data_hash"] == "data-hash-1"
    assert run["result_path"] == f"curve-{run_id}.json" and run["result_hash"] == digest
    assert hashlib.sha256((tmp_path / "curves" / run["result_path"]).read_bytes()).hexdigest() == digest
    assert json.loads(run["summary_json"])["ambiguous_bars"] == 0 and "ratios" in json.loads(run["summary_json"])["stats"]
    assert len(rows) == 4                                                              # one row per trade: three entry trades and one position
    first = rows[0]
    assert (first["contract_id"], first["side"]) == (NQ, "long")
    assert [D(first[k]) for k in ("qty", "entry_price", "exit_price", "pnl_usd", "costs_usd", "mae_usd", "mfe_usd")] == [D(1), D(20000), D(20030), D(595), D(5), D(-100), D(800)]
    assert (first["entry_tag"], first["exit_tag"]) == ("orb_long", "target")
    assert json.loads(first["context_json"]) == {"rsi": 61.5} and rows[1]["entry_tag"] == "add1"
    assert results.load_trades(database, run_id) == result.trades
    assert results.load_curve(database, run_id, curve_dir=tmp_path / "curves") == result.equity
    with pytest.raises(ResultError):                                                   # a finished run is not written twice
        results.record_result(database, result, curve_dir=tmp_path / "curves")
    (tmp_path / "curves" / run["result_path"]).write_bytes(b"{}")
    with pytest.raises(ResultError) as e:
        results.load_curve(database, run_id, curve_dir=tmp_path / "curves")
    assert e.value.code == "bad_curve"


def test_a_finished_run_has_its_row_its_trades_and_a_curve_with_a_hash_L_40_V_15(db, tmp_path):
    check_finished_run_is_saved_with_one_row_per_trade_and_a_hashed_curve(db, tmp_path)


def test_a_finished_run_is_saved_the_same_on_postgres_V_15(env, pg_url_migrated, tmp_path):
    from hokma.db import get_database, migrate
    from hokma.db.dbconfig import DatabaseConfig, save_config

    save_config(DatabaseConfig("postgres", pg_url_migrated))
    migrate()
    database = get_database()
    assert database.engine == "postgres"
    check_finished_run_is_saved_with_one_row_per_trade_and_a_hashed_curve(database, tmp_path)


def test_a_failed_run_has_its_error_and_no_trade_L_40(db, tmp_path):
    run_id = new_run(db)
    results.record_failure(db, run_id, "engine crashed at bar 17")
    with db.read() as u:
        run = u.session.one("SELECT * FROM backtest_runs WHERE id = :i", {"i": run_id})
        assert u.session.scalar("SELECT COUNT(*) FROM backtest_trades WHERE run_id = :i", {"i": run_id}) == 0
    assert run["status"] == "failed" and run["error"] == "engine crashed at bar 17" and run["result_path"] is None and run["finished_at"]
    with pytest.raises(ResultError):
        results.record_failure(db, run_id, "again")
    with pytest.raises(ResultError):
        results.record_result(db, make_result(run_id=new_run(db), status="failed"), curve_dir=tmp_path)


def test_a_result_that_does_not_belong_to_its_run_leaves_nothing_behind_L_40(db, tmp_path):
    run_id = new_run(db, spec_hash="another-hash")
    with pytest.raises(ResultError):
        results.record_result(db, make_result(run_id=run_id), curve_dir=tmp_path / "curves")
    with db.read() as u:
        assert u.session.scalar("SELECT COUNT(*) FROM backtest_trades WHERE run_id = :i", {"i": run_id}) == 0
        assert u.session.one("SELECT status, result_path FROM backtest_runs WHERE id = :i", {"i": run_id}) == {"status": "running", "result_path": None}
    assert not (tmp_path / "curves" / f"curve-{run_id}.json").exists()
    with pytest.raises(ResultError):
        results.record_result(db, make_result(run_id=99999), curve_dir=tmp_path)


def test_accounting_and_curves_are_one_implementation_P_11():
    source = Path(results.__file__).read_text(encoding="utf-8")
    assert source.count("def equity_returns") == 1 and source.count("def record_result") == 1 and source.count("def to_dict") == 1
