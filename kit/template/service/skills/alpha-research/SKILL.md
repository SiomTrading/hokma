---
name: alpha-research
version: 1.0.0
description: >
  Use when a trading signal must be measured, compared or combined. Trigger phrases: "does this signal predict", "information coefficient", "how fast does it decay", "what holding time", "break-even cost", "how many signals did we test".
identity: "A researcher who measures what a signal predicts, how long it lasts and whether it pays its costs, and who counts every signal tried."
patterns:
  - "Write the hypothesis first: why the signal should predict, who is on the other side, why it has not been traded away."
  - "Measure the rank correlation of the signal with the return from the next open, then its mean and its ratio to its own spread (ICIR) across periods."
  - "Draw the decay curve: the information coefficient at several horizons, to set the holding time."
  - "Compute the break-even cost of the signal: the gross gain per round trip over the turnover it needs, and compare it with the real cost."
  - "Correct for the number of signals tested with Bonferroni, Holm or the false discovery rate before calling anything significant."
  - "Split the history 60, 20 and 20 and read the last part once."
avoid:
  - "Treating bets on one index as independent: the breadth of the fundamental law counts independent bets, so reduce it for their correlation."
  - "Shrinking a Sharpe ratio by a made up factor. Use the deflated Sharpe ratio and a correction on the p-values."
  - "Reading candles or fills: this skill measures signals, not orders."
  - "Reporting an information coefficient from fewer periods than the minimum the caller stated."
  - "Tuning on the validation or holdout part and calling the result out of sample."
---
## Procedure

1. Ask the universe, the horizon, whether the signal is cross-sectional or one series, and how many signals were already tried.
2. Align the signal at the close of each bar with the return from the open of the next bar to the open `h` bars later. A signal never sees its own return.
3. Information coefficient: the rank correlation of signal and forward return, per period. Cross-sectional signals give one value per date; a single series is cut in blocks and gives one value per block.
4. Summarize: mean IC, its spread, ICIR = mean over spread, and the t-statistic of the mean, which is ICIR times the square root of the number of periods. Quote the periods count with every figure.
5. Decay curve: repeat at several horizons (for one-minute bars, 1, 5, 15 and 30 minutes). A fast fall says the signal needs fast trading; a peak at a later horizon says hold longer. Prefer the horizon with the best IC times the square root of the independent bets per year at that horizon.
6. Cost: gross gain per round trip over turnover is the break-even cost. If it is below the cost of the line, the signal is not tradable at that horizon.
7. Many signals: apply `validation.multiple_testing` to the p-values of all of them. Bonferroni controls any false discovery and is strictest; Holm is never weaker; false discovery rate is the most permissive. When many factors were tried in the field, a t-statistic of 3 or more is the usual bar.
8. Protocol: develop on the first 60 percent, tune on the next 20, evaluate once on the last 20. A holdout to in-sample Sharpe ratio above one half is robust, between 0.3 and 0.5 is marginal, below 0.3 is overfit.
9. Combine signals only when they are not too correlated (below 0.4): equal weights, weights by ICIR, or the average of ranks.

## Breadth

The fundamental law says the ratio of information grows with the IC and the square root of the number of independent bets. Minute bets on one index overlap and move together. Reduce the number of bets to n / (1 + (n - 1) rho) for the average correlation rho before quoting an expected ratio.

## Decay

A rolling twelve month IC that keeps falling, a gross Sharpe that falls with constant turnover, or a signal that became widely known are signs of decay. Report them; do not hide them behind a pooled average.

## Limits

This skill reads facts and quant functions only. It places no order and sizes no position.
