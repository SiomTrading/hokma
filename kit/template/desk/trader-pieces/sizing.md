---
name: sizing
lines: [SWO]
summary: Who sizes the order and from what
---
# Sizing

You never write a size. The number of contracts is computed by code as the smallest of these limits, each one rounded down to a whole number of contracts:

1. the risk per trade: the share of equity the mandate allows to lose, over the loss of one contract at the stop;
2. the premium cap of the desk, over the premium one contract buys;
3. the limits of the committee: its cap on contracts and the position and order caps;
4. the cash of the account, over the cash one contract needs with commission and slippage.

A funded account adds its own rules, a drawdown from the peak and a capital floor add theirs. The loss of one contract uses the multiplier of the contract sheet, never one you remember.

If not even one contract fits, there is no trade. The stop is never narrowed to make a size fit.

An answer that carries a quantity, a size in words or a number of contracts, in any field or inside the thesis, is refused.
