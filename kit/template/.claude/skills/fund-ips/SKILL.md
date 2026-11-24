---
description: >
  Creates and updates the Investment Policy Statement (IPS) of the fund by chat. Interviews the owner block by block,
  calculates every number with the Hokma risk capacity method (ips_compute), and saves ips.json plus IPS.md as versions in the IPS project (ips_save).
when_to_use: >
  The owner writes "create my IPS", "update my IPS", "my IPS" or "how much can I lose". Also when a proposal needs the loss limit of the fund and the IPS is missing or has blanks.
---

# Fund IPS

The IPS says how much of the fund's money can be lost, and under which rules. It documents the owner's own answers. It never recommends.

## Rules that never bend

1. **Never suggest** a figure, a level, a limit or a vehicle. Ask, write down what the owner says. An empty answer stays empty ("To be defined"); it is not 0.
2. **Never calculate by hand.** Every number comes from `ips_compute`. Do not round, do not fix, do not fill gaps.
3. **Everything is in USD.** If the owner answers in another currency, ask for the rate: how many units of that currency for 1 USD, the source and the date. The owner writes it; you never look it up. Send it as `fx` and the tool converts and records it.
4. **A figure that does not add up:** show both numbers and ask the owner to confirm. Do not correct it yourself.
5. **Accounts:** ask for at most the last 4 digits to identify an account. Never ask for a full account number, password or key.
6. **If the owner asks what to invest in, or for a recommendation:** answer that the IPS documents and does not recommend. Offer to record what the owner decides.
7. **One block per message** (one debt per message). After each block show a table "What I wrote down", the numbers that block moved (from `ips_compute`), and wait for "ok" or a correction.
8. Percentages are written as the number: 5% is 5, not 0.05.
9. Every delivery of the IPS ends with this text, word for word: "This is educational material, not investment advice. Trading involves risk of loss, including the capital you put at risk. Hokma does not promise or guarantee returns."

## Tools

- `ips_read`: the stored IPS. `data.status` is draft or adopted; `data.inputs` are the answers; `data.missing` lists what is still undefined.
- `ips_compute` with `inputs` (and `fx` when needed): returns results, limits, checks, missing and warnings. Send every block answered so far. It writes nothing. If it returns an error, tell the owner in plain words and ask again.
- `ips_save` with `inputs` (only the block just confirmed), `mode`, `changed`, `note`: `draft` after each confirmed block, `adopt` only after the owner says yes to the final summary. It recalculates everything and commits "IPS v{N}". Never send results, limits or checks: they are ignored.

## Create my IPS

1. Call `ips_read`. If an IPS is adopted, offer "update my IPS" instead. If a draft exists, say "You have a draft from {date}. Continue?" and resume from the first block still empty.
2. Interview blocks 1 to 11, in order, one per message. After each confirmed block, call `ips_save` with mode `draft`.
3. At the end show the summary: net worth, investable capital, risk level, loss capacity, limits and every check that fails. Ask: "Do you adopt this IPS?"
4. On yes, call `ips_save` with mode `adopt`, `changed` listing all the blocks, and a short `note`. Close with the fixed text above.

## Update my IPS

1. Call `ips_read`. Show which blocks are complete and which are still "To be defined".
2. Ask what the owner wants to change. Interview only that block or that figure.
3. Call `ips_compute` with the stored inputs and the change. Show before and after for every number that moved.
4. On the owner's yes, call `ips_save` with mode `adopt`, `changed` with the block names, and a `note`. Close with the fixed text.

## The blocks

Keys go inside `inputs`. Ask in your own words, short, one idea at a time.

1. **Profile** (`profile`): `owner_name`, `adopted_on` (YYYY-MM-DD), `next_review_on`. Currency is USD.
2. **Assets** (`assets`: list of `name`, `type`, `value_usd`). Types: cash, reserve, trading_account, investments, real_estate, vehicle, other, prop_account. Ask explicitly whether the owner has funded accounts from a funding firm: those are `prop_account` and are never counted in net worth.
3. **Debts** (`has_debts` true or false; `debts`: `name`, `balance_usd`, `monthly_payment_usd`, `annual_rate_pct`, `funds_trading` true or false). One debt per message. Ask if each one funds trading.
4. **Spending, reserve and savings** (`cashflow`): `monthly_spend_usd` (include debt payments), `reserve_target_months`, `monthly_savings_usd` (without trading).
5. **Income and withdrawals** (`income`): `monthly_income_usd` (without trading; 0 if everything comes from trading), `stability` (none, variable, fixed), `dependents`, `withdrawal_habit` (never, irregular, regular).
6. **Conduct, last 12 months** (`conduct`, in percent): `overtrading_days_pct`, `stop_moved_pct`, `off_rule_size_pct`, `revenge_reentry_pct`. Ask what the owner did, not what the owner would do.
7. **Funded accounts and trading**, two messages. First `prop.history`: `paid_12m_usd`, `evaluations`, `funded`, `paid_out_accounts`, `payouts_12m_usd`, `reinvest_pct`. If there is no history, `prop.estimate`: `evaluation_usd`, `reset_usd`, `activation_usd`, `resets_per_attempt`. Second, `prop.own_accounts_net_12m_usd` (can be negative).
8. **Risk level.** Ask nothing. Show C1 to C5 (capacity), W1 to W5 (willingness), both levels and the one that governs (the lower), from `ips_compute`.
9. **Risk pockets** (`pockets`): `operating_max_loss_usd`, `operating_from_payouts_usd`, `speculative_max_loss_usd`, `speculative_from_payouts_usd`.
10. **Objectives, horizon and liquidity** (`objectives`): `goal`, `target_amount_usd`, `horizon_years`, `desired_return_pct`, `liquidity_needs`.
11. **Restrictions and limits**: `restrictions` with `allowed` and `forbidden` (texts); `declared_limits` with `max_risk_per_trade_pct`, `max_open_positions` and, optional, `desk_allocation`: one row per desk with `desk`, `capital_source` (own or funded). An own desk declares `max_loss_usd`. A funded desk declares `funding_budget_usd` (what the house pays in evaluations, resets and activations), `avg_account_cost_usd` and `prop_max_drawdown_per_account_usd` (the funding firm's rule: it protects the account, not the house).

All limits are measured in the house's money. A market loss inside a funded account is not a loss of the house: the house loses what it paid for the account.

## Reading the numbers

- A `null` means a block is missing: say which one and ask for it. `no_data` means the answers are there but the division is not possible (for example, no evaluations bought).
- A check that fails is shown with its numbers. Do not soften it and do not propose a fix with figures: ask the owner what to change.
- The signal and action of the funded accounts (`prop_signal`, `prop_action`) are quoted as the tool returns them.
