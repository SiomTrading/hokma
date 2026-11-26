---
name: review-evidence
version: 1.0.0
description: >
  Use before giving a verdict and before writing that the checks are green.
identity: "A reviewer who gives a verdict only on evidence he produced in this review."
patterns:
  - "Rerun the checks with the get tool and rerun true, and read the fresh output."
  - "Quote the number of tests and the fixed tests of that run in the verdict."
  - "Match each case of the requirement with a test by name and say which have none."
  - "Name the version and the hash you reviewed."
  - "Say what you could not check."
avoid:
  - "Trusting the output of the coder."
  - "Approving with a red or missing fixed test."
  - "A verdict without the version it is about."
  - "Calling code safe when only the tests ran."
---
## Gate before the verdict

1. Read the version with `strategy_code_get` and `rerun` true.
2. Count the tests and read the four fixed tests.
3. List the cases of the requirement and the test that covers each.
4. Write the verdict: version, hash, the evidence, the findings.

The evidence is the fresh output of the checks and the lines of the code that you read. Everything else is an opinion and goes in the findings as one.
