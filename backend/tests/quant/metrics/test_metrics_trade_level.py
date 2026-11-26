"""Trade level and annualization metrics of the backtest result (SPEC-020, 5.4.4).

Rows: B-19, B-20, B-21, B-22, B-23, B-24, B-48, V-17, V-18, V-19, V-20, V-32, V-35, L-28, L-29, L-31, L-32, L-34, L-35, N-23, N-24.
Every expected value is worked out by hand in the comments.
"""
import ast
import importlib
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from hokma.quant import metrics, registry, risk
from hokma.quant.errors import BadInput
from hokma.quant.metrics import drawdown, returns as returns_module, trades

from .metrics_b_qa import assert_close, call

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[3]
MY_NAMES = ("metrics.trade_stats", "metrics.sqn", "metrics.trade_streaks", "metrics.periods_per_year", "metrics.cagr_by_date",
            "metrics.drawdown_episodes", "metrics.drawdown_duration")
ORDER = ("returns", "summary", "trades", "volatility", "drawdown", "distribution", "ratios", "locks", "accessor", "tail_view", "montecarlo",
         "attribution", "benchmark", "report")


@pytest.fixture(autouse=True)
def registered():
    """Other test files reset the registry; load the metrics modules again when that happened."""
    names = {s.name for s in registry.list("metrics")}
    if not set(MY_NAMES) <= names:
        with registry._LOCK:
            for name in [n for n in registry._FUNCTIONS if n.startswith("metrics.")]:
                registry._FUNCTIONS.pop(name)
        for mod in ORDER:
            importlib.reload(importlib.import_module(f"hokma.quant.metrics.{mod}"))
    yield


# 10 closed trades, 3 hours apart, 1 NQ at 20000 (multiplier 20): result in USD and minutes held
PNL = [200, -100, 150, -50, 300, -120, 80, -60, 250, 30]
MINUTES = [30, 60, 90, 30, 120, 60, 30, 90, 60, 30]
START = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)
ROWS = [{"pnl_usd": p, "entry_at": START + timedelta(hours=3 * i), "exit_at": START + timedelta(hours=3 * i, minutes=m), "entry_price": 20000.0,
         "qty": 1, "multiplier": 20, "costs_usd": 4.5, "mae_usd": -(p % 7) * 10.0, "mfe_usd": 100.0 + i} for i, (p, m) in enumerate(zip(PNL, MINUTES))]


def stats(rows=None):
    return call("metrics.trade_stats", trades=rows or ROWS)


def test_trade_stats_of_ten_trades_B_19_B_23_V_17():
    res = stats()
    v = res.value
    # wins 200 150 300 80 250 30 = 1010 (6), losses -100 -50 -120 -60 = -330 (4): total 680, mean 68
    assert (v["n_trades"], v["n_wins"], v["n_losses"]) == (10, 6, 4)
    assert_close(v["win_rate"], 0.6) and assert_close(v["loss_rate"], 0.4)
    assert_close([v["total_pnl_usd"], v["gross_profit_usd"], v["gross_loss_usd"], v["avg_pnl_usd"]], [680, 1010, -330, 68])
    assert_close([v["largest_win_usd"], v["largest_loss_usd"]], [300, -120])            # best and worst trade
    assert_close([v["avg_win_usd"], v["avg_loss_usd"]], [1010 / 6, -82.5])
    assert_close(v["payoff_ratio"], (1010 / 6) / 82.5) and assert_close(v["profit_factor"], 1010 / 330)      # 3.0606...
    assert_close(v["expectancy_usd"], 68) and assert_close(v["expectancy_r"], 0.6 * (1010 / 6) / 82.5 - 0.4)
    # durations: 600 minutes over 10 trades = 3600 seconds; winners 360 / 6 = 3600; losers 240 / 4 = 3600
    assert_close([v["avg_duration_seconds"], v["avg_win_duration_seconds"], v["avg_loss_duration_seconds"]], [3600, 3600, 3600])
    # exposure: 600 minutes open in the 27.5 hours (1650 minutes) from the first entry to the last exit
    assert_close(v["exposure"], 600 / 1650)
    # return per trade: result over the notional 20000 * 1 * 20 = 400000
    assert_close([v["avg_return_per_trade"], v["best_return_per_trade"], v["worst_return_per_trade"]], [68 / 400000, 300 / 400000, -120 / 400000])
    assert_close(v["total_costs_usd"], 45.0)
    assert (v["max_consecutive_wins"], v["max_consecutive_losses"]) == (2, 1)
    assert res.piece_ids == ("B-19", "B-23", "V-17", "L-34")


def test_win_rate_payoff_and_profit_factor_are_the_functions_of_the_return_series_B_23_V_17():
    """One implementation: the per trade figures equal QS-008 and QS-025 run on the result of each trade."""
    pnl = pd.Series(PNL, index=pd.bdate_range("2026-01-05", periods=10), dtype=float)
    v = stats().value
    win = call("metrics.win_rate_stats", returns=pnl).value
    ratios = call("metrics.payoff_ratios", returns=pnl).value
    assert v["win_rate"] == win["win_rate"] and v["avg_win_usd"] == win["avg_win"] and v["avg_loss_usd"] == win["avg_loss"]
    assert v["payoff_ratio"] == ratios["payoff_ratio"] and v["profit_factor"] == ratios["profit_factor"]


def test_no_loss_means_profit_factor_none_with_a_warning_not_infinity_B_23():
    res = stats([{"pnl_usd": 10.0}, {"pnl_usd": 20.0}, {"pnl_usd": 5.0}])
    assert res.value["profit_factor"] is None and any("no losing trades" in w for w in res.warnings)
    assert res.value["n_losses"] == 0 and res.value["avg_loss_usd"] is None


def test_the_registry_has_no_repeated_names_and_no_second_formula_B_19():
    names = [s.name for s in registry.list(include_blocked=True)]
    assert len(names) == len(set(names))
    text = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "hokma" / "quant").rglob("*.py"))
    for definition in ("def sqn_value", "def streak_runs", "def expectancy_r", "def mae_mfe", "def win_rate_value", "def profit_factor_value"):
        assert text.count(definition) == 1, definition


def test_trade_stats_checks_its_input_B_19():
    with pytest.raises(BadInput):
        call("metrics.trade_stats", trades=[{"qty": 1}])
    with pytest.raises(BadInput):
        call("metrics.trade_stats", trades="nope")
    assert call("metrics.trade_stats", trades=[]).value is None
    res = call("metrics.trade_stats", trades=[{"pnl_usd": float("nan")}, {"pnl_usd": 5.0}])
    assert res.value["n_trades"] == 1 and any("dropped 1" in w for w in res.warnings)


# 25 trades: 15 of +10 and 10 of -10. mean (150 - 100) / 25 = 2; squared deviations 15 * 8^2 + 10 * 12^2 = 2400; variance 2400 / 24 = 100; std 10
def test_sqn_of_25_trades_B_21_V_17():
    res = call("metrics.sqn", pnl=[10.0] * 15 + [-10.0] * 10)
    assert_close(res.value, math.sqrt(25) * 2 / 10, tol=1e-9)          # 1.0
    ten = call("metrics.sqn", pnl=PNL).value
    # squares sum 252800; minus 10 * 68^2 = 46240 gives 206560; variance 206560 / 9
    assert_close(ten, math.sqrt(10) * 68 / math.sqrt(206560 / 9), tol=1e-9) and assert_close(stats().value["sqn"], ten, tol=1e-12)
    assert res.piece_ids == ("B-21", "V-17")


def test_sqn_with_fewer_than_two_trades_is_none_with_a_warning_B_21():
    for pnl in ([], [5.0]):
        res = call("metrics.sqn", pnl=pnl)
        assert res.value is None and any("at least 2 trades" in w for w in res.warnings)
    same = call("metrics.sqn", pnl=[5.0, 5.0, 5.0])
    assert same.value is None and same.warnings


def test_kelly_is_the_function_of_QS_031_with_a_cap_notice_and_never_negative_B_22():
    assert trades.kelly is trades.kelly_cap
    assert {"QS-031", "B-22"} <= set(registry.describe("metrics.kelly_cap").piece_ids)
    losing = pd.Series([0.01, -0.03, 0.01, -0.03, 0.02, -0.04, 0.01, -0.02], index=pd.bdate_range("2022-01-03", periods=8))
    res = call("metrics.kelly_cap", returns=losing)
    assert res.value["position_cap"] == 0.0 and res.value["kelly"] < 0
    assert any("cap, not a target" in w for w in res.warnings)
    # FM-06: a bet with a losing edge is sized at 0, never below
    assert registry.call("risk.kelly_payoff", p_win=0.3, payoff=1.0).value == 0.0
    assert registry.call("risk.kelly_payoff", p_win=0.6, payoff=1.0).value == pytest.approx(0.2)


def test_streaks_are_the_longest_runs_and_a_tie_cuts_them_V_18():
    res = call("metrics.trade_streaks", pnl=[1, 2, 3, 4, -1, 1, -1, -2, -3, 1])
    assert res.value == {"max_consecutive_wins": 4, "max_consecutive_losses": 3}
    cut = call("metrics.trade_streaks", pnl=[5, 5, 0, 5, 5, -1, -1, 0, -1])
    assert cut.value == {"max_consecutive_wins": 2, "max_consecutive_losses": 2}
    assert trades.streak_runs([1, 1, 1, 0, 1, 1]) == (3, 0)
    assert call("metrics.trade_streaks", pnl=[]).value is None


# MAE and MFE of three trades over the bars of the fixture (multiplier 20, one contract each)
BARS_HIGH = [20005.0, 20015.0, 20025.0, 20040.0]
BARS_LOW = [19995.0, 20000.0, 20008.0, 20020.0]


def test_mae_and_mfe_of_three_trades_in_usd_with_the_multiplier_L_34_L_35():
    # long 1 from 20000 over bars 0..3: MAE (19995 - 20000) * 20 = -100, MFE (20040 - 20000) * 20 = 800
    assert trades.mae_mfe("long", 20000.0, 1, 20, BARS_HIGH, BARS_LOW) == (-100.0, 800.0)
    # long 2 from 20010 over bars 1..3: MAE (20000 - 20010) * 40 = -400, MFE (20040 - 20010) * 40 = 1200
    assert trades.mae_mfe("long", 20010.0, 2, 20, BARS_HIGH[1:], BARS_LOW[1:]) == (-400.0, 1200.0)
    # short 1 from 20020 over bars 2..3: MAE (20020 - 20040) * 20 = -400, MFE (20020 - 20008) * 20 = 240
    assert trades.mae_mfe("short", 20020.0, 1, 20, BARS_HIGH[2:], BARS_LOW[2:]) == (-400.0, 240.0)
    # a trade that is never hurt has MAE 0 (not negative) and one never helped has MFE 0
    assert trades.mae_mfe("long", 19990.0, 1, 20, BARS_HIGH, BARS_LOW) == (0.0, 1000.0)
    rows = [{"pnl_usd": 600.0, "mae_usd": -100.0, "mfe_usd": 800.0}, {"pnl_usd": 100.0, "mae_usd": -400.0, "mfe_usd": 1200.0},
            {"pnl_usd": -200.0, "mae_usd": -400.0, "mfe_usd": 240.0}]
    v = stats(rows).value
    assert_close([v["avg_mae_usd"], v["avg_mfe_usd"], v["largest_mae_usd"], v["largest_mfe_usd"]], [-300.0, 746.6666666666666, -400.0, 1200.0])
    with pytest.raises(BadInput):
        trades.mae_mfe("long", 20000.0, 1, 20, [], [])
    with pytest.raises(BadInput):
        trades.mae_mfe("sideways", 20000.0, 1, 20, BARS_HIGH, BARS_LOW)


# ---------------------------------------------------------------------------------------------------------------- B-24, V-32, N-23
def ppy(asset, bar, **kw):
    return call("metrics.periods_per_year", asset_type=asset, bar=bar, **kw).value


def test_periods_per_year_follow_the_session_of_the_asset_B_24_V_32_N_23():
    assert ppy("future", "1m") == 23 * 60 * 252 == 347_760            # NQ: 1380 one minute bars in a 23 hour session
    assert ppy("equity", "1m") == 390 * 252 == 98_280                  # SPY: 6.5 hours
    assert ppy("etf", "1d") == 252 and ppy("crypto", "1d") == 365 and ppy("future", "1d") == 252
    assert ppy("crypto", "1m") == 1440 * 365
    assert ppy("future", "1m", session="RTH") == 390 * 252             # the regular session of a future is 6.5 hours too
    assert ppy("future", "1h") == 23 * 252 and ppy("future", "15m") == 92 * 252 and ppy("equity", "1h") == 7 * 252   # 6.5 hours: a partial last bar counts
    assert ppy("equity", "1w") == 52 and ppy("future", "5m", session_hours=8) == 96 * 252
    assert ppy("future", "1m") != 252 * 390                            # the trap: 252 times the stock session for a 23 hour future
    for bad in ({"asset_type": "stonks", "bar": "1d"}, {"asset_type": "future", "bar": "3m"}, {"asset_type": "future", "bar": "1m", "session": "xx"},
                {"asset_type": "future", "bar": "1m", "session_hours": 25}):
        with pytest.raises(BadInput):
            call("metrics.periods_per_year", **bad)
    assert {"B-24", "V-32", "N-23"} <= set(registry.describe("metrics.periods_per_year").piece_ids)


def test_a_sharpe_annualized_with_the_wrong_periods_does_not_pass_B_24():
    r = pd.Series([0.001, -0.0005, 0.002, 0.0, 0.0015, -0.001, 0.0005, 0.0012], index=pd.date_range("2026-03-02 15:00", periods=8, freq="min"))
    mean = sum(r) / 8
    sd = math.sqrt(sum((x - mean) ** 2 for x in r) / 7)
    right = ppy("future", "1m")
    got = call("metrics.sharpe", returns=r, periods_per_year=right).value
    assert_close(got, mean / sd * math.sqrt(347_760), tol=1e-9)
    assert abs(got - mean / sd * math.sqrt(252)) > 1.0                # the fixed 252 gives another number


def test_cagr_by_date_uses_fractional_years_from_the_dates_B_48():
    idx = pd.date_range("2024-01-31", periods=18, freq="ME")          # 516 days from the first to the last date
    r = pd.Series([0.02, 0.0] * 9, index=idx)
    got = call("metrics.cagr_by_date", returns=r).value
    assert_close(got, (1.02 ** 9) ** (365.25 / 516) - 1, tol=1e-12)
    assert abs(got - (1.02 ** 9) ** (252 / 18) + 1) > 1               # counting bars over 252 gives a CAGR of more than 1000 percent
    assert call("metrics.cagr_by_date", returns=pd.Series([0.01], index=idx[:1])).value is None


# ---------------------------------------------------------------------------------------------------------------- B-20, V-20, L-31
def equity_returns(values):
    eq = pd.Series(values, index=pd.date_range("2025-01-01", periods=len(values)), dtype=float)
    r = eq.pct_change()
    r.iloc[0] = 0.0
    return r


def test_maximum_drawdown_duration_of_a_hand_series_B_20_L_31():
    # 100 90 95 100 98 99 97 96: peak day 1; recovered day 4 (3 days); new peak 100 on day 4, under water until the last day 8 (4 days, open)
    res = drawdown.drawdown_duration(equity_returns([100, 90, 95, 100, 98, 99, 97, 96]))
    assert res.value["max_recovery_days"] == 3 and res.value["max_underwater_days"] == 4 and res.value["current_underwater_days"] == 4
    assert res.value["n_episodes"] == 2 and res.value["peaks"] == ["2025-01-01", "2025-01-04"]
    assert {"B-20", "L-31"} <= set(res.piece_ids)
    assert call("metrics.drawdown_duration", returns=equity_returns([100, 90, 100])).value["max_recovery_days"] == 2


def test_drawdown_episodes_have_peak_valley_recovery_and_an_open_one_stays_open_V_20_L_31():
    eps = drawdown.drawdown_episode_objects(equity_returns([100, 90, 95, 100, 98, 99, 97, 96])).value
    assert eps[0] == {"peak": "2025-01-01", "start": "2025-01-02", "valley": "2025-01-02", "recovery": "2025-01-04", "open": False, "days": 3,
                      "depth": pytest.approx(-0.1)}
    assert eps[1]["peak"] == "2025-01-04" and eps[1]["valley"] == "2025-01-08" and eps[1]["recovery"] is None and eps[1]["open"] is True
    assert eps[1]["days"] == 4 and eps[1]["depth"] == pytest.approx(96 / 100 - 1)


def test_drawdown_uses_the_one_formula_of_the_module_B_20():
    """The episodes come from summary.drawdown_episodes: the depth of the worst one is metrics.max_drawdown (QS-030)."""
    r = pd.read_csv(Path(__file__).resolve().parents[1] / "fixtures" / "returns_daily.csv", parse_dates=["date"], index_col="date")["return"]
    worst = min(e["depth"] for e in drawdown.drawdown_episode_objects(r).value)
    assert worst == pytest.approx(call("metrics.max_drawdown", returns=r).value, abs=1e-12)
    source = (ROOT / "hokma" / "quant" / "metrics" / "drawdown.py").read_text(encoding="utf-8")
    assert "episode_objects" in source and "underwater(r)" in source and "cummax" not in source   # no second running peak: the episodes use summary.underwater


# ---------------------------------------------------------------------------------------------------------------- V-19, L-28, L-29, L-32, N-24, V-35
def test_the_backtest_uses_compute_and_var_and_no_other_sharpe_formula_exists_in_its_code_V_19():
    source = (ROOT / "hokma" / "backtest" / "results.py").read_text(encoding="utf-8")
    assert "metrics.compute(" in source and "risk.var(" in source
    for folder in ("backtest", "backoffice"):
        for path in (ROOT / "hokma" / folder).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
            assert not defined & {"sharpe", "sharpe_ratio", "annualized_sharpe", "sortino", "sortino_ratio", "calmar"}, path
    assert metrics.compute is not None and callable(risk.var)


def test_no_new_annual_formula_exists_L_28():
    annual = sorted(s.name for s in registry.list("metrics") if "annual" in s.name or "cagr" in s.name or "periods_per_year" in s.name)
    assert annual == ["metrics.annualize", "metrics.cagr", "metrics.cagr_by_date", "metrics.cagr_from_prices", "metrics.deannualize",
                      "metrics.montecarlo_cagr", "metrics.periods_per_year"]


# annual returns 10% -5% 20% 0% 15% with a risk free rate of 3% and one period per year: excess 7% -8% 17% -3% 12%
# mean 5%; squared deviations 0.0004 + 0.0169 + 0.0144 + 0.0064 + 0.0049 = 0.043; sample std sqrt(0.043 / 4) = 0.103682
# downside: (0.08^2 + 0.03^2) / 5 = 0.00146, root 0.038210
def test_sharpe_and_sortino_with_a_risk_free_rate_L_29():
    r = pd.Series([0.10, -0.05, 0.20, 0.0, 0.15], index=pd.date_range("2022-01-03", periods=5, freq="D"))
    assert_close(call("metrics.sharpe", returns=r, rf=0.03, periods_per_year=1).value, 0.05 / math.sqrt(0.043 / 4), tol=1e-9)
    assert_close(call("metrics.sortino", returns=r, rf=0.03, periods_per_year=1).value, 0.05 / math.sqrt(0.00146), tol=1e-9)
    assert {"QS-011", "FF-007"} <= set(registry.describe("metrics.sharpe").piece_ids)


def test_tracking_error_against_a_reference_L_32():
    # active returns 0.5% 1% -1% 1%: mean 0.375%; squared deviations sum 0.00026875; sample std sqrt(0.00026875 / 3)
    idx = pd.date_range("2022-01-03", periods=4, freq="D")
    r, b = pd.Series([0.01, 0.02, -0.01, 0.03], index=idx), pd.Series([0.005, 0.01, 0.0, 0.02], index=idx)
    assert_close(call("metrics.tracking_error", returns=r, benchmark=b, periods_per_year=1).value, math.sqrt(0.00026875 / 3), tol=1e-9)
    assert "FT-071" in registry.describe("metrics.tracking_error").piece_ids


def test_tail_and_downside_functions_already_exist_N_24():
    for name, piece in (("metrics.ulcer_index_pct", "FF-009"), ("metrics.omega", "QS-015"), ("metrics.ulcer_index", "QS-020"), ("metrics.tail_ratio", "QS-024"), ("metrics.rachev_ratio", "FT-074"),
                        ("metrics.gain_to_pain_ratio", "FT-074")):
        assert piece in registry.describe(name).piece_ids, name
    assert registry.describe("risk.cvar").fn is not None                     # expected shortfall of the risk module (QS-015 family)
    r = pd.Series([0.02, -0.01, 0.03, -0.02], index=pd.date_range("2022-01-03", periods=4, freq="D"))
    assert_close(call("metrics.omega", returns=r).value, 0.05 / 0.03, tol=1e-9)           # gains 0.05 over losses 0.03


def test_the_full_report_is_the_html_of_report_html_without_external_resources_V_35():
    r = pd.read_csv(Path(__file__).resolve().parents[1] / "fixtures" / "returns_daily.csv", parse_dates=["date"], index_col="date")["return"]
    html = metrics.report_html(r, title="Run <1>")
    assert html.lstrip().lower().startswith("<!doctype html") and "&lt;1&gt;" in html and "Run <1>" not in html
    for marker in ("http://", "https://", "<script src", "<link ", "@import"):
        assert marker not in html, marker
