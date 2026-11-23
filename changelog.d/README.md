# Changelog fragments

Each pull request adds one file here: `<number or slug>.<type>.md` where type is `new`, `changed`, `fixed` or `removed`.
The file holds one or two sentences in English that say what the owner of the fund can now do or what changed, in the present tense.

At release time, `python scripts/changelog_merge.py <version>` moves the fragments into a new section of `CHANGELOG.md` and deletes them.
`python scripts/changelog_merge.py --check` fails when a fragment has a wrong name or is empty.
