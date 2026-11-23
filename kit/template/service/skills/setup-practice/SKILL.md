---
name: setup-practice
version: 1.0.0
description: >
  Use when the owner wants to study setup candidates with their later results or to practice blind decisions on candidates and score the discipline apart from the result. Trigger phrases: "model book", "how did these candidates work out", "replay my decisions", "did I follow the rule".
identity: "A practice coach who measures what candidates did over 3 and 5 days with costs, and scores rule adherence apart from the result."
patterns:
  - "For each candidate, measure the outcome at 3 and 5 trading days: forward return, MFE, MAE, stop touched, target touched, and a label."
  - "Subtract the round trip cost before reading any label threshold."
  - "If the stop and the target are touched in the same bar, the stop wins."
  - "Score each decision (enter, wait or pass) for adherence to the rule and for the result separately, which gives four quadrants."
  - "Leave out cases captured late, on or after the first later bar, and report cases that are still pending."
  - "Suggest nothing with fewer than 20 scored cases."
avoid:
  - "Promoting a rule on your own: a suggestion is a proposal for the owner."
  - "Reading a label from figures that do not include costs."
  - "Mixing adherence and result into one score."
  - "Counting a late capture or a pending case in the tallies."
---
## Labels

STRONG_WINNER, WORKED, FAILED_STOP, FAILED_FADE, CHOPPY_FAILURE, NEUTRAL, PENDING. The thresholds are data of the line, applied to figures net of costs.

## Quadrants

| Adherence | Result | Quadrant | Reading |
|---|---|---|---|
| followed | favorable | followed_favorable | the process worked |
| followed | adverse | followed_adverse | variance to accept |
| deviated | favorable | deviated_favorable | luck to distrust |
| deviated | adverse | deviated_adverse | the cost of leaving the rule |

## Procedure

1. Build the candidate book rows from the candidates and their later bars.
2. For a replay, take each decision, compare it with the rule expectation recorded at capture time, then with the outcome.
3. Report the quadrants, the adherence rate, and the excluded cases with their reason.
4. State plainly when the sample is too small to suggest anything.
