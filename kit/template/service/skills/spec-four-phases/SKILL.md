---
name: spec-four-phases
version: 1.0.0
description: >
  Use when an idea, a source or a strategy need must become a spec that the coder can build without questions. Trigger phrases: "I want to create a strategy with", "write the spec", "turn this idea into tasks".
identity: "A specifier who moves a request through four gated phases (spec, checklist, plan, tasks) and does not let the next phase start until the current one is clean."
patterns:
  - "Work inside the folder of the strategy: service/strategies/STR-{NNN}-{slug}/. Every phase file lives there."
  - "Phase 1 writes spec.md: only WHAT and WHY, with prioritized stories (P1, P2, P3) and Given/When/Then scenarios."
  - "Phase 2 writes checklist.md and checks every item against the spec; fix the spec and re-check, three rounds at most."
  - "Phase 3 writes plan.md: engine contract used, inputs, parameters, risks, what the engine can fill."
  - "Phase 4 writes tasks.md: one responsibility per task, [P] on tasks that can run in parallel, a machine-time estimate on each."
  - "Then create the tasks with task_create and cite the path of spec.md in each body."
avoid:
  - "Starting a phase while the previous one has an open question."
  - "Putting code or library names in spec.md."
  - "Leaving more than three [NEEDS CLARIFICATION] markers: ask the owner instead."
  - "Creating tasks before tasks.md exists."
  - "Sending the coder a task without the path of spec.md."
---
## Folder

```
service/strategies/STR-{NNN}-{slug}/
  spec.md        phase 1: what and why
  checklist.md   phase 2: validation of the spec
  plan.md        phase 3: how it is built against the engine
  tasks.md       phase 4: the work, in order
  adr/           decisions that are hard to reverse
  versions/      the coder writes here, never Ian
  results/       optimizer output
```

The bank creates the folder with the card of the strategy (`bank_create`). The number is the bank id with three digits.

## Phase 1: spec.md

Use the template of `spec-templates`. Only what and why. Each story has a priority and can be tested alone. Success criteria are numbers (trades, drawdown, hit rate on a case), not feelings.

Gate before phase 2:
- no open [NEEDS CLARIFICATION]
- every story has Given/When/Then
- every requirement can be checked with bars
- there is an out of scope section
- no library, language or file name in the text

## Phase 2: checklist.md

One line per check, each tied to a line of the spec. A failed line sends you back to phase 1.

## Phase 3: plan.md

Name the pieces of the engine that the strategy uses (`factory_list`, `library_list`), the parameters with default, minimum and maximum, and what the engine cannot fill yet. If the strategy needs new stored data, add an Inventory section that lists what already exists and why it is not reused.

## Phase 4: tasks.md

Each task has: what to do, the exact file, and the done criterion. Mark independent tasks with [P]. Order respects dependencies. Then `task_create` for each row, `task_link` for dependencies, and write the task id back in tasks.md.

## When not to use it

A fix of one to three lines, a configuration change or an emergency. Say so in the body of the task.
