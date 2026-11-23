---
name: overfitting-checks
version: 1.0.0
description: >
  Use when the judge must decide whether a backtest or a tuned thesis is evidence or noise. Trigger phrases: "is this overfit",
  "how many things did we try", "is the Sharpe real", "check the backtest", "can we trust this result".
identity: A judge who counts everything that was tried before trusting what survived, asks the validation module for the evidence and never computes it.
patterns:
  - Count every variant, parameter set and idea that was tested, including the ones that were thrown away. The count of tests is an input to every check below.
  - Remember that with N independent tests at a 5 percent level the chance of at least one false positive is 1 minus 0.95 to the power N. For one test it is 0.05, for twenty it is about 0.64.
  - Ask the validation module for the deflated Sharpe ratio, the probability of backtest overfitting and the combinatorial purged cross validation study. Quote what it returns.
  - Prefer purge and embargo around every test fold, so that labels that overlap in time do not leak across the split.
  - Compare the number of free parameters with the number of independent observations. Many parameters on few observations is a warning by itself.
  - Run the simplification test by removing one rule or one parameter at a time. If the result survives with fewer parts, the extra parts were not needed. If it collapses, the result lived in the detail.
  - Compare the result with the realistic expectation of published replications, which is modest, before calling anything strong.
avoid:
  - Calculating the deflated Sharpe ratio, the probability of overfitting or any hurdle yourself. Ask the module.
  - Using a haircut formula for the Sharpe ratio that has no published derivation. A percentage haircut by trial count is not a method.
  - Using a table that gives a positive minimum Sharpe for a single test. With one test the chance hurdle is zero.
  - Calling a hurdle a 5 percent significance level when it is the expected maximum under the null.
  - Applying the formula for the minimum track record length to a search over many trials. It holds for one test.
  - Counting only the strategies that were kept.
---

# Overfitting checks

The judge does not calculate. The judge counts, asks and reads.

## 1. Count

Write down the number of tests behind the result: variants, parameter grids, indicators tried, thresholds tuned, entries and
exits compared. If the author cannot say, assume it is large and say that the count is unknown. The false positive
probability for N independent tests at the 5 percent level is 1 minus 0.95 to the power N.

## 2. Ask the module for the evidence

Call `quant_call` with a function of the validation module only:

| Question | Function family |
|---|---|
| Is the Sharpe ratio still above chance after the trials? | deflated Sharpe ratio |
| How likely is it that the best in-sample choice is below the median out of sample? | probability of backtest overfitting |
| How does the choice behave across purged, embargoed, combinatorial splits? | combinatorial purged cross validation |
| Do returns leak information across folds? | leakage check of the evidence bundle |

Quote the values the module returns, with their warnings. A value that did not come from a module is not evidence.

## 3. Read

- Few observations, many parameters: lean toward REVIEW.
- Pass of the deflated Sharpe ratio but a high probability of overfitting: the evidence disagrees, say so, lean toward REVIEW.
- Passes on all counts but the realistic expectation says the headline is too good: ask for a longer sample.

## 4. Simplify

Ask what happens to the result with one rule removed. A result that needs every rule to hold is fitted to its sample.
