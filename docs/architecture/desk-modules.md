# Desk modules

The modules that every desk of Hokma stands on. A desk agent never calls a numerical or data library: it asks a module, and the module answers with a number, its method, its parameters and its source.

## Layers

| Package | What it does |
|---|---|
| `hokma.quant` | Registry, result type, limits and the worker process that run every quant function |
| `hokma.quant.metrics` | Return, ratio, volatility, drawdown, distribution, benchmark, attribution, trade statistics, Monte Carlo, summary table and HTML report |
| `hokma.quant.risk` | Value at risk, expected shortfall, drawdown risk, VaR backtests, stress scenarios, copulas, liquidity and Kelly sizing |
| `hokma.quant.validation` | Probabilistic and deflated Sharpe, probability of backtest overfitting, purged cross validation, bootstrap and the evidence bundle a judge receives |
| `hokma.data` | Point in time bars, snapshots, fundamentals, macro series, universe, news, social, COT and SEC filings |
| `hokma.desks.core` | Signal contract, graph, state, personas, prompts, skills, tools, safety, sealed credentials and the content hash |
| `hokma.llm` | One client over the engines Hokma already uses, and extraction of structured answers |
| `hokma.lessons` | Lessons of a desk, saved only from decisions whose order executed |
| `hokma.kit.quant_tools`, `data_tools` | The MCP tools through which agents reach the modules |

## One way in

An agent lists functions with `quant_list`, reads one with `quant_describe` and runs it with `quant_call`. Data comes from the `data_*` tools. Every call goes through `hokma.quant.registry.call`, which

1. checks the arguments against the limits in `hokma.quant.limits` (sizes, combinations, total values);
2. runs the function in a worker process that is killed if it exceeds the time limit;
3. returns a `QuantResult`: value, unit, method, parameters, input hash, cut-off, warnings and the matrix ids behind the function.

A function that cannot compute a value returns `None` with a warning. It never fills a gap.

## Libraries stay behind the module

Each module has one `_lib.py` that imports the libraries. A test fails if any other file imports them. When two libraries compute the same quantity, the module keeps the correct one and blocks the other: the probabilistic Sharpe ratio of the metrics library is blocked and `validation.psr` is the only one.

## Signal contract

`hokma.desks.core.signal.Signal` carries a conviction from -1 to +1, a confidence from 0 to 100, the criteria with the numbers that were computed by a module, the data that was missing and a short reason. A signal abstains when it has no value, and an abstention is never read as neutral. The contract has no field for size, stop, target or entry time.
