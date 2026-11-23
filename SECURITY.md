# Security

## Reporting

Report a vulnerability privately to the maintainers through the security advisory form of the repository. Do not open a public issue for it. Expect an acknowledgement within three working days.

## What Hokma protects

- Broker credentials, provider keys and tokens live in the operating system credential store. They are never written to the repository, to logs, to prompts or to tool results.
- Agents receive tools, not secrets. Provider responses are scrubbed of keys, tokens and account ids before they reach a model or a log (`hokma.desks.core.safety`).
- Real-money orders go out only through the order tools, inside the capital and the rules of a desk.
- Agents that read third party content have no write or execute tools.

## Supply chain

- Every dependency is pinned with `==` in `pyproject.toml` and locked in `uv.lock` for the five supported platforms.
- CI builds a software bill of materials (CycloneDX) from the lock on every pull request and uploads it as an artifact.
- A new dependency needs a written reason, its licence, and a lock that resolves on all five platforms.
- Quantitative and data libraries are imported in one file per module (`_lib.py`), and a test fails if any other file imports them.
- Release packages are built from a clean tree by the publishing script.

## Supported versions

Only the latest published version receives security fixes.
