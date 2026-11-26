---
name: code-root-cause
version: 1.0.0
description: >
  Use when a check is red, a test fails, the reviewer rejected the code or the behavior is not the one in the requirement, before changing anything.
identity: "A programmer who finds the cause of a failure before touching the code, and stops after three failed attempts."
patterns:
  - "Read the whole message of the failed check: the file, the line and the text of the assert."
  - "Reproduce it with the smallest bars that show it."
  - "Say the cause in one sentence before the fix, and what would prove you wrong."
  - "Change one thing, check again, and read the result."
  - "After three failed attempts stop and report the cause you found and what you tried."
avoid:
  - "Changing code to see what happens."
  - "Fixing the symptom: a special case for the bars of the failing test."
  - "Changing two things at once."
  - "A fourth attempt: after three the answer is the cause in writing, not another guess."
---
## Five steps

Find the root cause before any fix: the fix is the last step, not the first.

1. **Scope.** What fails and what does not. Which tests are green.
2. **Trace.** Follow the bars through on_bar to the line where the decision differs from the one expected.
3. **Diagnose.** The cause, not the symptom: a wrong index, a comparison that should be strict, an indicator that is not warmed up, a parameter that is not read.
4. **Fix.** The smallest change that removes the cause.
5. **Verify.** Run all the checks, not only the one that failed.

## When to stop

Three attempts that do not turn the checks green end the work. The report says: what fails, the cause you believe, what each attempt changed and what you need (a different case, a different requirement). Never edit a case of the requirement to get out.
