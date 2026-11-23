---
name: catchup
description: Resume from the last checkpoint.
---

# catchup

Resume from the last checkpoint.

1. Read the newest file in `checkpoints/`.
2. Check that every file it names still exists and that the open items are still open.
3. Re-run any computation whose inputs changed since the note and say which results moved.
4. Reply with three lines: where things stand, what changed since the note, the next step.

This is real money work: never write a number that is not backed by a function call or a source, and never place an order from a command.
