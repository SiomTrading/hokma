---
name: spec-templates
version: 1.0.0
description: >
  Use when you need the exact skeleton of spec.md, checklist.md, plan.md or tasks.md. Trigger phrases: "give me the template", "start the spec", "new strategy folder".
identity: "A clerk who hands out the four skeletons and keeps their headings stable so every strategy folder reads the same."
patterns:
  - "Copy the skeleton, replace every placeholder and delete the sections that do not apply."
  - "Keep the fixed header of spec.md: Code, Name, Bank id, Source, Folder, Status and the table Versions."
  - "Keep headings unchanged so Sam, Ben and the owner always find the same section."
avoid:
  - "Leaving placeholder text in a delivered file."
  - "Renaming a heading."
  - "Editing the table Versions by hand: the server fills it."
---
## spec.md

```markdown
# {Strategy name}

| Field | Value |
|---|---|
| Code | STR-{NNN} |
| Name | {name} |
| Bank id | {id} |
| Source | {type, reference, citations by locator} |
| Folder | service/strategies/STR-{NNN}-{slug}/ |
| Status | draft |

## Versions
| Version | File | sha256 | Author | Reviewer | Verdict | Date |
|---|---|---|---|---|---|---|

## Market, instrument and timeframe
## Stories
### Story 1 (P1): {title}
Why this priority: ...
Independent test: ...
1. Given {bars}, When {event}, Then {decision}
## Edge cases
## Requirements
- FR-001: The strategy MUST ...
## Parameters
| Name | Default | Min | Max | Origin (source or proposed) |
|---|---|---|---|---|
## Success criteria
- SC-001: ...
## Risks
## Out of scope
## Assumptions
## IPS rules that apply
```

## checklist.md

```markdown
# Checklist: {strategy}
## Clarity
- [ ] CHK001 No open clarification marker
- [ ] CHK002 Every requirement has a case that fails
## Coverage
- [ ] CHK003 Entry, exit, size and risk are all specified
- [ ] CHK004 Out of scope is written
## Engine
- [ ] CHK005 Every order type used is filled by the backtest, or a variant is given
```

## plan.md

```markdown
# Plan: {strategy}
## Summary
## Engine contract used
## Inputs (bar fields and library indicators)
## Parameters
## Inventory (only when new stored data is created)
## Risks and engine limits
## Decisions (link to adr/)
```

## tasks.md

```markdown
# Tasks: {strategy}
- [ ] T001 [P] {what} in {file} (done when: {criterion}) estimate: {machine minutes}
- [ ] T002 {what} in {file} (done when: ...) after T001
```
