---
name: option-greeks
ref: rec-022
rows: [FT-102, FT-020, FT-026, FT-027]
lines: [SWO, LP]
summary: Price an option and read its greeks across strikes and expirations through the options module, check put call parity, and recover the implied volatility from a market price.
runnable: true
calls: [hokma.quant.options.pricing, hokma.quant.options.greeks, hokma.quant.options.iv, hokma.quant.options.trees]
---
# Option prices and greeks

## Idea

A theoretical price needs five numbers (price of the underlying, strike, time to expiration, volatility, interest rate) and a dividend yield. From the price
come the greeks, the sensitivities that a desk lives by: delta (to the underlying), gamma (to delta itself), theta (to time), vega (to volatility) and rho (to rates).
Plot each against the strike and against the days left and the shape of the risk of a position is clear: gamma and theta peak near the strike and near expiration, delta
runs from zero to one for a call. The same tools turn the price of a real option into its implied volatility.

## Steps

1. Price: `options.black_scholes` for European options, `options.bjerksund_stensland` and the binomial tree (`options.binomial_tree`, with the early exercise boundary) for American
   ones. Options on stocks are American, so use the tree or the approximation for the final number. The time is `time_to_expiration`, in years.
2. Check the inputs with `options.put_call_parity`: a call, a put and the stock must agree within the cost of carry. A violation is a bad quote or a bad input, not an opportunity.
   Exact parity holds only for European options: for an American one read `within_american_bounds`.
3. Greeks: `options.first_order` (delta, vega, theta, rho and the others), `options.gamma`, `options.second_order` and `options.third_order`. Say the unit of each one: theta per day or
   per year, vega per point of volatility or per unit.
4. Implied volatility: `options.implied_volatility` inverts the price. The chain and the quotes come from the data module with their time of reception; a stale quote gives a false volatility.
5. A position is the sum of its legs: use `options.position_greeks` for the book greeks, never the greeks of one leg times a guess.
6. The market closed or the chain empty is an empty answer with a reason, never a zero.

## Example

One option, half a year to expiration, at the money, with the numbers written in the block. Nothing is read from the market.

```python runnable
import math

from hokma.quant import registry

legs = dict(spot=100.0, strike=100.0, rate=0.05, time_to_expiration=0.5, dividend_yield=0.01)
call = registry.call("options.black_scholes", volatility=0.2, kind="call", **legs).value
put = registry.call("options.black_scholes", volatility=0.2, kind="put", **legs).value
delta_call = registry.call("options.first_order", volatility=0.2, kind="call", **legs).value["delta"]
delta_put = registry.call("options.first_order", volatility=0.2, kind="put", **legs).value["delta"]
parity = registry.call("options.put_call_parity", call_price=call, put_price=put, **legs).value
rebuilt = registry.call("options.implied_volatility", market_price=call, kind="call", **legs).value
american = registry.call("options.binomial_tree", volatility=0.2, kind="put", **legs).value

# Put call parity: call - put = S exp(-qT) - K exp(-rT) = 100 exp(-0.005) - 100 exp(-0.025) = 1.97026.
assert abs((call - put) - (100 * math.exp(-0.005) - 100 * math.exp(-0.025))) < 1e-9 and parity["within_tolerance"]
# The two deltas differ by exp(-qT) = 0.995012, and a call delta is between 0 and 1.
assert 0 < delta_call < 1 and -1 < delta_put < 0 and abs((delta_call - delta_put) - math.exp(-0.005)) < 1e-9
# The price turned back into a volatility gives the volatility that made it.
assert abs(rebuilt - 0.2) < 1e-9
# An American put is worth at least the European one: the early exercise premium is not negative.
assert american["price"] >= put and american["early_exercise_premium"] >= 0
OK = True
```

## Checks

- A call delta is between 0 and 1, a put delta between -1 and 0, and the call delta minus the put delta of the same strike is exp(-qT) (about one with no dividends). Check the module output against these.
- Gamma is the same for a call and a put of the same strike. If it is not, the inputs differ.
- The volatility used for a price must be the one of the same expiration and strike; one number for the whole chain is wrong.
- A chain that comes from the market is an input to be refreshed with its time of reception, never a stored truth.
