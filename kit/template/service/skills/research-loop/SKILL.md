---
name: research-loop
version: 1.0.0
description: >
  Use when improving a strategy by trying changes and measuring them, one at a time, against a judge that does not move. Trigger phrases: "improve this strategy", "try a variation", "tune it", "keep or discard", "optimize step by step".
identity: "A disciplined experimenter who changes one thing per experiment, lets a fixed out of sample judge decide, and records every try, the failures included."
patterns:
  - "Fix the judge first: an out of sample backtest with its whole configuration frozen and hashed. You never touch it again in this loop."
  - "Run the starting parameters once as the baseline: it is the number to beat and the first experiment."
  - "Change exactly one parameter per experiment, state why, and run it."
  - "Keep the change only if the judge scores it above the best so far; otherwise discard it and the kept parameters stay as they were."
  - "Let every experiment be recorded with its decision, the discarded and the failed ones too: the deflated Sharpe ratio counts them all."
  - "Read the stall check after each experiment and stop tuning when it fires."
avoid:
  - "Changing the judge, its dates, its costs or its objective in the middle of the loop: that is the error evaluator_changed."
  - "Changing two parameters at once, or a parameter and the rule."
  - "Scoring an experiment on the data the parameters were tuned on."
  - "Keeping a change because it looks plausible when the judge did not score it higher."
  - "Any path to a desk, an order or a broker, and any use of git: the loop reads and records, nothing else."
---
## The loop

| Step | What |
|---|---|
| 1 Judge | an out of sample window that starts after the training data ends, the costs and the fill rules of the house, one objective, all frozen and hashed |
| 2 Baseline | score the starting parameters: experiment 1, kept |
| 3 Change | one parameter, one new value, a reason |
| 4 Score | the judge runs the spec with that one change |
| 5 Decide | keep if the objective is above the best so far, discard if not |
| 6 Record | the experiment, its parameters and its decision go to the ledger of trials |
| 7 Check | has the loop stalled? if so, stop and use `strategy-pivot` |

## Why the judge is fixed

If the judge can move, an experiment can win by changing the question. The loop builds every experiment from the frozen spec and rejects an evaluator whose hash differs, or a run that answers for another spec, with `evaluator_changed`. To change the judge you start a new loop, and the old trials still count.

## Keep or discard

Discarding is not undoing. Nothing is reset and nothing is deleted: the kept parameters simply stay where they were, and the experiment remains in the ledger marked discard. A failed run is also a trial.

## Out of sample

The training data ends at a date you state. The judge starts after it. Parameters are chosen by their score on the judge, so after many experiments the judge itself is partly fitted: the deflated Sharpe ratio, which counts every trial, and `backtest_walkforward` on fresh windows are what tell you how much.

## Budget

A loop has a fixed number of experiments. When it is spent, report what you have. Do not open a second loop on the same idea to get more tries without saying so.

## Limits

The loop proposes and measures. It never sends an order, never touches a desk, and never uses git: undoing a change here means not keeping it.
