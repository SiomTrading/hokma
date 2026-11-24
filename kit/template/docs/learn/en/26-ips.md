# Your IPS by chat

The IPS (Investment Policy Statement) is the page that says how much of the fund's money you can lose and under which rules. You write it once, in the chat, and Hokma keeps it as versions you can read at any time. Hokma documents your own answers. It never recommends what to invest in.

## Create it

1. Open the chat and write: **create my IPS**.
2. Hokma asks one block at a time: profile, assets, debts, spending and reserve, income, conduct, funded accounts, pockets, objectives and restrictions.
3. After each block it shows a table "What I wrote down" and the numbers that block moved. Answer **ok** or tell it what to fix.
4. At the end it shows a summary and asks if you adopt it. Answer **yes** and the IPS becomes version 1.

You should see: an **IPS** project with two files, `IPS.md` (the text) and `ips.json` (the data, with the limits the desks read).

If the session is cut, the blocks you confirmed are kept as a draft. Next time Hokma says "You have a draft" and continues.

## Update it

Write **update my IPS**. Hokma shows which blocks are complete and which are still "To be defined", asks what you want to change, shows the numbers before and after, and saves a new version. Every version is a commit in the IPS project.

## What to know

- Everything is in USD. If you answer in another currency, Hokma asks you for the exchange rate, its source and its date. You write it; Hokma does not look it up.
- Hokma never suggests figures. An answer you leave empty stays "To be defined", which is not the same as 0.
- The numbers always come from the Hokma risk capacity method, never from memory. If a figure does not add up, Hokma shows you both numbers and asks you to confirm.
- Funded accounts from a funding firm are not part of your net worth. A loss inside a funded account is not a loss of the house: the house loses what it paid for the account.
- To identify an account, give only the last 4 digits.

This is educational material, not investment advice. Trading involves risk of loss, including the capital you put at risk. Hokma does not promise or guarantee returns.
