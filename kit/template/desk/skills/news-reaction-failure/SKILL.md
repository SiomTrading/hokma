---
name: news-reaction-failure
version: 1.0.0
description: >
  Use when the owner or another agent asks whether the market failed to react to good news on a crowded stock, or whether a
  stock sells off on favourable reports. Trigger phrases: "did it ignore the good news", "priced in", "sold the news", "does it
  fail to react to beats", "is the reaction to news fading".
identity: A reviewer who judges a failure to react with a significance test over independent groups of events, and who refuses to confirm anything with too few groups or with a count of failures.
patterns:
  - Take the list of news events from a primary source with dates and timings, and say who curated the list and how.
  - Call stats.reaction_failure with the returns, the events, a market series and the direction of the news (1 for favourable), with the 3 day window.
  - Read the number of independent groups first; events whose windows overlap are merged into one group, and fewer than 3 groups gives INSUFFICIENT_EVIDENCE.
  - Report the verdict (CONFIRMED, NOT_CONFIRMED or INSUFFICIENT_EVIDENCE) with the mean directional abnormal return, the t statistic, the number of groups and the p values.
  - Show the share of events with no rise only as a reference and state that with noise it is near half of the events.
  - For macro news, use the same call with the dates and timings of the releases.
avoid:
  - Confirming a failure because most events had no rise; the plain ratio of failures confirms noise in roughly half to most of the random cases.
  - Counting overlapping events as separate observations.
  - Trusting an event list that a language model collected from the web without checking each event against a primary source.
  - Using intraday reactions from this test; it works on daily returns.
  - Reading NOT_CONFIRMED as proof that the market reacts; it only says a failure is not established.
---

# News reaction failure

A failure to react is a claim about drift after news, so it is tested as one.

## 1. Inputs

1. Events: date, timing (before open, after close or intraday) and an id. Every event is checked against a primary source.
2. Daily returns of the stock and of a market series, with enough history before each event for the estimation window.
3. The direction of the news: 1 when the news is favourable and a rise is expected, -1 when the reverse.

## 2. Test

Call `quant_call` with `stats.reaction_failure`. The function:

1. measures the abnormal return of each event over the 3 day window from the reaction day;
2. merges events with overlapping windows into groups, so the correlated ones count once;
3. tests the mean directional abnormal return across groups with a t test;
4. returns CONFIRMED when the mean is significantly below zero (the favourable news was sold), NOT_CONFIRMED when it is not, and
   INSUFFICIENT_EVIDENCE when there are fewer than 3 groups.

## 3. Report

Give the verdict, the groups, the mean abnormal return, the t statistic and the p value of each side. State the reason line of
the result. If events were skipped, name the reasons (missing timing, not enough history).

## 4. Limits to say out loud

- The event list can be wrong or invented; the test cannot fix that.
- With strongly correlated events the false positive rate of any such test rises: say so when most events sit in a few groups.
- A confirmed failure is a finding about the past, not a trade signal.
