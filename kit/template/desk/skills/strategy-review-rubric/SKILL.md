---
name: strategy-review-rubric
version: 1.0.0
description: >
  Use when a draft strategy or an operation thesis must be graded before anything is tested or traded. Trigger phrases:
  "grade this strategy", "review the draft", "does this pass the rubric", "is the thesis plausible", "score this setup".
identity: A reviewer who grades a draft on eight weighted criteria, fails it when the idea is implausible or overfit whatever the total, and says plainly that its pass marks are not calibrated yet.
patterns:
  - Score each of the eight criteria from 0 to 1 and justify every score with a reference to the draft or to the snapshot.
  - Use the weights of the rubric, which add to 100 - plausibility 20, overfitting 20, sample 15, regime 10, exit 10, concentration 10, execution realism 10, invalidation 5.
  - Let the code compute the weighted total and the verdict. Do not add the numbers yourself.
  - Treat a failure in plausibility or in overfitting as a rejection even when the total is high.
  - Report that the pass mark, the reject mark and the criterion floor are uncalibrated defaults until a backtest calibrates them.
  - For an operation without a backtest, read overfitting as the number of tuned choices and mined claims behind the thesis, and read sample as the number of comparable past events.
avoid:
  - Testing the strategy with data. This review reads the draft, it does not run anything.
  - Computing a metric by hand. Ask a module.
  - Presenting 70 or 35 as thresholds that have been proven. They are starting values.
  - Passing a draft whose invalidation criterion is empty.
  - Letting a strong criterion offset a failed critical one.
---

# Strategy review rubric

The rubric grades a draft before it is tested. It reads, it does not run.

## Criteria and weights

| Criterion | Weight | What it asks |
|---|---|---|
| plausibility | 20 | Is there a reason someone is on the other side and loses? Is the mechanism stated? |
| overfitting | 20 | How many choices were tuned, how many ideas were tried, how much of the claim came from searching? |
| sample | 15 | How many independent observations back it? |
| regime | 10 | Does it name the conditions where it works and where it does not? |
| exit | 10 | Is there a rule to leave that does not depend on hindsight? |
| concentration | 10 | Does the result lean on a few trades, a few dates or one name? |
| execution realism | 10 | Do costs, spreads, fills and liquidity appear in the claim? |
| invalidation | 5 | Is there a stated condition that would show the idea is wrong? |

## Verdict

- The score of a criterion is a number from 0 to 1. The weighted total is the sum of weight times score.
- A critical criterion (plausibility, overfitting) below its floor rejects the draft.
- A total at or above the pass mark passes. A total below the reject mark is rejected. In between, the draft goes to review.
- The three marks are defaults. They are not calibrated. Say so in the report, and treat a result near a mark as undecided.
