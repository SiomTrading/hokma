# Team of development agents

How the people who maintain Hokma split work among agents. Each desk of the owner arranges its own team with the same pattern.

| Agent | Responsibility |
|---|---|
| Engineering manager | Turns a request into a spec with rows, interfaces and acceptance tests |
| Developer | Implements one slice of a spec on a feature branch, with tests, and opens a pull request |
| QA | Runs the suite, checks every acceptance test against the spec and issues a written verdict |
| Deploy | Publishes only a build that has a QA verdict |
| Data engineer | Owns data modules, fixtures and ingestion checks |
| Researcher | Reads papers and documentation and writes the evidence a spec cites |

## Working agreements

- One agent, one responsibility. Two tasks are two agents.
- A developer agent never merges, never publishes and never edits files outside its slice.
- A spec lists every piece with its target path and test. The developer records the real path of each piece when it is done.
- QA reads the code, not the report: each acceptance test is run, and a piece without a test is rejected.
- Agents that read third party content (mail, web pages) have no write or execute tools.

## Hand over

A developer closes a slice with: files created, pieces done out of pieces assigned, pieces not done with the exact reason, the test command and its totals, and every decision that changes an interface.
