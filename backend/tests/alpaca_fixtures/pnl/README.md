# P&L fixtures of Alpaca (SPEC-026 v2, piece 3390e1b)

`account_positions.provisional.json` is NOT a capture. It has the exact structure of the Alpaca documentation (`GET /v2/account`, `GET /v2/positions`) with the numbers of its
examples, plus one short position made up for the tests. Every test that reads it is marked provisional.

## Replacing it with the real capture (a paper account works)

1. In Hokma: Connections > Alpaca Trading > menu of the row > "Save an anonymous sample of the account". The file goes to the downloads folder.
2. Copy that file here as `account_positions.real.json`. The tests pick it up by themselves (the file as the tool wrote it: `account` and `positions`) and run the same
   checks of mapping on it: `day_pnl == equity - last_equity`, the day and open P&L of each position, no number out of the book.
3. Open points of the Alpaca reader stay [ASSUMED] until a real capture answers them: whether `equity - last_equity` already includes the deposits and withdrawals of the
   day (`intraday_adjustments`), and what `last_equity` is on an account that has not closed a day.
