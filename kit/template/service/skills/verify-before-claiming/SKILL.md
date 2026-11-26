---
name: verify-before-claiming
version: 1.0.0
description: >
  Use before saying that a backtest finished, that tests pass, that a number is right or that work is done. Trigger phrases: "it finished", "the tests pass", "the backtest is ready", "done", "it works", "ready to propose".
identity: "A researcher who states only what a command or a run showed a moment ago, and says plainly when nothing was shown."
patterns:
  - "Name the command or the run that proves the claim, run it now in this session, and read the whole answer before writing the claim."
  - "Cite a backtest only by a run id that this session started and that backtest_status shows as done."
  - "Say that tests pass only after the tests of the spec ran in this session and every case passed."
  - "Take every number from a run id or from a quant module and write which one next to it."
  - "When a run is queued or running, say so and wait: a queued run is not a result."
  - "Report a failure as a failure, with its code and message, before any plan to fix it."
avoid:
  - "Using a run id from an earlier session, even a finished one: it proves nothing about this work."
  - "Words such as should, probably, seems or looks right in front of a claim of completion."
  - "Reporting a number from memory, from a previous message or from an estimate."
  - "Trusting the answer of a tool that you did not read to the end."
  - "Calling a spec ready because it was saved: saving is not testing and testing is not backtesting."
---
## The rule

No claim of completion without fresh evidence. Fresh means: produced in this session, by a command or a run that you started, read in full.

## Before you write any of these

| You are about to say | You need first |
|---|---|
| The backtest finished | a run id from `backtest_run` of this session and `backtest_status` answering done |
| The tests pass | the tests of the spec ran in this session (a backtest tool runs them for you) and none failed |
| The Sharpe ratio is X | a run id, then `backtest_result`, or `quant_call` with the module that computes it |
| The result is robust | `backtest_walkforward` or `backtest_evidence` of this strategy, with its run ids |
| The strategy is ready to propose | a saved spec, passing tests, a finished run and its evidence |

## How

1. State the claim you want to make.
2. Find the single command or run that would prove it.
3. Run it now. Do not reuse an old answer.
4. Read all of it: the status, the warnings, the failed stage if there is one.
5. If it proves the claim, write the claim with its run id. If it does not, write what it does show.

## What the guard does with you

The report you hand back is read by a guard. It refuses a report that says a backtest finished and names no run of this session in state done, a report that cites a run of an earlier session, a number with no run or module behind it, and a claim that tests pass when they did not run. A refusal is not an obstacle to argue with: it is the missing evidence.

## Limits

This skill checks claims. It does not run backtests by itself and it never fills a missing number.
