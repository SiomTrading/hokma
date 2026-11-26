---
name: backtest-debugging
version: 1.0.0
description: >
  Use when a backtest fails, stops early, returns no trades or gives a result that cannot be true. Trigger phrases: "the run failed", "no trades", "the Sharpe is huge", "the result makes no sense", "account blown", "engine error".
identity: "A methodical investigator who finds the root cause before changing anything and stops after three failed attempts."
patterns:
  - "Read the whole failure first: the status, the error text, the failed stage and the warnings of the run."
  - "Reproduce it with the same spec and the same seed, and write down the exact steps that trigger it."
  - "Find where the bad value starts by checking each stage in order: data, spec, strategy, fills, result."
  - "Form one hypothesis, change one thing to test it, and read the result before the next change."
  - "Treat a result that is too good as a failure to explain: look for look-ahead, a fill at a price that could not be had, or costs left out."
  - "After three failed attempts on the same spec, stop and write the root cause in the report."
avoid:
  - "Changing several things at once, so that nobody knows which one mattered."
  - "Trying a quick fix before the cause is known."
  - "Another attempt after the third one: the fourth is refused, and the answer is a written root cause."
  - "Loosening a limit, a fill rule or a check to make the run pass."
  - "Calling an absurd result luck."
---
## The rule

Find the root cause before you change anything. A change made without knowing the cause is a guess, and each guess that fails counts as an attempt.

## The stages of a run

| Stage | Typical failure | Where to look |
|---|---|---|
| data | no bars for the range, a gap, a contract without a sheet | `data_bars` for the same symbol and dates |
| spec | a bar size not allowed for the line, dates out of order | the message of the spec error |
| strategy | the strategy raised, emitted something other than an order intent, read a bar that is not closed yet | the error text of the run |
| fills | an order rejected by margin or by the guards of the line | the `rejected` counts of the result |
| result | statistics that cannot be computed on zero trades | the warnings of the result |

## Steps

1. Read the error and the warnings in full. Note the failed stage.
2. Reproduce: the same spec and the same seed must fail the same way. If it does not, the cause is not the strategy.
3. Trace backward from the bad value to where it first appears.
4. Write one hypothesis: "the cause is X because Y".
5. Test it with the smallest change. One change, then read the run.
6. If it holds, fix the cause once and add a test that would have caught it. If not, write what you learned and form a new hypothesis. That is one failed attempt.

## Three attempts

The service counts the failed runs of a spec. After three, the fourth attempt is refused with `attempt_budget_spent`. Your next output is then a report with status blocked and the root cause written in full: what fails, where it starts, what you tested and what it ruled out, and what you would try next. Do not try to get around the count by saving the same spec again: the same text is the same spec.

## A result that is too good

An annual Sharpe above about 3 on a simple rule, no losing month, or a curve with no drawdown is a bug until shown otherwise. Check, in this order: the signal uses only closed bars, the fill is at the next open one tick against, costs are charged, the data is not from the future, one trade does not carry the profit.

## Limits

This skill diagnoses. It does not lower the standard of a check to get a pass.
