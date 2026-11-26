---
name: code-test-first
version: 1.0.0
description: >
  Use before writing or changing any line of strategy.py. Trigger phrases: "write the strategy", "add a rule", "fix this decision", "change the stop".
identity: "A programmer who writes the test first, watches it fail for the right reason, and only then writes the least code that passes."
patterns:
  - "Turn each case of the requirement into a test_ function with bars worked out by hand."
  - "Run the checks with the strategy missing or empty and read that every test fails for the reason you expect."
  - "Write the smallest strategy that passes, then run the checks again."
  - "Add one test for each rule you add and one for each bug before you fix it."
  - "Keep the expected decision in the test as a literal, never copied from the output of the code."
avoid:
  - "Writing strategy.py first and the tests after it to match what it does."
  - "A test that passes the first time you run it: it proved nothing."
  - "Changing a case of the requirement to make a test pass."
  - "Bars that hide the rule: use a case where the rule is just true and one where it is just false."
---
## Why

A test written after the code agrees with the code, right or wrong. A test that never failed has not shown that it can.

## Steps

1. Read the requirement: its behavior statements and its cases.
2. Write test_strategy.py with one function for each case, using `replay(strategy.ClassName, bars)` and asserting the action of the last decision (and of the bars before the warm-up when it matters).
3. Check with `strategy_code_save` and `dry_run` true. Without a strategy the answer says it is missing; with a stub that always holds, the cases that expect long or short fail. That is the red step: keep the output.
4. Write strategy.py. Check again until every test and the two fixed tests are green.
5. Save with `dry_run` false.

## A good case

Four bars: a mother bar with high 12 and low 8, an inside bar with high 11 and low 9, and a bar that closes at 11.5. The decision on the last bar must be long. The numbers are chosen so that one tick less would not be.
