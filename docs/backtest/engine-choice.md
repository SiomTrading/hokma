# Choosing the backtest engine

Status: decided in SPEC-020 (H4). This page records the criteria, the measurements and the reason why the backtest service runs on a single engine that only one file of Hokma imports (backend/hokma/backtest/_lib.py). The names of the engines that were compared, their authors and their repositories are listed only in .notices/THIRD_PARTY.txt. Here the chosen one is called "the engine" and the others "candidate A" to "candidate E".

## Decision

- The service uses one engine, installed as a separate package and never modified.
- Only backend/hokma/backtest/_lib.py imports it. A scan test fails if another file does. Every agent, tool and module reaches the engine through the service, never directly.
- Everything that changes how a run behaves is Hokma code: the fill rules (backend/hokma/backtest/fills.py), the order of the ticks inside a bar (backend/hokma/backtest/ticks.py), the costs (backend/hokma/backtest/costs.py) and the switch off in memory of the engine portfolio statistics. The statistics a student reads come from hokma.quant.metrics and hokma.quant.validation, never from the engine.

## Criteria and how each one is measured

Every measurement uses real one minute bars of the NQ future from the Trading Core store: the contract of one November week for the fill scenarios, and the whole year 2025 (332,721 bars) for speed. The expected values of the fill scenarios are computed by hand before any run, with 2.25 USD commission per side per contract, one tick of slippage per side and a multiplier of 20.

| Criterion | How it is measured | Pass condition |
|---|---|---|
| Fill rules T1 to T4 | Four fixed scenarios with hand computed results (below) | Each result matches to the cent |
| Licence | Licence text of the package and what it requires when Hokma ships a kit or hosts a service | Usable as a library from closed code, in the kit and in a hosted service, without publishing Hokma code |
| Speed | CPU seconds for one year of one minute bars: empty strategy, a 20/100 moving average cross, a 200 trial search | A student run of one year in under one minute; 200 trials in under ten minutes on five cores |
| Same code simulated and live | Does the same strategy class run on simulated fills and on a live node, only changing the adapter | Yes, with no change to the strategy |
| Instruments | Contract multiplier, per contract fees, one tick slippage, expiring contracts, options, several assets in one run | Native, not patched |

## Fill rules T1 to T4

| Scenario | Hand computed expectation | Engine with its default behaviour | Engine behind Hokma rules |
|---|---|---|---|
| T1 fixed round trip | 100.50 USD (a 5.75 point move, minus 0.5 point of slippage, times 20, minus 4.50 fees) | 75.50 USD: it fills at the close of the previous bars instead of the opens | 100.50 USD exact |
| T2 stop and target touched in the same bar, long | the stop fills: exit 25466.50, -114.50 USD | the target fills first: +45.50 USD, optimistic | exit 25466.50, -114.50 USD |
| T2 mirror, short | exit 25477.00, -114.50 USD | same optimistic fill | exit 25477.00, -114.50 USD |
| T3 gap through a stop (Sunday open, 94 points) | fills at the open plus one tick: 25321.00, -2059.50 USD | fills at the open | 25321.00, -2059.50 USD exact |
| T4 signal at the close of bar N | entry 25552.00 (next open 25551.75 plus one tick), exit 25549.75, -49.50 USD | fills at the close of the signal bar | entry 25552.00, exit 25549.75, -49.50 USD exact |

How the gap is closed without touching the library: each bar enters as synthetic ticks (open, extremes in the pessimistic order for the side of the position, an exact tick at the stop and target price when the range crosses it, close), and the decision of bar N is sent at the first tick of bar N+1. The order of the extremes inside a one minute bar is an assumption because the data does not say which came first; Hokma always takes the pessimistic one and reports every bar where stop and target were both touched. A control test runs T2 with the default order of the engine and checks that it fills the target: if a future version changes that behaviour, the test says so.

The fixtures of these scenarios live in backend/tests/backtest/fixtures/nq_scenarios.json and the tests in backend/tests/backtest/test_fills.py.

## Licence

| Candidate | Licence class | Effect on a paid kit | Effect on a hosted service |
|---|---|---|---|
| A (bar based, one instrument) | Copyleft with a network clause | The combined kit must be published | The full source of the service must be offered to its users: not acceptable |
| B (vectorised) | Permissive plus a clause against selling | A paid kit built on it falls in the forbidden "sell" | Hosting it for a fee is forbidden: blocking |
| The engine | Weak copyleft (LGPL) | Allowed as a separate, unmodified, replaceable package | No network clause: allowed |
| D (event driven, .NET) | Permissive | Allowed | Allowed, but needs a second runtime |
| E (daily futures) | Strong copyleft (GPL) | A kit that includes it must be published | No network clause |

The engine is therefore the only candidate that is both usable in the kit and in the service. How Hokma complies is in .notices/THIRD_PARTY.txt: the package is installed with pip, no file of it is edited, copied or redistributed inside Hokma, and the student can replace it with another version.

## Speed

| Measurement (CPU seconds, one year of one minute NQ, 332,721 bars) | Result |
|---|---|
| Core with an empty strategy | 2.4 s (about 139,000 bars per second) |
| 20/100 moving average cross, 4,568 trades, default statistics | 40 to 43 s |
| Same, with the portfolio statistics switched off in memory by Hokma | 10.6 s, same result to the cent |
| Cross 5/50, 12,219 trades, default statistics | 502 s |
| Same, statistics switched off in memory | 21 s, same result |
| Search of 200 trials on 5 processes | 433 s wall, about 1,660 trials per hour, 91 percent parallel efficiency |
| Candidate A and candidate B on the 20/100 cross | 1.1 s and 2.4 s |

The default statistics of the engine rebuild the whole history after each closed position, a cost that grows with the square of the number of trades. Switching that accounting off, in memory and without editing a file, is what makes searches practical. Hokma computes its own statistics from the trade list, so nothing a student reads depends on that accounting.

The engine is four to ten times slower than the two fastest candidates on this test and meets the pass conditions above only with the switch off. That cost is accepted for the next four properties.

## Simulated and live with the same code

| Candidate | Same strategy in a simulated node and in a live node |
|---|---|
| A | No |
| B | No |
| The engine | Yes: a sandbox execution client gives real market data with simulated orders, then the adapter is swapped for a real broker |
| D | Yes by code, not run here (no runtime available on the test machine) |
| E | Only one broker, with separate production code |

The backtest service itself never has a path to the order submission, the approval or any broker connector of Hokma (SPEC-020 section 4.7). The live path of a desk goes through Hokma's own execution layer, not through the engine adapters; the engine property matters because it keeps the strategy class identical between research and operation.

## Instruments

| Property | Engine | Candidate A | Candidate B |
|---|---|---|---|
| Contract multiplier | Native | No (scale the prices) | No in the open version |
| Per contract commission | Native | Own function | Per order only |
| One tick of slippage per side | Native | Relative spread, entry side only | Possible with a per bar array |
| Expiring contracts | Native expiry handling | No | No |
| Options | Yes (not measured here) | No | No |
| Several assets in one run | Yes | No | Yes |

## Why one engine, and why only behind _lib.py

1. One place to pin the version (pyproject.toml and uv.lock) and one place to change when the library changes. Hokma was measured on one version, and every claim on this page holds for that version only.
2. One licence boundary. The rule "unmodified and replaceable" is easy to audit when a single file imports the package.
3. One definition of a fill. Two engines would give two answers to the same scenario, as the table above shows for the default behaviour of every candidate. A calculation exists once.
4. One place where a missing wheel is handled. On a machine without a wheel for the engine (macOS on Intel) the service answers engine_unavailable and the tests that need it skip with that reason.
5. No agent or module can call a third party library on its own: a statistic or a fill that does not come from Hokma code is rejected by the scan test.

## What it costs and what is not covered

- The default fill behaviour of the engine is optimistic on T1, T2 and T4. The Hokma layer corrects it and the control test watches it.
- No adapter for the broker whose data feeds Hokma exists in the engine: a live account needs Hokma's own adapter.
- Speed is lower than the vectorised candidates; the switch off in memory is part of the decision and must be kept in the service.
- Not measured: options, continuous futures and the step from sandbox to a real broker. Candidate D was evaluated by reading its code only, because its runtime is not installed on the test machine.
- Six more engines appear in the wider index of the factory (see the notices file). They were not run, their commit dates were not verified, and they are idea references only. High frequency engines for tick level market making are out of scope: the DT line of Hokma works on bars of one minute to one hour. Engines for daily bars are used only as ideas for sizing by volatility and roll calendars on the SWC, LP and RF lines.

## How to repeat the measurements

The scenarios are built from the fixture, the expectations are computed by hand in the tests, and the speed figures come from one year of one minute NQ bars exported from the Trading Core store with read only access. A result that differs from the tables is a finding to report, not a number to adjust.
