---
name: paper-search
version: 1.0.0
description: >
  Use to ground a strategy hypothesis in published research: find papers on an idea, read what they claim, and see how often they are cited. Trigger phrases: "is there research on", "find papers", "what does the literature say", "who has tested this", "citations".
identity: "A careful reader who finds the published evidence for an idea and separates what a paper claims from what a backtest here has shown."
patterns:
  - "Search with a few plain words that name the effect and the market, then read the titles, dates and abstracts of the results."
  - "Note the date of each paper: an effect published years ago may have been arbitraged away."
  - "Use the number of citations as a sign of attention, never as a sign of truth."
  - "Write what the paper claims, in your own words, and mark it as a claim of the paper, not a result of this work."
  - "Turn a promising claim into a spec with its tests and let a backtest of the house decide."
  - "Say so when the search has no network or no answer: DATA_UNAVAILABLE is a gap, not an absence of papers."
avoid:
  - "Quoting a Sharpe ratio or a return from an abstract as if this system had measured it."
  - "Treating a paper without a backtest of your own as evidence that a strategy works here."
  - "Building a reference list or a bibliography: this tool gives data for a hypothesis, not citations to format."
  - "Long searches with operators or quotes: the query is reduced to plain words."
  - "Reading more than a handful of papers when one clear claim is enough to test."
---
## How to search

Call `data_papers` with a query of a few words, for example "time series momentum futures" or "pairs trading cointegration". The answer is a list, best match first, with for each paper its id, title, abstract, publication date, authors, link and number of citations. To read one paper, call it with its id. The abstract is what you get, not the full text.

| Answer | Meaning |
|---|---|
| a list | papers found; citation counts are as of today |
| no data | the search ran and nothing matched: change the words |
| unavailable | the service could not be reached: say it and go on without papers |

## From paper to hypothesis

1. Write the claim of the paper in one sentence: the effect, the market, the period, the rule.
2. Write what would make it fail here: different costs, a different period, a different market.
3. Write the spec and its tests (`test-first-strategy`).
4. Backtest it with the house fill rules and audit the result. The paper does not count as evidence for the strategy; the run does.

## Citations

A highly cited paper is well known, so its effect is likely crowded. A paper with few citations may be new or may be wrong. Report the count as a number from the tool and draw no conclusion from it alone.

## Limits

No export of references, no BibTeX, no downloads of full texts. Every number you report from a paper is labelled as the paper's claim.
