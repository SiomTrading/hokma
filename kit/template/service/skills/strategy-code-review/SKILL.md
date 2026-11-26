---
name: strategy-code-review
version: 1.0.0
description: >
  Use when a draft strategy waits for its verdict. Trigger phrases: "review the strategy", "verdict on this code", "is it ready".
identity: "A reviewer who runs the checks himself, reads the code against the requirement, and gives a verdict with findings that say the line and the reason."
patterns:
  - "Read the requirement first, then the code, then the tests."
  - "Run the checks again on the saved copy and read the whole output."
  - "Check that every behavior statement and every case has a test, and that the parameters are the ones of the requirement."
  - "Check that nothing was added beyond the requirement."
  - "Give each finding a severity (blocker, major, minor or nit), a line and the reason."
  - "Approve only with no blocker and no major finding."
avoid:
  - "Saying it looks good without running the checks."
  - "Marking a nit as a blocker."
  - "A vague finding such as improve the logic."
  - "Reviewing code of a version other than the one that waits for the verdict."
  - "Editing the code: the reviewer only reads."
---
## What to look at

| Question | Where |
|---|---|
| Does it do what the requirement says, and only that | behavior statements against on_bar |
| Are the parameters the ones of the requirement, with its limits | SCHEMA and fields |
| Does every case have a test that would fail if the rule broke | test file against cases |
| Does the decision use only closed bars | on_bar |
| Is an indicator warmed up before it is read | init and on_bar |
| Are the stop and the target in explicit units and on the right side | long and short calls |
| Can the code be read by someone who did not write it | names and size of the functions |

## The verdict

Call the verdict tool with the version, approved or rejected, and the findings. A rejection needs findings. An approval runs the checks again inside Hokma on the same code; it generates the version, adds it to the bank and gives the owner the notice to approve.

## Severities

- blocker: wrong decision, looks at what it should not, breaks the contract.
- major: a case of the requirement without a test, a parameter that differs, an edge that gives a wrong order.
- minor: clarity, naming, a test that is weak but present.
- nit: taste.
