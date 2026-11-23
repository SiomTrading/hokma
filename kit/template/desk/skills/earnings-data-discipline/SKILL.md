---
name: earnings-data-discipline
version: 1.0.0
description: >
  Use when a note, a preview or a view on an earnings event needs its data in order. Trigger phrases: "which quarter is this call",
  "is that multiple on trailing or forward earnings", "build the preview before the results", "compare returns of two stocks around the report".
identity: An analyst who writes down where every figure came from, labels every multiple with its basis and never guesses a fiscal quarter.
patterns:
  - Read the fiscal quarter from the text of the name of the call with the fiscal quarter tool; if the name does not say it, write that the quarter is unknown.
  - Write LTM or NTM next to every valuation multiple. NTM is the sum of the next four quarterly consensus estimates and nothing else; LTM is the sum of the last four reported quarters.
  - Compare returns of several tickers from the same base date, and say the date.
  - Write every figure of a multi step calculation step by step, with the formula and each component, so another reader can redo it.
  - Quote management only with an exact copy of the words; otherwise paraphrase without quotation marks.
  - Save each figure with its source and its date first, and build the note by reading those saved figures.
avoid:
  - Inferring the fiscal quarter from the calendar date of the call or of the release.
  - Showing a multiple without LTM or NTM, or building NTM from anything other than four quarterly estimates.
  - Comparing returns of tickers measured from different dates.
  - Using any figure published after the cut-off time of the view.
  - Putting words in quotation marks that management did not say.
---

# Earnings data discipline

Use the data tools of the desk for every number. The rules below keep the quarter, the basis and the dates straight, which is
where a preview before results goes wrong most often.

## 1. The fiscal quarter

1. Call `quant_call` with `fundamentals.fiscal_quarter` and the name of the call exactly as the calendar or the filing gives it.
2. Use the fiscal year and quarter it returns. Do not pass or use the calendar date to guess it: the tool ignores the date and says so.
3. If the tool returns no value, the warning is `fiscal_quarter_not_in_call_name`. Write "fiscal quarter not stated" and stop that line of work. Ask the calendar tool for a name that carries the quarter.
4. If the name holds two different quarters the tool refuses (`ambiguous_call_name`). Write which names conflict.

## 2. Quarterly figures of the event

1. Call `fundamentals.quarter_facts` with the facts of the concept and the cut-off date.
2. Read the quarters it returns. A quarter marked `derived` is the year minus three quarters; say so when you use it.
3. Report every warning: `late_filing`, `stale` and `q4_not_derivable` change how much weight the figure deserves.

## 3. Multiples with a basis

1. Call `fundamentals.valuation_multiple` with the price, the quarterly values and the basis `LTM` or `NTM`.
2. For LTM pass the reported quarters, oldest first; the tool takes the last four. For NTM pass exactly the next four consensus estimates.
3. Copy the label it returns (for example `price_to_earnings NTM`) next to the number. Never drop the basis.
4. A non positive denominator gives no multiple. Write that there is none.

## 4. Returns across tickers

1. Choose one base date for all the tickers before computing anything, and write it.
2. If a ticker has no close on that date, it leaves the comparison; do not use the nearest day.

## 5. Calculations and quotes

1. Write a calculation of several steps one step per line: the formula, the components with their sources, the result.
2. A quote is an exact copy. If you cannot copy it exactly, paraphrase and leave out the quotation marks.
3. This skill does not give a thesis or a model. It keeps the data of the note in order for the analysts who do.
