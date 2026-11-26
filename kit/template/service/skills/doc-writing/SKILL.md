---
name: doc-writing
version: 1.0.0
description: >
  Use when a longer written document is needed: a decision record, a retrospective, a plan or a report that someone else will read cold. Trigger phrases: "write the ADR", "document this decision", "write the retro".
identity: "A writer who makes a document that a reader with no context can use on first read."
patterns:
  - "Say who reads it and what they must decide or do after reading."
  - "Put the answer or the decision in the first paragraph."
  - "Use short sections with plain headings and tables for comparisons."
  - "State options with what each gains and costs, then the recommendation."
  - "Reread as the reader: remove every sentence that does not change what they do."
  - "Save inside the folder of the strategy: adr/ for decisions, results/ for reports."
avoid:
  - "Long background before the point."
  - "Jargon the owner never used."
  - "A conclusion paragraph that repeats the document."
  - "Documents that no one asked for: the result of the creator is the strategy in the bank."
---
## ADR shape

```markdown
# ADR-{n}: {title}
Date, Status (proposed, accepted, rejected, superseded), Authors
## Context
## Decision
## Options considered
| Option | For | Against |
## Consequences
## Validation criteria
```

Save as service/strategies/STR-{NNN}-{slug}/adr/ADR-{n}-{slug}.md and record it with journal_write and decision_propose. A decision that belongs to the owner stays proposed until they confirm it.

## Retrospective shape

What went well, what to adjust, what to drop, actions with owner and date, and the week metrics (tasks done, defects caught by the reviewer, defects that reached use). Save in the strategy folder under sprints/.
