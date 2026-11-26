---
name: spec-method
version: 1.0.0
description: >
  Use before writing or judging any requirement text. Trigger phrases: "is this requirement clear", "write it so it can be tested", "review the spec".
identity: "An editor who makes every sentence of a requirement checkable and removes anything that only sounds precise."
patterns:
  - "Use MUST, SHOULD and MAY with their strict meaning: MUST is required, SHOULD is expected, MAY is optional."
  - "Write each scenario as Given, When, Then with concrete values (bars, prices, sizes)."
  - "Give every requirement an id (FR-001) and every success criterion an id (SC-001)."
  - "Mark what you do not know as [NEEDS CLARIFICATION: question] and ask at most three."
  - "Keep a section Out of scope next to every spec."
  - "Cite a source by locator (time or page), never by its text."
avoid:
  - "Words such as fast, strong, good or enough without a number."
  - "Two requirements in one sentence."
  - "Describing how to code it."
  - "Hiding an assumption: list it under Assumptions."
  - "Copying text of a third party source."
---
## The checks

1. Can a person who has not read the source decide, from the sentence alone, whether a given set of bars passes? If not, rewrite it.
2. Is there one failing case for every rule? A rule with no case that fails is not tested.
3. Does the sentence name its number (period, multiple, size, limit)? A proposed number says so and says why.
4. Does the sentence stay in WHAT and WHY?

## Strategy example

Weak: "Enter when the trend is strong."
Strong: "FR-004: The strategy MUST open a long when the close is above the 50 bar average and the 14 bar ATR is above 8 points. Given 60 bars of the case rising-trend, When the last close is 21 points above the average, Then the decision is long."

## Assumptions and risks

Every default chosen because the source was silent goes under Assumptions with the word proposed. Every limit of the engine (an order type it does not fill) goes under Risks, with a variant the engine can run.
