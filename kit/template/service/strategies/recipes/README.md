# Recipes

A recipe is a short, runnable way to do one job in the strategy factory: what the job is for, the steps, one example on the real fixtures, and the checks that
catch the usual mistakes. Recipes are written in the words of Hokma and call only the modules of Hokma. Each one has a ref id (`rec-NNN`) that points to the idea it
grew from in `.notices/THIRD_PARTY.txt`.

What a recipe produces stays on the machine of the owner: no recipe sends, shares or sells a signal, and none names a channel for it.

## Format

- Front matter: `name` (the file name), `ref`, `rows` (the rows of the plan it covers), `lines` (DT, SWC, SWO, LP, RF), `summary`, `runnable` and `calls`.
- Sections: Idea, Steps, Example, Checks.
- A block marked `python runnable` is run by `backend/tests/factory/test_recipes.py` against the real fixtures: the NQ bars of `backend/tests/backtest/fixtures`
  and the daily returns of `backend/tests/quant/fixtures`. A block marked `python runnable engine` also needs the backtest engine and is skipped where the
  engine has no wheel. A block marked `python illustrative` only has to parse: it calls a module that another block of the plan builds.
- `calls` lists the modules a recipe names that may not exist yet; the test skips them until they do.
- A recipe imports only Hokma, numpy and pandas. Statistics come from `hokma.quant` through the registry, never from a library.

## Names a runnable block receives

`STATEMENTS` (the three companies of the fundamentals fixture, one frame each, with the date each period was filed), `QUARTERLY` (quarterly revenue facts of the first company), `BARS_DAILY` (70 real daily sessions of NQ), `BARS_DAY` (390 real minute bars of one regular session), `BARS_NIGHT` (10 real overnight minutes), `MULTI` (real minute bars of
ES, MES and CL), `RETURNS` and `BENCHMARK` (756 real daily returns each), `CONTRACT` (`CONT:NQ`), `source_for(bars)` (a source of bars for a run), `DB` and `CURVES` (a database and a folder for a
run), `LEDGER` (an in memory ledger of trials). The last block of a runnable recipe sets `OK = True` after its own assertions.

## Running and searching

| Recipe | Lines | Runs here | What it does |
|---|---|---|---|
| [multi-timeframe](multi-timeframe.md) | DT, SWC | yes | Read a higher timeframe from a strategy that decides on a short bar, without ever seeing a bar that is still forming. |
| [optimize-and-surface](optimize-and-surface.md) | DT, SWC, SWO, LP | yes | Search the parameters of a strategy, look at the whole surface and not only at the best point, and report the deflated Sharpe ratio with the number of trials. |
| [signal-plus-exit](signal-plus-exit.md) | DT, SWC | yes | One entry signal and three independent ways out (a hard stop, a trailing exit and a time limit), each one a rule that can be removed on its own. |
| [parity-check](parity-check.md) | DT, SWC | yes | When two ways of running the same rule disagree, compare the signals first, then the fills, then the costs, and explain the gap to the cent. |

## Cases of use

| Recipe | Lines | Runs here | What it does |
|---|---|---|---|
| [stop-exit-study](stop-exit-study.md) | DT, SWC | yes | Compare a hard stop, a target, a trailing exit, a random exit and plain holding over many entry windows before trusting any stop. |
| [pairs-spread-zscore](pairs-spread-zscore.md) | SWC, LP | yes | Trade the gap between two related instruments from the z-score of a rolling regression spread, with both legs sized by the hedge ratio. |
| [macd-parameter-cube](macd-parameter-cube.md) | DT, SWC, LP | yes | Sweep the three lengths of a MACD rule over several time ranges and keep only the settings that work in all of them. |
| [moving-average-heatmap](moving-average-heatmap.md) | SWC, LP | yes | Test a dual moving average rule against holding and against random trading with the same number of trades, on a grid of lengths, for any market including crypto. |
| [weight-search-portfolio](weight-search-portfolio.md) | LP | yes | Search portfolio weights at random, read the best one as an optimistic number, deflate it by the number of tries and compare it with the optimizers of the portfolio module. |

## Risk, statistics and context

| Recipe | Lines | Runs here | What it does |
|---|---|---|---|
| [risk-report](risk-report.md) | LP, SWC, SWO, RF | yes | A risk report of a return series with value at risk, expected shortfall, drawdown measures and volatility, and a backtest of the value at risk model itself. |
| [risk-adjusted-performance](risk-adjusted-performance.md) | LP, SWC, SWO | yes | Judge a return series against a benchmark with Sharpe, Sortino, Treynor, alpha, beta and rolling ratios, and add factor exposures through the statistics module. |
| [period-statistics](period-statistics.md) | LP, SWC, SWO, RF, DT | yes | Turn one daily return series into period views (week, month, quarter), growth against a lag, trailing and rolling figures and a standardized score. |
| [regime-filter](regime-filter.md) | DT, SWC, SWO, LP | yes | Split history into a calm and a stormy regime with only the information known at the time, and measure what each regime pays. |
| [statistical-tests](statistical-tests.md) | SWC, LP, SWO | yes | Test a relationship before trading it with regression, residual checks and a multiple testing correction, and leave unit roots, causality and cointegration to the statistics module. |
| [macro-context](macro-context.md) | LP, SWC, RF, DT | no (needs H3) | Use macro series (growth, inflation, unemployment, rates, confidence) as slow context for a decision, with the date each figure was known and a smoothing window. |
| [bond-statistics](bond-statistics.md) | RF | no (needs H3) | Describe a bond from its price, coupon and maturity with present value, yield, duration and convexity, and read the curve, spreads and central bank rates around it. |

## Data, universe and books

| Recipe | Lines | Runs here | What it does |
|---|---|---|---|
| [universe-screen](universe-screen.md) | SWO, LP, SWC | yes | Find candidates from a universe with filters and a movers table (gainers, losers, most active) before any strategy is written. |
| [own-data-adapter](own-data-adapter.md) | DT, SWC, SWO, LP, RF | yes | Feed a backtest from your own bars the safe way, with the oldest bar first, one row per bar, the same hash on any machine and a loud failure on bad rows. |
| [ledger-pnl](ledger-pnl.md) | LP, SWC, RF, DT | yes | Turn a list of fills into positions and profit and loss with average cost and with first in first out, and see why the two disagree. |

## Company and option models

| Recipe | Lines | Runs here | What it does |
|---|---|---|---|
| [fundamental-ratios](fundamental-ratios.md) | SWO, LP | yes | Compute the ratio families of a company from its statements through the fundamentals module, with growth, trailing and the written convention of each formula. |
| [valuation-scores](valuation-scores.md) | SWO, LP | yes | Read a company with the classic scores and models, the DuPont split of return on equity, bankruptcy and quality scores, enterprise value and a discounted cash flow with its checks. |
| [option-greeks](option-greeks.md) | SWO, LP | yes | Price an option and read its greeks across strikes and expirations through the options module, check put call parity, and recover the implied volatility from a market price. |
| [indicator-panel](indicator-panel.md) | DT, SWC, SWO, LP | yes | Build a panel of technical indicators from bars, such as bands, relative strength, trend strength, channels, volume weighted price and volatility cones, through the indicator catalog. |

## Learned signals and forecasts

| Recipe | Lines | Runs here | What it does |
|---|---|---|---|
| [ml-signal](ml-signal.md) | DT, SWC, SWO, LP | yes | A learned signal done safely, with labels that cannot leak, features that look only backward and a model that is fit again on every window and scored only on the window after it. |
| [time-series-forecast](time-series-forecast.md) | SWC, SWO, LP | yes | Evaluate any forecast of the next return against simple baselines in a walk forward, with a test of equal accuracy, before it is allowed near a strategy. |
