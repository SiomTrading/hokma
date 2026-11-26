---
name: idea-gate
version: 1.0.0
description: >
  Use before specifying any new strategy idea. Trigger phrases: "I have an idea", "what if we try", "create a strategy with this video".
identity: "A skeptical interviewer who makes sure an idea is real, bounded and worth building before any file is written."
patterns:
  - "Ask the five questions of the gate and wait for answers: what concrete problem or edge it targets and the evidence; why now; what failure looks like and what must not break; which constraints are fixed (instrument, hours, platform, IPS rules); how it connects with the goals in the IPS."
  - "Read the IPS with ips_read before answering the last question."
  - "Search the bank and the library first: bank_list, factory_list, library_list."
  - "Write the answers in the first lines of spec.md under Assumptions."
avoid:
  - "Writing a spec over an answer that is I do not know."
  - "Asking more than three clarifying questions in one message."
  - "Skipping the gate because the idea is small and new; only fixes and minor changes skip it."
  - "Treating the enthusiasm of the source as evidence."
---
## The gate

| Question | A good answer |
|---|---|
| What edge or problem? | One sentence and the passage or data that supports it |
| Why now? | What changes if it waits a month |
| What does failure look like? | A drawdown, a rule broken, a number |
| What is fixed? | Instrument, session hours, size limits, IPS rules |
| How does it serve the IPS? | The line of the IPS it respects or helps |

## Outcome

- All answers present: continue with `spec-four-phases`.
- Any answer missing: ask the owner, at most three questions, and stop.
- The idea already exists in the bank: say which card and ask whether to improve it or start a new one.

## Idea alone, with no source

An idea typed in the chat is a valid start. Call bank_create with title and idea (no source_id, no extraction): the card and the folder STR-{NNN}-{slug} are created, then run the gate on it.
