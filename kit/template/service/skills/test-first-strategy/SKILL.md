---
name: test-first-strategy
version: 1.0.0
description: >
  Use when a strategy spec is being written or changed, before any backtest. Trigger phrases: "write the spec", "new strategy", "draft the strategy", "change the entry rule", "size the position", "add a signal".
identity: "A strategy developer who writes the tests of the signal and of the position size first, watches them fail, and only then writes the strategy."
patterns:
  - "Write the signal test first: facts in, the view that must come out (long, short, flat or abstain), with the numbers worked out by hand."
  - "Write the position size test next: a size rule, a price, the equity and the contract, and the quantity that must come out, worked out by hand."
  - "Run the tests with the strategy not written yet and see them fail for the right reason."
  - "Write the smallest alpha and parameters that make them pass, then run them again."
  - "Add one test for each rule you add, and one for each bug you fix, before the change."
  - "Keep the tests in the spec itself: a spec with no tests cannot go to a backtest."
avoid:
  - "Writing the strategy first and the tests afterwards to match what it does."
  - "Expected values copied from the output of the code: work them out by hand."
  - "A signal test with no case for the flat or the abstain outcome."
  - "A size test with a round number that hides the rounding: include a case that rounds down."
  - "Running a backtest to find out whether the signal is right."
---
## Why the tests come first

A backtest tells you whether an idea made money. It does not tell you whether the code does what you meant. A test written after the code agrees with the code, right or wrong. A test written before it can fail, and a test that never failed has not shown that it can.

## The shape of a spec

A spec is a short YAML text. The tests are data, not code: the factory runs them with its own functions.

```yaml
strategy_id: ema_cross_demo
line: DT
alpha: ema_cross
params: {fast: 3, slow: 5}
hypothesis: A fast average above a slow one means the move continues.
tests:
  signal:
    - name: fast_above_slow_is_long
      facts: {ema_fast: [101, 102, 103], ema_slow: [100, 100, 100]}
      expect: long
    - name: fast_below_slow_is_short
      facts: {ema_fast: [99, 98, 97], ema_slow: [100, 100, 100]}
      expect: short
    - name: equal_averages_are_flat
      facts: {ema_fast: [100, 100, 100], ema_slow: [100, 100, 100]}
      expect: flat
    - name: a_missing_average_abstains
      facts: {ema_fast: [101, 102, 103]}
      expect: abstain
  position_size:
    - name: risk_500_on_a_20_point_stop
      contract_id: FUT:NQ:2025-03
      asset_type: future
      multiplier: 20
      kind: risk_usd
      value: 500
      price: 20000
      equity: 100000
      stop_distance: 5
      expect_qty: 5
```

Worked by hand: with the fast average 3 above a slow one at 100 the view is (103 - 100) / 100 / 0.01 = 3, clipped to 1, so long. A risk of 500 dollars with a stop 5 points away on a contract worth 20 dollars a point loses 100 dollars per contract, so 500 / 100 is 5 contracts.

## Steps

1. Write the hypothesis in one sentence.
2. Write the signal tests: at least one that must go long or short, and one that must stay flat or abstain.
3. Write the size tests: at least one exact case and one where the quantity is rounded down to whole contracts.
4. Run them through `factory_draft`. With no strategy that passes, they fail: that is the red step.
5. Choose the alpha and the parameters from `factory_list`, run again and read each failure.
6. When all pass, the spec can go to `backtest_run`. A failing test stops the backtest and tells you which case failed.

## Errors you will see

| Code | Meaning | What to do |
|---|---|---|
| missing_tests | the spec has no signal test or no size test | write them, then try again |
| tests_failed | a case gave another answer than expected | read the case: either the code or the expectation is wrong, find out which by hand before changing anything |

## Limits

This skill writes and runs tests. It does not judge whether the strategy is profitable; that is the work of the backtest and the audit.
