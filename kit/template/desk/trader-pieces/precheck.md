---
name: precheck
lines: [SWO]
summary: The check that runs before any proposal
---
# Pre-trade check

Before you propose anything, read the check the code ran on the account. It stops any new risk, whatever the size, when:

- the desk is not active, orders are switched off or there is no trading account;
- the drawdown from the peak of the book reached its limit;
- the equity is at or under the capital floor;
- a funded account lost its daily allowance or reached its trailing drawdown;
- a limit is set and the fact it needs is unknown. Unknown is not zero and it is not fine: it stops the order.

If the check reports a blocker, stand aside and name it. The filter of the execution door runs the same rules again when the order is sent, and it has the last word.
