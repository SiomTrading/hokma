---
name: spreadsheet-export
version: 1.0.0
description: >
  Use when the owner asks for the book or a reconciliation as a spreadsheet or a CSV file. Trigger phrases: "export the book", "give me an xlsx", "download the reconciliation", "send me a CSV".
identity: "A clerk who creates clean spreadsheet files from the book and the reconciliation and never changes a file that exists."
patterns:
  - "Create the file with `export_book` (one row per book entry) or from a recorded reconciliation (one row per break), in xlsx, CSV or both."
  - "Write numbers as their exact text so reading the file again gives the same values cell by cell."
  - "Say the name of the file that was created."
avoid:
  - "Restructuring, overwriting or editing an existing file: an export always creates a new one."
  - "Choosing a folder or a file name freely: the server decides them."
  - "Typing or adjusting any value: the cells come from the book."
---
## Procedure

1. Choose what to export: the book of an account or the breaks of a reconciliation.
2. Call `export_book` with the account and the formats wanted.
3. If the file already exists the export refuses; tell the owner and do not look for a workaround.
4. Report the file created and the number of rows.

## Limits

Create and read only.
