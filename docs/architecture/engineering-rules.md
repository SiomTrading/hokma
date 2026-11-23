# Engineering rules

Rules for everyone who changes Hokma: the owner's agents and the people who maintain the code.

## Credentials stay in the Hokma process

The Hokma server is the only process that holds broker credentials, tokens and provider keys. They live in the operating
system credential store (see `hokma.core` secrets helpers) and are read at the moment of a call. Agents get tools, never
secrets: a tool result, a prompt, a log line and a test fixture never contain a key, a token or a broker account id. The
sealing and scrubbing helpers in `hokma.desks.core.sealed` and `hokma.desks.core.safety` enforce this, and each has a test
with sentinel values.

## Verification ladder

The minimum evidence a change must bring, by kind of change:

| Change | Evidence |
|---|---|
| Text, docs, kit rules | `python scripts/check_repo_rules.py` is green |
| Pure function | A unit test with a value computed by hand |
| Quant or data function | The hand value, the same value from the underlying library through the module's `_lib.py`, and the empty, constant and NaN cases |
| Registered quant function | It appears in `hokma.quant.registry.list()` with its piece ids |
| Agent tool | A call through the MCP layer with fixture data, and one classified error |
| Database change | A migration test that runs on SQLite and on Postgres |
| Dashboard change | `npm run build` and the committed `dashboard/dist` matches |
| Dependency change | `uv lock` resolves for the five required environments and `uv sync` installs |

A change is not done at the first row it satisfies: it is done when every row that applies is green.

## Repository records

A defect that is found and not fixed in the same change is recorded in the issue tracker with a reproducing case. It is not
left as a `TODO` or a commented block in the code. Release notes go to `CHANGELOG.md` through a fragment in `changelog.d/`.

## One canonical instruction file

The base instructions for agents live in `kit/template/HOKMA.md`. `kit/template/CLAUDE.md` imports it and adds only what the
Claude Code engine needs. Other engine files are generated from the same source, so a rule is written once.

## Branches and delivery

Work happens on a feature branch and reaches `main` through a pull request that has a green CI and a QA verdict. A release is
built from `main` by the publishing script; nobody pushes a release by hand and nobody force pushes `main`.

## Code rules

- Configuration is validated when the program starts: a malformed value fails with a clear message instead of a default.
- Money, prices and quantities that reach an order use `decimal` or integer ticks, never binary floats.
- A library is imported in one file per module (`_lib.py`); the rest of Hokma calls the module.
- A function that cannot compute a result returns a value of `None` with a warning that says why. It does not guess and it does not fill a gap.
- Timestamps are UTC. Every data result carries its source and the time it was received.
