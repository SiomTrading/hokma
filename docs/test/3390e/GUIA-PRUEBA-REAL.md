# Guide of the real test of the freeze with the data of the broker (SPEC-026 v2, piece 3390e2, task 3390)

For Cristian. The plan P1 to P9 of section 10.1 of the spec already runs in the tests with PROVISIONAL answers (the structure of the documentation with made-up numbers):
`backend/tests/test_freeze_plan_3390e.py`. These are the steps with the REAL accounts. Each step leaves a screenshot and the raw answer of the broker (masked) in
`uploads/test/3390e/` of the project. Do the steps in this order; stop at the first one that does not show what it says.

Rules for the whole session: use the smallest real account of Schwab, one liquid ETF, one share; never a size that hurts. The Trader API has no paper accounts.

## Before: the real captures replace the provisional ones

1. Hokma > Connections > Schwab > menu of the row > "Save an anonymous sample of the account". The file goes to the downloads folder.
2. Copy it as `backend/tests/schwab_fixtures/pnl/accounts_positions.real.json`. For Alpaca the same, as `backend/tests/alpaca_fixtures/pnl/account_positions.real.json`.
3. Run the tests of the P&L (`pytest backend/tests/test_broker_pnl.py backend/tests/test_broker_pnl_alpaca.py`): they pick the real files up by themselves.
4. Send the Message Type of the last ACCT_ACTIVITY that the sample carries to the notes of 4.1.1 of the spec.

## The steps

| # | Account | What you do on screen | What must be seen |
|---|---|---|---|
| P1 | Schwab, the smallest real account | Open the sheet of the account in Hokma | Equity, loss of the day and open P&L of each position, each with the hour of the reading and the source line (`desks.accounts.pnl.source.broker`, Schwab). They match thinkorswim or schwab.com at that moment |
| P2 | Schwab, the same | Rule `daily_loss` with a cap of 1 USD in notice mode. Buy 1 share of a liquid ETF at market with an order of Hokma | When the number moves 1 USD against you: a notice in the chat and in Telegram (the text says account, value of the broker, hour of the reading and cap). The next opening waits with Follow, No and Close. "Close" sends the sale; the position leaves the sheet only when the NEXT reading of Schwab no longer carries it |
| P3 | Schwab, the same | The same in restriction with "block entries"; then try another buy | The account shows "closed for the day"; the new buy is refused `day_loss_reached` with value, hour and cap |
| P4 | Schwab, the same | Send a limit order for 3 shares far from the price and lower it until it fills in part (or use a thin symbol) | A message of ACCT_ACTIVITY arrives; Hokma reads in less than 1 s; the sheet shows the quantity Schwab reports, not the one of the order. Save the real Message Type that arrived |
| P5 | Schwab, the same | Cut the network of the machine of Hokma for 30 s and connect it again | At 15 s: the sheet says there is no data from the broker since HH:MM:SS (the text of `pnl.lost`) and one notice; nothing is closed; a buy in that lapse is blocked (restriction). When it is back: a notice that it came back and the numbers up to date |
| P6 | Hokma simulated, 5000 | Rule `daily_loss` 50 in restriction with "block and flatten"; buy 1 MES with its stop; lower the price of the simulator | When it crosses 50: the day closes and the sale goes out; the sheet confirms the position closed in the next reading |
| P7 | Hokma simulated, 5000 | Send MES with no stop, with a stop above the buy, and with a STOP exit of 1 for an entry of 2 | Three refusals: `stop_required`, `stop_invalid`, `stop_invalid` (quantity) |
| P8 | Hokma simulated, 5000 | Limit order for 3 MES with only 1 at that price in the book of the simulator | The sheet shows 1 contract and its open P&L; the freeze measures what the simulator reports |
| P9 | NinjaTrader Sim101 | Nothing in Hokma until 3391 | If the account is registered it shows the text of `pnl.err.unavailable` (freeze not available: no data from the broker) and a loss rule in restriction cannot be saved |

## Two things that changed in this piece and are worth seeing once

- A rule of ONE desk in an account that several desks use is refused when it is created (text of `live.shared.rejected`: the broker does not split the loss by desk, put the rule on the account). The rule of the whole account is accepted. A desk rule that already existed in a shared account stays, blocks the openings and tells you once to move it (`live.shared.move`).
- If the reads of the broker keep giving way to the orders for more than 60 s after their pause ended, Hokma tells you once (`pnl.yielding`): the data may go stale.
