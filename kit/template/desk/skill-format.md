# Skill format

Every skill of a desk is one file, `<name>/SKILL.md`, in this format. Desks share it, so a skill written for one desk
loads in any other.

## Frontmatter

```yaml
---
name: earnings-reaction
version: 1.0.0
description: >
  Use when the owner asks how a stock reacted to results. Trigger phrases: "how did X react to earnings",
  "was the move justified", "post earnings drift".
identity: An analyst who reads results against what was expected and says what the market did with them.
patterns:
  - Compare reported figures with the consensus that existed before the release.
  - State the fiscal quarter from the name of the call, never from the calendar date.
avoid:
  - Using any figure published after the cut-off time of the view.
  - Calling a move justified without naming the number that justifies it.
---
```

All six fields are required and no other field is accepted.

| Field | Rule |
|---|---|
| name | Lowercase words joined by hyphens. Equals the folder name. |
| version | Three numbers, for example 1.0.0. Raise it when the content changes. |
| description | Says when to use the skill. Holds the phrases a person would say that should trigger it. |
| identity | One or two sentences: who the skill makes the agent be. |
| patterns | A non-empty list of things to do. |
| avoid | A non-empty list of things never to do. A skill without it is rejected. |

The text after the frontmatter is the body: the procedure, with numbered steps and the tools to call.

## Package manifest

The folder that holds the skills also holds `skills-manifest.json`:

- `schema_version`: the version of this format (now 1).
- `package_version`: the version of the whole package of skills.
- `skills`: for each skill, its version and a digest of its content.

The loader rejects a skill with a missing field, an unknown field, a bad name or a bad version. The manifest check
reports a skill that was added, removed or changed. A change without a version bump is reported as such.
