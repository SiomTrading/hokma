---
name: feature-engineering
version: 1.0.1
description: >
  Use when features are built for a model or a signal from prices, volume, fundamentals or cross-asset data. Trigger phrases: "build features", "normalize this", "select features", "is there leakage", "label for training", "illiquidity measure".
identity: "A careful builder of features who guarantees that every value at a row was knowable at that row, and who selects only on the training part."
patterns:
  - "Keep each feature at one row from data of that row and earlier ones, and test it by removing the later rows: the value must not change."
  - "Take the target from the open of the next bar, the same fill as the backtest."
  - "Standardize with past data only: a rolling or expanding window for a series, the same date for a cross-section."
  - "Carry the last real value forward, with a limit; never interpolate."
  - "Select features inside the training part of each fold and check the correlation and the variance inflation of what is kept."
  - "Measure illiquidity per dollar traded: price times volume times the contract multiplier."
avoid:
  - "A statistic computed on the whole sample, a centered average, or any column shifted backwards."
  - "Interpolating missing values: it reads the next value."
  - "Choosing features on the full history and then cross-validating."
  - "Dividing a return by volume in units: a cheap and an expensive name become comparable only in dollars."
  - "Thresholds on IC, ICIR and t-statistic that contradict each other: the t-statistic is ICIR times the square root of the periods, so state one."
---
## Procedure

1. State the target, the horizon and the bar: the one the owner picks for the run (any interval; the strategy has none). A close is not defined the same way for a future with a long session: say which one.
2. Build the features from the kinds below. Each one is a function of the rows up to the current one.

| Kind | Examples |
|---|---|
| price | returns, log returns, distance from a moving average, range over close, overnight gap |
| volume | relative volume, volume weighted distance, illiquidity per dollar |
| fundamental | valuation, quality, growth, payout, with the date each figure became public |
| microstructure | spread, order imbalance, trade size mix |
| cross | return against the sector, beta adjusted return, rolling correlation |

3. Transform: returns instead of prices; fractional differencing when memory matters (the smallest order that gives a stationary series); z-score or rank; winsorize before scaling; several window lengths as separate features.
4. Cross-section: standardize every date against the assets of that date only; use the rank when outliers matter.
5. Missing values: carry forward the last real value up to a stated number of rows, then leave them missing.
6. Check each feature for leakage by running it on the full data and on the data cut earlier. If a value changes, the feature reads the future and is rejected.
7. Select inside the training rows of each fold: IC and ICIR per period, then drop the weaker of two features that move together, then drop the feature with the largest variance inflation above the stated limit. Every limit is stated by the caller.
8. Output the feature table aligned with the target, with the split by time and never at random.

## Rules about the past

- A rolling window does not read the future. An expanding window keeps a longer memory; neither one is the safe one by itself: both are safe when they stop at the current row.
- Fundamentals enter on the date they were published, as first reported, not as later restated.
- A signal computed at a close can be traded at the next open at the earliest, so the target starts there.

## Limits

This skill builds and tests features. It never reads rows after the current one and never selects with test rows.
