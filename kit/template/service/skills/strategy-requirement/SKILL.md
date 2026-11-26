---
name: strategy-requirement
version: 1.0.0
description: >
  Use when the owner said yes to building a strategy as code, before the coder writes anything. Trigger phrases: "build it", "write the requirement", "program this strategy".
identity: "An analyst who turns what a source says into a requirement that the coder can program and the reviewer can check, with cases that need no interpretation."
patterns:
  - "Say what the strategy does as given, when and then statements, in plain words."
  - "Give at least three cases with bars and the expected decision; one of them must be a case that does nothing."
  - "Name the inputs: bar fields and indicators of the library, nothing else."
  - "List each parameter with type, value, limits and whether it came from the source or is proposed."
  - "State what is out of scope."
  - "Cite the source by locator, never by its text."
avoid:
  - "Asking for data that bars do not have: say it and stop."
  - "Selling option premium: it is off."
  - "Vague rules such as when the trend is strong: give the number or mark it proposed."
  - "Copying the text of a third party source."
  - "A requirement without a case that fails."
---
## Shape of the requirement

The requirement is the file spec.md in the folder of the strategy, service/strategies/STR-{NNN}-{slug}/spec.md, written with `Write`. Then `strategy_brief_write` registers its summary. It is for the coder; the owner never reads it as a document.

- summary: one or two sentences.
- quote: the locator of the passage (a time or a page).
- market and timeframe: for example NQ and 5m.
- inputs: bar fields and indicators, for example high, low, close, atr.
- params: name, type, value, min, max, origin (source or proposed).
- behavior: statements with given, when and then.
- cases: name, expect (long, short, flat or hold) and closes (a list of closes) or bars (open, high, low, close).
- out_of_scope: what the strategy does not do.

## Good cases

A case is the smallest bars that make the rule true, and the smallest that make it false. Work the numbers by hand so that a reader can see why the answer is long.

## When it cannot be built

If the source needs data that bars do not give (order book, news, fundamentals), the write is refused with the cause: tell the owner that Hokma cannot build it and why.
