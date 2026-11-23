# Metrics upgrades

What the metrics module does today where it differs from a plain use of the metric libraries it wraps. Each entry names the
function, the behaviour and the reason. The library is reached only through `backend/hokma/quant/metrics/_lib.py`.

## Sources read for this document

The two project notes of the metrics library (the project overview and the modernization notes) were read in full before writing
this file. They state three facts the module relies on and documents:

1. The trade-style statistics (win rate, payoff ratio, streaks) are computed from return periods, not from discrete trades. The
   module keeps that meaning and says so in the docstring of `trades.py` and in every result that uses it. Trade-level statistics
   come from the trade journal, not from this module.
2. Monte Carlo by shuffling preserves the return distribution and varies the path. The module uses that only for what a path can
   change (drawdown, bust probability) and takes the other statistics from a bootstrap.
3. Frequency aliases and version differences of pandas and numpy are handled in one place. In this module that place is `_lib.py`;
   no other file contains a version check.

The modernization notes list packaging, typing and issue work for the library itself. None of it changes a number the module
returns, so nothing from that list is copied.

## Behaviour of the module

| Area | Function | Behaviour today | Reason |
|---|---|---|---|
| Probabilistic Sharpe | `metrics.summary_psr_row`, `metrics.report_metrics` | The row comes from `validation.psr`. The library formula is blocked in the module. | The library formula subtracts the kurtosis twice and multiplies the probability by the square root of the periods. |
| Risk of ruin | `metrics.risk_of_ruin`, `metrics.risk_of_ruin_params` | Uses the real win rate, average win and average loss and a ruin level, through the adjustment coefficient. | The library formula depends only on the win rate and the count of periods and ignores the size of wins and losses. |
| Monte Carlo | `metrics.montecarlo_drawdown` | Reports drawdown and bust probability only. Asking for another statistic raises `BadInput` that names the right function. | A shuffle leaves mean, deviation and compounded product unchanged, so Sharpe and CAGR come out identical in every path. |
| Monte Carlo | `metrics.montecarlo_sharpe`, `metrics.montecarlo_cagr`, `metrics.montecarlo_goal_probability` | Stationary bootstrap with percentiles 5, 50 and 95, the seed and the number of runs. | Resampling with replacement changes the totals, so the distribution is informative. |
| Monte Carlo | `metrics.montecarlo_drawdown` | Option `include_start_capital` makes the starting capital the first peak. | By default the first peak is the first path value, so a loss on the first day is not counted as drawdown. |
| Report | `metrics.report_html` | One file, inline charts, no external address, no remote icon, deterministic output. | A report must open offline and must not contact a third party; a test fails on any `http` or `https` text. |
| Report | `metrics.summary_periods` | Period returns count back from the last observation. | Counting from today's date makes a stored report change when it is opened again. |
| Report | `metrics.report_html` | An unusable benchmark gives a report without a benchmark and a warning. | The strategy figures remain valid. |
| Kelly | `metrics.kelly_cap` | Reported as a ceiling for position size, floored at 0, scaled by a fraction. | Kelly is an upper bound on size, and a negative value means no edge. |
| Missing data | every function | Empty, constant and all-NaN input gives `value = None` with warnings. Isolated NaN values are dropped and reported. | Agents must receive an explanation, not an exception. |
| Benchmark | `benchmark.py` | Series are aligned on shared dates; fewer than three shared dates, or a constant benchmark, gives `value = None`. | A beta from two points is not a measurement. |
| Factor data | `metrics.factor_model` | The factor dataset is an argument. The module downloads nothing. | Data access belongs to the data module and tests run without network. |
| Tail view | `metrics.tail_view` | Parametric and historical estimators, both returned as returns (negative for losses). | The two estimators disagree on fat-tailed series and the caller needs to see both. |
| Drawdown | `metrics.summary_drawdown` | Episodes run from the first underwater day to the last underwater day; days include both ends. | One definition across the summary, the drawdown table and the charts. |
| Warnings | every function | Library warnings are silenced inside the call and the problem is reported in `warnings`. | One channel for problems. |
