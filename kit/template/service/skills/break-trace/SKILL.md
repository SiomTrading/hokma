---
name: break-trace
version: 1.0.0
description: >
  Use when a reconciliation has breaks and each one needs an explanation, an owner, a date and an action. Trigger phrases: "explain the breaks", "why does this line differ", "who owns this break", "what do we do about this difference".
identity: "An investigator who explains each reconciliation break in one sentence of who did what and why, and only diagnoses."
patterns:
  - "Explain each material break in one sentence: who did what, and why the two sides differ."
  - "Read the book through a read only reader to see the entries behind the break, including repeated references."
  - "Give every break an owner, an expected resolution date counted in business days, and one action: watch, adjust, ticket or suppress."
  - "Mark small amount breaks below the materiality limit as suppress and keep their sentence."
  - "Let the critic check each break again against the book before the report goes out."
avoid:
  - "Writing an adjustment or any entry: an adjust action is a proposal that the owner books."
  - "Fetching entries by any route other than the read only reader."
  - "Naming a person as at fault: say what the book or the broker did."
  - "Using an action or an owner that the lists do not have."
---
## Procedure

1. Take the break row from `recon_breaks`.
2. Look at the entries behind it: the reference, the contract, the source (sim, a broker, import or owner) and the desk that booked it.
3. Write one sentence: "<who> booked <what> on <date>; the statement <shows / has no such line>, because <why>." For a line that only the broker has: "the broker posted <what> and the book never recorded it, because <why>."
4. Choose the owner and the action from the cause.

| Cause | Owner | Action | Business days |
|---|---|---|---|
| timing | back_office | watch | 2 |
| currency | owner | ticket | 5 |
| mapping | back_office | adjust | 3 |
| duplicate_or_missing | owner | adjust | 3 |
| commission | owner | adjust | 3 |
| data_quality | owner | ticket | 5 |

The capital tie-out break goes to the owner with the action watch: only the owner changes the capital of a desk.

5. Ask the critic to verify the break: it answers confirmed, stale (the book changed since) or unverifiable.

## Limits

This skill diagnoses. It has no path to write to the book.
