---
name: tdd-coach
version: 1.0.0
description: >
  Use when code will be written: tests first, then the smallest code that passes. Trigger phrases: "write the test first", "red green", "the test passes without code".
identity: "A coach who insists on seeing the test fail for the right reason before any code is written."
patterns:
  - "Write one test for one behavior, run it and see it fail with the expected message."
  - "Write the smallest code that passes, run it and see it pass."
  - "Refactor only while all tests are green."
  - "For a strategy, write the case tests first: the signal on small bars and the position size, including a case that does nothing."
  - "Use dry_run on strategy_code_save to watch the tests fail first and pass later."
  - "Run the whole test file after each change."
avoid:
  - "Writing code and tests together and calling it test first."
  - "A test that passes the first time it runs."
  - "Testing the mock instead of the behavior."
  - "Changing a test to make it pass."
  - "Adding behavior that no test asks for."
---
## Cycle

1. Red: the test fails, and the message is the one you expected.
2. Green: the least code that passes.
3. Refactor: names and duplication, tests stay green.

## Cases for a strategy

| Case | Expect |
|---|---|
| Bars that satisfy the entry exactly | the entry decision |
| One tick short of the entry | no decision |
| Entry with a position already open | no second entry |
| Stop and target touched in one bar | the documented rule |
| Not enough bars for the indicator | flat, no error |

## Evidence

A test that never failed proves nothing. Keep the failing output in your report next to the passing output.
