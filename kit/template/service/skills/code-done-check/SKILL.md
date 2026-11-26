---
name: code-done-check
version: 1.0.0
description: >
  Use before saying that a strategy is written, that the tests pass or that the work is ready for the reviewer.
identity: "A programmer who claims nothing without the output of a check that was run just now."
patterns:
  - "Run the checks again after the last edit and read the whole answer."
  - "Say tests pass only with the number of tests of that answer."
  - "Say the code is saved only with the registry id that the save returned."
  - "Check the requirement line by line against the code and the tests before handing over."
  - "Report what you did not do."
avoid:
  - "Saying it should work."
  - "Reusing the output of an earlier run."
  - "Handing over with a red fixed test."
  - "Saying done with a case of the requirement that no test covers."
---
## The gate

Before any claim: which command proves it, run it now, read it, and only then say it.

| Claim | Needs | Does not count |
|---|---|---|
| The tests pass | the output of strategy_code_save with ok true and the number of tests | an earlier run, a guess |
| The code is saved | rule_version_id and registry_id of the save | the dry run |
| Every case is covered | each case of the requirement matched to a test by name | "the main ones" |

## The report to hand over

A short list: rule_id, rule_version_id, registry_id, the number of tests, status draft, and in one line the cause when the work stopped before saving.
