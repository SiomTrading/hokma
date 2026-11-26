# P&L fixtures of Schwab (SPEC-026 v2, piece 3390e1)

`accounts_positions.provisional.json` is NOT a capture. It has the exact structure of the guide (`uploads/spec/schwab-trader-api-documentacion.md`, sections 3.1 and B.3) with
numbers made up for the tests: one margin account and one cash account. Every test that reads it is marked provisional.

## Replacing it with the real capture

1. In Hokma: Connections > Schwab > menu of the row > "Save an anonymous sample of the account". The file goes to the downloads folder.
2. Copy that file here as `accounts_positions.real.json`. The tests pick it up by themselves (they accept the file as the tool wrote it or only its
   `accounts_fields_positions` list) and run the same checks of mapping on it: `day_pnl == equity_now - equity_start`, the open P&L of each position, no
   number out of the book. The `account_activity_last` of the file is the first real ACCT_ACTIVITY message: add its Message Type to the notes of 4.1.1.
3. Open points 1 to 5 of 4.1.1 stay [ASSUMED] until a real capture answers them in writing.
