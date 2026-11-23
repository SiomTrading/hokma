# Reproducibility

A Hokma result can be repeated. This page lists where a rerun can differ and what Hokma does about each source.

| Source of variation | What Hokma does |
|---|---|
| Data changes after the fact | Every data call is point in time: it takes `as_of`, returns `received_at` and the source, and the snapshot is hashed (`hokma.desks.core.hashing`). The same hash means the same numbers. |
| Random components | Every random function takes a seed. The seed is in the call parameters and in the result, so the same call returns the same numbers. |
| Library versions | Dependencies are pinned with `==` in `pyproject.toml` and locked in `uv.lock` for the five supported platforms. |
| Model output | A model reasons over numbers already computed by the quant and data modules. Its text can vary between runs; the numbers behind it do not. A signal stores the snapshot hash it was built from. |
| Time of day | Timestamps are UTC. A run states its cut-off (`as_of`) and uses nothing later. |
| Provider outages | A missing value is reported as `NO_DATA`, `DATA_UNAVAILABLE` or `STALE`. A gap is never filled. |

## How to repeat a run

1. Take the snapshot hash and `as_of` stored with the decision.
2. Run the same registered function with the same parameters: `hokma quant run <name> --file <returns.csv>`.
3. Compare `inputs_hash` and `value` in the result. Equal hash and equal value mean the run is reproduced.
