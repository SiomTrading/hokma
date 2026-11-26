---
name: plan-writing
version: 1.0.0
description: >
  Use when work has more than one step and each step needs an exact file, command and expected result. Trigger phrases: "write the plan", "break this into steps", "plan the build".
identity: "A planner who writes steps small enough that an agent with no context can execute them and see them pass."
patterns:
  - "Start with the goal in one sentence and the list of files that will be created or changed."
  - "Make each step one action of two to five minutes of machine time."
  - "Give each step the exact file path, the command to run and the output that means it worked."
  - "Put the test before the code in the order of steps."
  - "Mark steps that can run in parallel with [P] and name their dependency otherwise."
  - "Finish with a step that runs the whole check and reports the result."
avoid:
  - "Steps such as handle errors or add validation with no detail."
  - "A step that changes more than one concern."
  - "Placeholders such as TBD or similar to the step above."
  - "A plan that names a file that does not exist without saying it is new."
---
## Shape

```markdown
# Plan: {goal}
Files: {list, new or changed}
## Steps
1. Write the failing test in {file}. Run: {command}. Expect: {failure text}.
2. Write the minimal code in {file}. Run: {command}. Expect: pass.
3. [P] ...
## Final check
Run {command}. Expect {result}.
```

## Review before handing over

- Every requirement of the spec appears in a step.
- No step depends on something a later step creates.
- Names are the same in every step (a function called one thing in step 2 is called the same in step 6).
- Each estimate is in machine time.
