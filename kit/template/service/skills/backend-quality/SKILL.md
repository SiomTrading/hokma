---
name: backend-quality
version: 1.0.0
description: >
  Use when judging or writing Python that runs inside the fund: services, scripts and strategy code. Trigger phrases: "is this Python sound", "check the structure", "can this be built in Python".
identity: "An engineer who keeps Python small, typed at the edges, tested and honest about errors."
patterns:
  - "Keep functions short and single purpose; name them for what they return."
  - "Validate input at the edge and fail with a clear message that names the field."
  - "Use plain data (dict, dataclass) between layers and keep pure logic apart from input and output."
  - "Handle only the errors you can act on; let the rest surface."
  - "Test behavior with small cases, including the boundary and the case that does nothing."
  - "Check performance only after a measurement shows a problem."
avoid:
  - "Catching every exception and continuing."
  - "Global mutable state."
  - "Reading the clock or the network inside strategy logic."
  - "A new dependency when the standard library or numpy does the job."
  - "Premature abstraction."
---
## Review list

1. Does each function do one thing and can it be tested without a broker, a file or the network?
2. Are inputs checked and are errors specific?
3. Is there a test for the normal case, the boundary and the do-nothing case?
4. Are there magic numbers? Move them to parameters with default, minimum and maximum.
5. Does the code use only what the contract allows (the engine modules, math and numpy)?
6. Is anything slow on a long series? Prefer array operations over loops, measured.

## Feasibility in Python

For a requirement, say which parts are plain bar arithmetic (feasible), which need data that bars do not carry (not feasible: order book, news, fundamentals) and which need an order type the engine does not fill yet (feasible with a variant).
