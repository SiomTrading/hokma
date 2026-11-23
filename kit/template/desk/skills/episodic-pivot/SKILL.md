---
name: episodic-pivot
version: 1.0.0
description: >
  Use when the owner or another agent asks whether a stock that jumped on news is a day one episodic pivot candidate, how good
  the catalyst is, or whether it should be handed to the drift screen. Trigger phrases: "episodic pivot", "gap on news", "is
  this a day one candidate", "catalyst quality", "FDA gap", "guidance gap".
identity: A classifier of news driven jumps who weighs the catalyst and four price confirmations, and who refuses to call a candidate actionable on a catalyst nobody verified.
patterns:
  - Get the catalyst type from the news tools and whether it was verified against a filing or a release; pass both to indicators.screens.episodic_pivot with the reaction date and the as_of time of the view.
  - Report the catalyst quality and the four confirmations with their numbers; range against the mean of 20 sessions, volume against the same, close position inside the bar, and the risk to the low of the day.
  - Report the state; ACTIONABLE_DAY1, DAY1_WATCH, DELAYED_EP_WATCH, CATALYST_WATCH or REJECT, with the reason when it is a reject.
  - State the stop as the day one low and the entry as the next open plus one tick; when that bar is not in the data show the entry as an estimate.
  - When the result flags pead_handoff, send the candidate to the drift screen and say so.
  - Repeat the warning when the catalyst is unverified.
avoid:
  - Searching for news inside this skill; the catalyst comes from the news tools.
  - Calling a candidate actionable with an unverified catalyst.
  - Measuring the risk from a fill at the signal close.
  - Keeping a candidate after the day one low broke on a close.
  - Giving a size or an order; those belong to the risk committee and the trader.
---

# Episodic pivot

An episodic pivot is a stock that jumps on news with a real change in what the business is worth. Day one is the session that
trades on the news.

## 1. Run it

1. Collect the catalyst type (earnings, guidance, FDA, contract or merger), whether it was verified, and the reaction date.
2. Call quant_call with indicators.screens.episodic_pivot with the daily bars, catalyst, catalyst_verified, reaction_date and as_of.
3. If there is no value, give the warning (not enough bars, a missing value).

## 2. State the class

- ACTIONABLE_DAY1: day one is the last closed bar, the catalyst is verified and strong, and all four confirmations hold.
- DAY1_WATCH: day one is the last closed bar and at least two confirmations hold, or the catalyst is not verified.
- DELAYED_EP_WATCH: day one was up to a few sessions ago, held its low on closes and passed at least three confirmations.
- CATALYST_WATCH: a good catalyst but no reaction session yet.
- REJECT: weak catalyst, broken low or too old.

## 3. Entry and stop

The stop is the day one low. The entry is the next open plus one tick. When the next bar is in the data the risk uses it;
otherwise the close plus one tick is shown as an estimate. A bar that touches the stop after the entry counts as stopped first.

## 4. Handoff

Earnings and guidance candidates carry pead_handoff: run the drift screen on them next.
