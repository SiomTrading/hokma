"""Metrics part B: summary blocks, comparison table, fund metrics and statistics sheets (QS-034, QS-038 to QS-048, AH-36, FF-001 to FF-003)."""
import itertools
import math
from statistics import NormalDist

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
import importlib

from hokma.quant.metrics import _lib

summary = importlib.import_module("hokma.quant.metrics.summary")  # the package attribute "summary" is the entry point function

from .metrics_b_qa import assert_bad_data_gives_none, assert_close, assert_registered, benchmark, call, returns

R = returns()
B = benchmark()
N = len(R)


def lib():
    return _lib.returns_stats()


def test_header_QS_038():
    assert_registered("metrics.summary_header", "QS-038")
    res = call("metrics.summary_header", returns=R)
    exposure_hand = math.ceil(((R != 0).sum() / N) * 100) / 100
    assert res.value["start"] == "2021-01-04" and res.value["end"] == "2023-11-27"
    assert res.value["observations"] == 756
    assert_close(res.value["time_in_market"], exposure_hand)
    assert_close(res.value["time_in_market"], lib().exposure(R, prepare_returns=False))
    assert res.as_of is not None and res.inputs_hash
    assert_bad_data_gives_none(lambda s: call("metrics.summary_header", returns=s))


def test_returns_QS_039():
    assert_registered("metrics.summary_returns", "QS-039")
    res = call("metrics.summary_returns", returns=R)
    cumulative = float(np.prod(1 + R.to_numpy()) - 1)
    assert_close(res.value["cumulative_return"], cumulative)
    assert_close(res.value["total_return"], float(R.sum()))
    assert_close(res.value["cagr"], (1 + cumulative) ** (252 / N) - 1)
    assert_close(res.value["cagr"], lib().cagr(R, 0.0, True, 252))
    assert_close(res.value["cumulative_return"], lib().comp(R))
    assert_bad_data_gives_none(lambda s: call("metrics.summary_returns", returns=s))


def test_ratios_QS_040a():
    assert_registered("metrics.summary_ratios", "QS-040a")
    res = call("metrics.summary_ratios", returns=R)
    sharpe_hand = R.mean() / R.std(ddof=1) * math.sqrt(252)
    downside = math.sqrt((R[R < 0] ** 2).sum() / N)
    sortino_hand = R.mean() / downside * math.sqrt(252)
    gains, losses = R.clip(lower=0).sum(), (-R.clip(upper=0)).sum()
    assert_close(res.value["sharpe"], sharpe_hand)
    assert_close(res.value["sortino"], sortino_hand)
    assert_close(res.value["sortino_over_sqrt2"], sortino_hand / math.sqrt(2))
    assert_close(res.value["omega"], gains / losses)
    assert_close(res.value["smart_sharpe"], lib().smart_sharpe(R, 0.0, 252, True))
    assert_close(res.value["smart_sortino"], lib().smart_sortino(R, 0.0, 252, True))
    assert "probabilistic_sharpe_ratio" not in res.value and "prob_sharpe" not in res.value
    assert_bad_data_gives_none(lambda s: call("metrics.summary_ratios", returns=s))


def test_psr_row_QS_040b():
    from hokma.quant.validation import overfit
    assert_registered("metrics.summary_psr_row", "QS-040b")
    row = call("metrics.summary_psr_row", returns=R)
    direct = overfit.psr(R, sr_benchmark=0.0)
    assert row.value is not None and direct.value is not None
    assert row.value == direct.value
    assert "validation" in row.method
    assert_bad_data_gives_none(lambda s: call("metrics.summary_psr_row", returns=s))


def test_drawdown_QS_041():
    assert_registered("metrics.summary_drawdown", "QS-041")
    res = call("metrics.summary_drawdown", returns=R)
    wealth = (1 + R).cumprod()
    dd = wealth / wealth.cummax() - 1
    assert_close(res.value["max_drawdown"], float(dd.min()))
    assert res.value["max_dd_date"] == str(dd.idxmin())[:10]
    assert_close(res.value["max_drawdown"], lib().max_drawdown(R))
    ref = lib().drawdown_details(lib().to_drawdown_series(R))
    assert res.value["longest_dd_days"] == int(ref["days"].max())
    worst = ref.sort_values("max drawdown").iloc[0]
    assert res.value["max_dd_start"] == str(worst["start"])[:10]
    assert res.value["max_dd_end"] == str(worst["end"])[:10]
    flat = pd.Series([0.01, 0.02, 0.01, 0.03], index=pd.bdate_range("2022-01-03", periods=4)) + np.array([0, 0.001, 0, 0.002])
    up = call("metrics.summary_drawdown", returns=flat)
    assert up.value["max_drawdown"] == 0.0 and up.value["episodes"] == 0
    assert_bad_data_gives_none(lambda s: call("metrics.summary_drawdown", returns=s))


def test_volatility_QS_042():
    assert_registered("metrics.summary_volatility", "QS-042")
    res = call("metrics.summary_volatility", returns=R, benchmark=B)
    assert_close(res.value["volatility_annual"], R.std(ddof=1) * math.sqrt(252))
    assert_close(res.value["skew"], R.skew())
    assert_close(res.value["kurtosis"], R.kurt())
    assert_close(res.value["risk_return_ratio"], R.mean() / R.std(ddof=1))
    assert_close(res.value["r_squared"], lib().r_squared(R, B, prepare_returns=False))
    assert_close(res.value["information_ratio"], lib().information_ratio(R, B, prepare_returns=False))
    assert_close(res.value["calmar"], lib().calmar(R))
    no_bench = call("metrics.summary_volatility", returns=R)
    assert no_bench.value["r_squared"] is None and no_bench.value["information_ratio"] is None
    assert_bad_data_gives_none(lambda s: call("metrics.summary_volatility", returns=s))


def test_operations_QS_043():
    assert_registered("metrics.summary_operations", "QS-043")
    res = call("metrics.summary_operations", returns=R)
    wins, losses = R[R > 0], R[R < 0]
    assert_close(res.value["avg_win"], wins.mean())
    assert_close(res.value["avg_loss"], losses.mean())
    assert_close(res.value["avg_return"], R[R != 0].mean())
    assert_close(res.value["win_loss_ratio"], wins.mean() / abs(losses.mean()))
    assert_close(res.value["profit_ratio"], lib().profit_ratio(R, prepare_returns=False))
    assert_bad_data_gives_none(lambda s: call("metrics.summary_operations", returns=s))


def test_expectations_QS_044a():
    assert_registered("metrics.summary_expectations", "QS-044a")
    res = call("metrics.summary_expectations", returns=R)
    mu, sigma = R.mean(), R.std(ddof=1)
    var_hand = NormalDist(mu, sigma).inv_cdf(0.05)
    z = NormalDist().inv_cdf(0.05)
    es_hand = mu - sigma * NormalDist().pdf(z) / 0.05
    assert_close(res.value["expected_daily"], float(np.prod(1 + R.to_numpy()) ** (1 / N) - 1))
    assert_close(res.value["daily_var"], -abs(var_hand))
    assert_close(res.value["expected_shortfall"], -abs(es_hand))
    assert_close(res.value["kelly_criterion"], lib().kelly_criterion(R, prepare_returns=False))
    monthly = (1 + R).resample("ME").prod() - 1
    assert_close(res.value["expected_monthly"], float(np.prod(1 + monthly) ** (1 / len(monthly)) - 1))
    assert_bad_data_gives_none(lambda s: call("metrics.summary_expectations", returns=s))


def test_ruin_row_QS_044b():
    from hokma.quant.metrics import trades
    assert_registered("metrics.summary_ruin_row", "QS-044b")
    row = call("metrics.summary_ruin_row", returns=R)
    fn = trades.risk_of_ruin(R)
    assert row.value == fn.value and row.value is not None
    assert "adjustment" in row.method
    assert_bad_data_gives_none(lambda s: call("metrics.summary_ruin_row", returns=s))


def _longest(flags):
    return max((len(list(g)) for k, g in itertools.groupby(flags) if k), default=0)


def test_streaks_QS_045():
    assert_registered("metrics.summary_streaks", "QS-045")
    res = call("metrics.summary_streaks", returns=R)
    assert res.value["max_consecutive_wins"] == _longest((R > 0).tolist())
    assert res.value["max_consecutive_losses"] == _longest((R < 0).tolist())
    wins, losses = R[R >= 0].sum(), abs(R[R < 0].sum())
    assert_close(res.value["profit_factor"], wins / losses)
    assert_close(res.value["gain_pain_ratio"], R.sum() / abs(R[R < 0].sum()))
    assert_close(res.value["tail_ratio"], abs(R.quantile(0.95) / R.quantile(0.05)))
    assert_close(res.value["common_sense_ratio"], lib().common_sense_ratio(R, prepare_returns=False))
    assert_close(res.value["cpc_index"], lib().cpc_index(R, prepare_returns=False))
    assert_bad_data_gives_none(lambda s: call("metrics.summary_streaks", returns=s))


def test_periods_QS_046():
    assert_registered("metrics.summary_periods", "QS-046")
    res = call("metrics.summary_periods", returns=R)
    end = R.index[-1]
    ytd = float(np.prod(1 + R[R.index.year == end.year].to_numpy()) - 1)
    mtd = float(np.prod(1 + R[(R.index.year == end.year) & (R.index.month == end.month)].to_numpy()) - 1)
    one_year = float(np.prod(1 + R[R.index >= end - pd.DateOffset(years=1)].to_numpy()) - 1)
    assert_close(res.value["ytd"], ytd)
    assert_close(res.value["mtd"], mtd)
    assert_close(res.value["1y"], one_year)
    assert res.value["5y_annualised"] is None and res.value["10y_annualised"] is None
    assert res.value["3y_annualised"] is None          # the fixture spans a little under three years
    span = (end - R.index[0]).days / 365.25
    assert_close(res.value["all_time_annualised"], float(np.prod(1 + R.to_numpy())) ** (1 / span) - 1)
    again = call("metrics.summary_periods", returns=R)
    assert again.inputs_hash == res.inputs_hash          # anchored on the last observation, not on today
    assert_bad_data_gives_none(lambda s: call("metrics.summary_periods", returns=s))


def test_quality_QS_048():
    assert_registered("metrics.summary_quality", "QS-048")
    res = call("metrics.summary_quality", returns=R)
    wealth = (1 + R).cumprod()
    dd = wealth / wealth.cummax() - 1
    assert_close(res.value["recovery_factor"], R.sum() / abs(dd.min()))
    assert_close(res.value["ulcer_index"], math.sqrt((dd ** 2).sum() / (N - 1)), tol=1e-6)
    assert_close(res.value["ulcer_index"], lib().ulcer_index(R))
    assert_close(res.value["serenity_index"], lib().serenity_index(R, 0.0))
    assert_bad_data_gives_none(lambda s: call("metrics.summary_quality", returns=s))


def test_compare_periods_QS_034():
    assert_registered("metrics.compare_periods", "QS-034")
    res = call("metrics.compare_periods", returns=R, benchmark=B, aggregate="M")
    jan = R[(R.index.year == 2021) & (R.index.month == 1)]
    bjan = B[(B.index.year == 2021) & (B.index.month == 1)]
    first = res.value["rows"][0]
    assert_close(first["returns"], 100 * (np.prod(1 + jan.to_numpy()) - 1))
    assert_close(first["benchmark"], 100 * (np.prod(1 + bjan.to_numpy()) - 1))
    assert first["won"] == ("+" if first["returns"] >= first["benchmark"] else "-")
    ref = lib().compare(R, B, "ME", prepare_returns=False)
    assert len(res.value["rows"]) == len(ref)
    assert_close([row["returns"] for row in res.value["rows"]], ref["Returns"].tolist())
    with pytest.raises(BadInput):
        call("metrics.compare_periods", returns=R, benchmark=B, aggregate="X")
    assert_bad_data_gives_none(lambda s: call("metrics.compare_periods", returns=s, benchmark=B))
    assert_bad_data_gives_none(lambda s: call("metrics.compare_periods", returns=R, benchmark=s))


def test_fund_metrics_AH_36():
    assert_registered("metrics.fund_metrics", "AH-36")
    idx = pd.date_range("2024-01-01", periods=5, freq="D")
    nav = pd.Series([1100.0, 1210.0, 1089.0, 1197.9, 1317.69], index=idx)
    bench = pd.Series([1010.0, 1020.0, 1030.0, 1040.0, 1050.0], index=idx)
    res = call("metrics.fund_metrics", nav=nav, capital=1000.0, benchmark_nav=bench, periods_per_year=252)
    steps = np.array([0.1, 0.1, -0.1, 0.1, 0.1])
    total = 1317.69 / 1000 - 1
    years = max(4 / 365.25, 0.01)
    assert_close(res.value["total_return"], total)
    assert_close(res.value["annualized_return"], (1 + total) ** (1 / years) - 1)
    assert_close(res.value["sharpe_ratio"], steps.mean() / steps.std(ddof=1) * math.sqrt(252), tol=1e-9)
    assert_close(res.value["max_drawdown"], 0.1)
    assert_close(res.value["benchmark_return"], 0.05)
    assert_close(res.value["excess_return"], total - 0.05)
    assert_close(call("metrics.fund_metrics", nav=nav, capital=1000.0).value["total_return"], total)
    assert call("metrics.fund_metrics", nav=nav, capital=-5.0).value is None
    assert call("metrics.fund_metrics", nav=pd.Series([], dtype=float), capital=1000.0).value is None
    nan_nav = pd.Series(np.nan, index=idx)
    assert call("metrics.fund_metrics", nav=nan_nav, capital=1000.0).value is None
    assert call("metrics.fund_metrics", nav=nav.where(nav < 1200, -1.0), capital=1000.0).value is None


def _prices(r: pd.Series) -> pd.Series:
    return summary.prices_from_returns(r)


def test_performance_stats_FF_001():
    assert_registered("metrics.performance_stats", "FF-001")
    res = call("metrics.performance_stats", returns=R)
    ref = _lib.price_stats().PerformanceStats(_prices(R)).stats
    assert_close(res.value["total_return"], ref["total_return"])
    assert_close(res.value["daily_sharpe"], ref["daily_sharpe"])
    assert_close(res.value["max_drawdown"], ref["max_drawdown"])
    assert_close(res.value["total_return"], float(np.prod(1 + R.to_numpy()) - 1))
    assert_close(res.value["best_day"], float(R.max()))
    assert_close(res.value["worst_day"], float(R.min()))
    assert_close(res.value["sharpe_monthly_over_daily"], ref["monthly_sharpe"] / ref["daily_sharpe"])
    assert_bad_data_gives_none(lambda s: call("metrics.performance_stats", returns=s))


def test_group_stats_FF_002():
    assert_registered("metrics.group_stats", "FF-002")
    frame = pd.DataFrame({"strategy": R, "benchmark": B})
    res = call("metrics.group_stats", returns_frame=frame)
    ref = _lib.price_stats().GroupStats(_prices(R).rename("strategy"), _prices(B).rename("benchmark")).stats
    for col in ("strategy", "benchmark"):
        assert_close(res.value["total_return"][col], ref.loc["total_return", col])
        assert_close(res.value["max_drawdown"][col], ref.loc["max_drawdown", col])
    assert_close(res.value["total_return"]["strategy"], float(np.prod(1 + R.to_numpy()) - 1))
    assert_close(res.value["total_return"]["benchmark"], float(np.prod(1 + B.to_numpy()) - 1))
    with pytest.raises(BadInput):
        call("metrics.group_stats", returns_frame=R)
    bad = pd.DataFrame({"empty": pd.Series([], dtype=float)})
    assert call("metrics.group_stats", returns_frame=bad).value is None
    assert call("metrics.group_stats", returns_frame=pd.DataFrame({"x": pd.Series(np.nan, index=R.index)})).value is None


def test_calc_stats_FF_003():
    assert_registered("metrics.calc_stats", "FF-003")
    from_returns = call("metrics.calc_stats", series=R, kind="returns")
    from_prices = call("metrics.calc_stats", series=_prices(R), kind="prices")
    ref = _lib.price_stats().calc_stats(_prices(R)).stats
    assert_close(from_returns.value["total_return"], ref["total_return"])
    assert_close(from_returns.value["total_return"], float(np.prod(1 + R.to_numpy()) - 1))
    assert_close(from_prices.value["total_return"], from_returns.value["total_return"], tol=1e-9)
    perf = call("metrics.calc_stats", series=R, kind="returns", perf_only=True)
    assert_close(perf.value["cagr"], _lib.price_stats().calc_perf_stats(_prices(R)).stats["cagr"])
    with pytest.raises(BadInput):
        call("metrics.calc_stats", series=R, kind="weights")
    assert_bad_data_gives_none(lambda s: call("metrics.calc_stats", series=s))
    assert call("metrics.calc_stats", series=pd.Series([-1.0] * 40, index=pd.bdate_range("2022-01-03", periods=40)) + np.arange(40) * 0.0, kind="prices").value is None


def test_build_summary_has_every_block():
    blocks = summary.build_summary(R, benchmark=B)
    assert set(blocks) == {"header", "returns", "ratios", "psr", "drawdown", "volatility", "operations", "expectations", "ruin", "streaks",
                           "periods", "quality", "versus_benchmark"}
    assert all(b.value is not None for b in blocks.values())
