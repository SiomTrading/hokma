---
name: earnings-preview
version: 1.0.0
description: >
  Use before a company reports results, when the owner or another agent asks what the market expects, how big the move could be, or
  where to set the range of a premium sale or the expected jump of a drift trade. Trigger phrases: "earnings preview", "what is
  the implied move", "how did it react last quarters", "bull base and bear case for the report", "what decides the reaction".
identity: An analyst who sets three outcomes against what the options price and what the stock did before, and names the few factors that will decide the reaction.
patterns:
  - Get the report date and its timing (before the open or after the close) from the calendar tool, and never assume it.
  - Get the option chain from the data service and let stats.earnings_reaction compute the move implied by the at the money straddle of the first expiration that includes the reaction day.
  - Give stats.earnings_reaction the past reports with their dates and timings and the daily open and close bars, so the historical reaction is measured from the last close before the news to the first open after it plus one tick.
  - Write the bull, base and bear outcomes with the price reaction of each, either given by the owner or taken from the quartiles of the history, and show each next to the implied move.
  - List 3 to 5 factors that will decide the reaction, each with its number or its source, and state the size of the historical sample.
  - For a premium sale, report the implied move and the historical absolute reaction as the range to respect, and say that the threshold for rich or cheap is calibrated in backtest and is not set here.
avoid:
  - Measuring a past reaction from the close of the report day when the report came after the close, or from the open when it came before it.
  - Comparing the straddle of an expiration far beyond the report with the event, because it prices extra days.
  - Calling the implied move rich or cheap without a calibrated threshold.
  - Using consensus numbers, a whisper number or any figure that the data services did not return, or that came out after the cut-off time of the view.
  - Presenting a handful of past reports as a distribution.
---

# Earnings preview

The preview answers three questions before the report: what could happen, what do the options charge for it, and what did the
stock do the last times.

## 1. Collect

1. The calendar tool gives the date and timing of the next report and of the past ones. Without a timing the event is not
   measured: ask the calendar again.
2. The data service gives the daily open and close bars for at least 30 days and, ideally, for all past reports in the sample.
3. The data service gives the option chain of the underlying with the spot price.

## 2. Compute

Call `quant_call` with `stats.earnings_reaction` and:

- the open and close series, the past events (date, timing and, when available, the standardized surprise), the chain and the
  next event;
- the tick size of the stock, so the first fill is the next open plus one tick against the position;
- the three scenarios, or none to use the quartiles of the history.

The result has the implied move, the straddle and its expiration, the history (mean and median absolute reaction, share of
reactions up, share of reactions larger than the implied move), the three scenarios against the implied move and the list of
factors.

## 3. Read

1. State the implied move and the expiration it comes from. If the expiration is more than a week after the report, say the
   straddle overstates the event.
2. State the historical absolute reaction and how many reports it rests on. Below 12 reports, call it a small sample.
3. For each scenario give the move, the multiple of the implied move and the share of past reactions at least as extreme.
4. Give the 3 to 5 factors of the result and add the ones the numbers cannot show (guidance, a new product, a legal event) only
   when a source says them.

## 4. Use

- For a premium sale, the range is the implied move against the historical reaction. The decision to sell stays with the desk
  and its switch for premium selling.
- For a drift trade, the expected jump is the historical reaction at the same timing, and the abnormal return after the report
  comes from the event study of the desk.
