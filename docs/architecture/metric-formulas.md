# Metric formulas

Formulas of the metrics in the part of the metrics module that covers the summary table, trade statistics, tail view, Monte Carlo,
attribution, benchmark metrics and reports. Each entry gives the formula the code evaluates, the units, and the paper that defines
the measure. Code lives in `backend/hokma/quant/metrics/`; every function is registered as `metrics.<name>` and returns a
`QuantResult`.

Notation: `r_t` is the return of period `t`, `n` the number of periods, `m` the periods per year (252 for daily data),
`rf` the annual risk-free rate, `b_t` the benchmark return. All figures are computed from period returns, not from discrete
trades. A series with fewer than two observations, a constant series or a series with no finite values gives `value = None` and
the reason in `warnings`. Non-finite values inside a series are dropped and reported in `warnings`.

## Summary blocks (`summary.py`)

| Function | Figure | Formula |
|---|---|---|
| `metrics.summary_header` | time in market | `ceil(100 * count(r != 0) / n) / 100` |
| `metrics.summary_returns` | cumulative return | `prod(1 + r_t) - 1` |
| | total return | `sum(r_t)` |
| | CAGR | `(1 + cumulative)^(m / n) - 1` |
| `metrics.summary_ratios` | Sharpe | `mean(r - rf_d) / std(r, ddof=1) * sqrt(m)`, with `rf_d = (1 + rf)^(1/m) - 1` (Sharpe, 1966) |
| | Sortino | `mean(r - rf_d) / sqrt(sum(min(r, 0)^2) / n) * sqrt(m)` (Sortino and Price, 1994) |
| | Sortino over root 2 | Sortino divided by `sqrt(2)` |
| | smart Sharpe, smart Sortino | the same ratios with the standard deviation scaled by the autocorrelation penalty of the series |
| | Omega | `sum(max(r - h, 0)) / sum(max(h - r, 0))` with threshold `h = 0` (Keating and Shadwick, 2002) |
| `metrics.summary_psr_row` | probabilistic Sharpe | the value returned by `validation.psr`; this module does not compute it |
| `metrics.summary_drawdown` | drawdown | `W_t / max(W_s, s <= t) - 1` with `W_t = prod(1 + r_s, s <= t)`; an episode runs from the first underwater day to the last underwater day before the peak is regained; days are counted as calendar days, both ends included |
| `metrics.summary_volatility` | volatility | `std(r, ddof=1) * sqrt(m)` |
| | skew, kurtosis | sample skewness and excess kurtosis |
| | Calmar | `CAGR / abs(max drawdown)` |
| | risk-return ratio | `mean(r) / std(r, ddof=1)` |
| | R squared, information ratio | against the benchmark, only when one is given |
| `metrics.summary_operations` | average win, average loss | mean of positive returns, mean of negative returns |
| | win/loss ratio | `mean(wins) / abs(mean(losses))` |
| | profit ratio | `(mean(wins) / count(wins)) / abs(mean(losses) / count(losses))` over returns `>= 0` and `< 0` |
| `metrics.summary_expectations` | expected return | `prod(1 + x)^(1/k) - 1` over the `k` periods of the chosen frequency (daily, month end, year end) |
| | daily VaR, expected shortfall | parametric, see the tail view |
| `metrics.summary_streaks` | longest streaks | longest run of `r > 0`, longest run of `r < 0` |
| | gain/pain | `sum(r) / abs(sum(r where r < 0))`, also on monthly sums |
| | profit factor | `sum(r where r >= 0) / abs(sum(r where r < 0))` |
| | tail ratio | `abs(q95 / q05)` |
| | common sense ratio | profit factor times tail ratio |
| | CPC index | profit factor times win rate times win/loss ratio |
| `metrics.summary_periods` | MTD, 3M, 6M, YTD, 1Y | compounded return since an anchor date counted back from the last observation, never from today's date |
| | 3Y, 5Y, 10Y, all-time | annualised: `total^(1 / years) - 1`, with years from calendar days divided by 365.25; undefined when the series is shorter than the window |
| `metrics.summary_quality` | recovery factor | `sum(r) / abs(max drawdown)` |
| | ulcer index | `sqrt(mean(D_t^2))` where `D_t` is the drawdown |
| | serenity index | `(sum(r) - rf) / (ulcer index * pitfall)`, pitfall `= -CVaR(D) / std(r)` |
| `metrics.compare_periods` | period table | compounded return of strategy and benchmark per period, multiplier `R / B`, winner marker |
| `metrics.fund_metrics` | fund figures | returns of a value curve including the starting capital; total `V_n / V_0 - 1`; annualised `(1 + total)^(1 / years) - 1` with `years = max(days / 365.25, 0.01)`; Sharpe `mean / std(ddof=1) * sqrt(m)`; maximum drawdown `max((peak - V) / peak)`; excess over the benchmark curve |
| `metrics.performance_stats`, `metrics.group_stats`, `metrics.calc_stats` | statistics sheets | computed on a price curve that starts at 100 on the business day before the first return; Sharpe is reported at daily, monthly and yearly frequency; the ratio of monthly to daily Sharpe is added, because a large gap between them reveals autocorrelation |

## Trade statistics (`trades.py`)

| Function | Figure | Formula |
|---|---|---|
| `metrics.consecutive_streaks` | streaks | longest run of wins, longest run of losses |
| `metrics.exposure` | exposure | share of non-zero periods, rounded up to 1% |
| `metrics.win_rate_stats` | win rate | `count(r > 0) / count(r != 0)` |
| `metrics.payoff_ratios` | payoff ratio | `mean(wins) / abs(mean(losses))` |
| `metrics.outlier_ratios` | outlier win, outlier loss | `q99 / mean(r where r >= 0)`, `q01 / mean(r where r < 0)` |
| `metrics.kelly_cap` | Kelly fraction | `(P * p - q) / P` with payoff `P`, win rate `p`, loss rate `q = 1 - p` (Kelly, 1956); the ceiling is `max(0, K) * fraction` and is a limit on size, never a size |
| `metrics.risk_of_ruin`, `metrics.risk_of_ruin_params` | risk of ruin | see below |

### Risk of ruin

Wealth is multiplicative, so the walk is on log wealth. A win adds `a = ln(1 + W)` with probability `p`. A loss subtracts
`c = -ln(1 - L)` with probability `q = 1 - p`. `W` and `L` are the average win and the average loss as fractions of capital.
Ruin means losing the fraction `u` of capital (default 0.5), a barrier at `B = -ln(1 - u)` in log wealth.

- Drift `p * a - q * c`. With no positive drift the probability is 1.
- The adjustment coefficient `theta > 0` is the root of `p * exp(-theta * a) + q * exp(theta * c) = 1`, found by bisection.
- Probability of ever reaching the barrier: `exp(-theta * B)`.
- Special cases: no losses gives 0; no wins gives 1; an average loss of 100% of capital or more gives 1.

When wins and losses have the same size in log wealth, the root is `exp(theta * a) = p / q` and the probability is
`(q / p)^(B / a)`. The tests use this closed form. The sample-size formula `((1 - p) / (1 + p))^n` ignores the size of wins and losses and
tends to zero as `n` grows, which hides real risk; it is not used.

## Tail view (`tail_view.py`)

Level `c` (default 0.95), `alpha = 1 - c`, `mu` and `sigma` the sample mean and standard deviation (ddof 1).

| Method | VaR | CVaR and expected shortfall |
|---|---|---|
| parametric | `mu + sigma * z_alpha`, `z_alpha` the normal quantile | `mu - sigma * phi(z_alpha) / alpha` |
| historical | the empirical quantile `q_alpha` | mean of the returns at or below `q_alpha` |

Expected shortfall equals CVaR. Figures are returns: a loss is negative.

## Monte Carlo (`montecarlo.py`)

- **Permutation** (`metrics.montecarlo_drawdown`): the returns are shuffled, the first path keeping the original order. For each
  path the maximum drawdown is `min(W_t / max(W_s, s <= t) - 1)`. By default the first peak is the first path value; with
  `include_start_capital` the starting capital 1.0 is the first peak. Output: minimum, maximum, mean, median, standard deviation,
  percentiles 5, 50 and 95, and the bust probability `count(maxdd <= bust) / runs`. Shuffling cannot change the mean, the deviation or
  the compounded product, so this function reports drawdown only and refuses any other statistic.
- **Stationary bootstrap** (Politis and Romano, 1994): the path takes a random start; at every step it continues with the next
  observation with probability `1 - 1/k` and jumps to a new random start with probability `1/k`, `k` the mean block length
  (default 10). Sharpe per path is `mean / std(ddof=1) * sqrt(m)`; CAGR per path is `prod(1 + r)^(m / n) - 1`; the goal
  probability is the share of paths with `prod(1 + r) - 1 >= goal`.
- Every result carries the seed and the number of runs. The same seed gives the same numbers.

## Attribution (`attribution.py`)

Factor model `R = B f + e` with asset-by-factor loadings `B`, factor covariance `F` and idiosyncratic covariance `D`. Portfolio
weights `w`, factor exposure `x = B' w`.

- Predicted variance: `sigma_P^2 = x' F x + w' D w`.
- Volatility contribution of factor `k`: `x_k (F x)_k / sigma_P`; the contributions of all factors sum to the systematic part
  `x' F x / sigma_P`, and systematic plus idiosyncratic sum to `sigma_P`.
- Expected return contribution of factor `k`: `x_k * mu_k`.
- Realized attribution: the contribution of factor `k` to variance is `Cov(x_k f_k, R_P) / sigma_P`; the mean contribution is the
  annualised mean of `x_k f_k`. Systematic, idiosyncratic and unattributed parts sum to the total.
- Views by asset, by factor and by family add the same contributions over the corresponding groups. Rolling attribution repeats the
  realized calculation over windows that advance by a fixed step.

## Benchmark metrics (`benchmark.py`)

- Beta: `Cov(r, b) / Var(b)`. Alpha: `(mean(r) - beta * mean(b)) * m`. Correlation: Pearson (Sharpe, 1964 for the model; Jensen, 1968 for alpha).
- Treynor ratio: `(cumulative return - rf) / beta` (Treynor, 1965).
- CAPM expected return: `rf + beta (R_m - rf)` on annualised returns. Jensen's alpha: `R - [rf + beta (R_m - rf)]`.
  Residuals: `(r - rf_d) - beta (b - rf_d)`, annualised by `sqrt(m)`. Appraisal ratio: Jensen's alpha divided by the residual
  deviation (Treynor and Black, 1973).
- Factor regressions: excess return on `Mkt-RF, SMB, HML` (three factors) or on those plus `Mom` (four factors), least squares with an
  intercept; the factor dataset is supplied by the caller in decimals.
- Capture: `(prod(1 + r)^(1/k) - 1) / (prod(1 + b)^(1/k) - 1)` over the `k` periods where the benchmark rose (upside) or fell (downside).
- Fama decomposition (Fama, 1972): net selectivity `(R - rf) - (sigma / sigma_m)(R_m - rf)`; diversification
  `(sigma / sigma_m - beta)(R_m - rf)`; their sum is Jensen's alpha.
- Market timing: Treynor-Mazuy (1966) `r - rf = alpha + beta (b - rf) + gamma (b - rf)^2`; Henriksson-Merton (1981)
  `r - rf = alpha + beta (b - rf) + beta_up max(b - rf, 0)`. A positive `gamma` or `beta_up` indicates timing skill.
- Rolling alpha and beta: over a window, `beta = corr * sd / sd_b` and `alpha = mean - beta * mean_b`.

## Reports (`report.py`)

One HTML file with inline styles and inline SVG charts, a template of 23 markers, and no external address of any kind. Every marker is
filled with content or with `n/a`. Output is deterministic: it depends only on the inputs. Charts that need a benchmark show `n/a`
without one. The metrics table is the summary blocks laid out one row per metric; the probabilistic Sharpe row comes from
`validation.psr`.

## Function index

Every registered function of this part, by file.

- `summary.py`: `metrics.summary_header`, `metrics.summary_returns`, `metrics.summary_ratios`, `metrics.summary_psr_row`, `metrics.summary_drawdown`, `metrics.summary_volatility`, `metrics.summary_operations`, `metrics.summary_expectations`, `metrics.summary_ruin_row`, `metrics.summary_streaks`, `metrics.summary_periods`, `metrics.summary_quality`, `metrics.compare_periods`, `metrics.fund_metrics`, `metrics.performance_stats`, `metrics.group_stats`, `metrics.calc_stats`
- `trades.py`: `metrics.consecutive_streaks`, `metrics.exposure`, `metrics.win_rate_stats`, `metrics.risk_of_ruin`, `metrics.risk_of_ruin_params`, `metrics.payoff_ratios`, `metrics.outlier_ratios`, `metrics.kelly_cap`
- `tail_view.py`: `metrics.tail_view`
- `montecarlo.py`: `metrics.montecarlo_drawdown`, `metrics.montecarlo_sharpe`, `metrics.montecarlo_cagr`, `metrics.montecarlo_goal_probability`, `metrics.montecarlo_summary`
- `attribution.py`: `metrics.attribution_predicted`, `metrics.attribution_breakdown`, `metrics.attribution_components`, `metrics.attribution_realized`, `metrics.attribution_rolling`
- `benchmark.py`: `metrics.alpha_beta`, `metrics.alpha_beta_rolling`, `metrics.versus_benchmark`, `metrics.capm`, `metrics.factor_model`, `metrics.capture_ratios`, `metrics.fama_decomposition`, `metrics.market_timing`
- `report.py`: `metrics.report_html`, `metrics.report_example`, `metrics.report_metrics`, `metrics.report_full`, `metrics.report_basic`, `metrics.report_plots`, `metrics.report_heatmap`
