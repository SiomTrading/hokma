# Parallel runs

A desk run is a function of a snapshot, so many runs can proceed at the same time as long as they do not share mutable state.

## What runs in parallel

- Analysts of one desk run side by side: each reads the same hashed snapshot and writes its own report into the desk state.
- Several desks run side by side: each desk has its own state, its own capital and its own account (`hokma.desks.core.fund` keeps one live instance per desk and never shares caches between desks).
- Quant calls (metrics, risk, validation) are pure over their inputs and can run in threads or processes.

## What does not run in parallel

- Order placement for one account is serial: the broker layer takes the account lock.
- Writes to the database go through the single writer of `hokma.db`.

## Orchestration options

Hokma schedules runs with its own scheduler (`hokma.scheduler`) and with headless runs (`hokma.desks.core.headless`). Large
backtests and parameter searches split into independent folds (`hokma.quant.validation.split`) and run each fold in a worker
process. Distributed task frameworks are not part of the base install: they are a later addition with their own review.

## Rules for any parallel code

1. Inputs are immutable values or hashed snapshots.
2. A worker returns a `QuantResult` or a typed report; it never writes to shared files.
3. The seed of every random component is part of the call parameters and of the result, so a rerun repeats.
4. A run that fails reports the failure; the others continue.
