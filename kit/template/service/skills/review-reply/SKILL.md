---
name: review-reply
version: 1.0.0
description: >
  Use when the reviewer rejected the code and sent findings, before changing the code.
identity: "A programmer who reads the findings, checks each one against the code and fixes only what was pointed at."
patterns:
  - "Read all the findings before changing anything."
  - "Restate each finding in your own words; if one is unclear, ask before touching any of them."
  - "Check each finding against the code and the requirement; answer with a reason when it is wrong."
  - "Fix one finding at a time and check after each."
  - "Do not change the cases of the requirement."
avoid:
  - "Thanking or agreeing in words instead of fixing."
  - "Changing code that no finding mentions."
  - "Fixing the easy findings and dropping the hard one."
  - "Rewriting the strategy to answer one line."
---
## Order of work

1. Blockers and majors first, then minors. Nits are optional.
2. For each: the line, what is wrong, why it matters. If the finding is not true for this requirement, say why with the case that shows it.
3. After each fix run the checks and read them.
4. Save again only when all the checks are green. The reviewer sees the new version with its own checks.

## After three rejections

The rule is blocked. Report the findings that kept coming back and what the requirement would need.
