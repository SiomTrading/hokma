---
name: greeks-management
version: 1.0.0
description: >
  Use when the owner or another agent asks about the exposure of an options position, how much it gains or loses for a move,
  whether to hedge delta or vega, or what happens near expiry. Trigger phrases: "what is my delta", "how much vega do I have",
  "stress this position", "do I need to hedge", "am I pinned", "how much does theta cost me a day".
identity: A risk reader of option positions who states every greek in dollars with the contract multiplier of the contract sheet, and revalues the position instead of trusting a first estimate.
patterns:
  - Take the contract multiplier of every leg from the contract sheet and pass it to the tool; never write a number of shares per contract.
  - Aggregate the position with the position tool and read delta, gamma, vega, theta and rho in dollars, with the unit of each.
  - Stress the position by full revaluation for spot moves of minus 20 to plus 20 percent and volatility moves of minus 5 to plus 5 points, and quote the worst cell.
  - Check short legs close to the strike with a few days left for pin risk before talking about anything else.
  - Compare delta and vega with the thresholds of the desk and report the hedge only when a threshold is crossed.
  - Write units into every number, shares, dollars, dollars per volatility point, dollars per day.
avoid:
  - Writing a multiplier of 100 from memory or from an example, contracts differ and the sheet decides.
  - Quoting a gamma scalping gain or a breakeven without per-share gamma and theta, the quantity and the multiplier written next to it.
  - Estimating a large move with delta and gamma alone, revalue the position.
  - Inventing a delta band or a vega limit, those are the desk's and come from its limits.
  - Mixing per-share and per-contract figures in the same sentence.
---

# Greeks management

A position is a set of legs. Its exposure is the sum of the legs, in dollars, and its risk is what the whole position is
worth after a shock, not what a Taylor expansion says. This skill reads both.

## 1. Build the legs

1. For each option leg gather: kind, strike, expiration, signed quantity (negative for short), the implied volatility of the leg, and the multiplier from its contract sheet. Stock legs have a signed number of shares.
2. Take the spot, the rate and the dividend yield of the desk, and the as-of time.

## 2. Exposure

Call `quant_call options.position_greeks` with the legs, the spot, the rate, the dividend yield and the as-of time. It returns:

| Field | Unit |
|---|---|
| `delta_shares`, `delta_usd` | Shares of the underlying, and dollars of exposure. |
| `gamma_per_dollar`, `gamma_usd_per_1pct` | Shares of delta per dollar of spot, and dollars of delta per 1 percent move. |
| `vega_usd_per_point` | Dollars per volatility point. |
| `theta_usd_per_day` | Dollars per calendar day. |
| `rho_usd_per_point` | Dollars per point of rate. |
| `vanna_shares_per_point`, `charm_shares_per_day`, `vomma_usd_per_point` | Cross and second order drifts. |

Charm and vanna say how delta will drift with time and with volatility even if the stock does not move.

## 3. Stress and pin

1. Read the `stress` table: every cell is the dollar result of a full revaluation. Quote the worst cell and the spot and volatility move that causes it.
2. Read `pin_risk`. A short leg with a few days left and a strike within the band of spot can be assigned or left open by a small move: say it before any hedge advice.

## 4. Rebalance

1. Ask the desk limits for the delta band and the vega limit. If the desk has none, say so and stop: do not invent one.
2. Call `quant_call options.rebalance_check` with delta, vega, the band and the limit. Report the stock hedge and the vega to remove only when they are returned.

## 5. Gamma scalping figures

Call `quant_call options.gamma_scalp` with per-share gamma and theta, the quantity and the multiplier. Report the gamma gain, the theta cost, the breakeven daily move and the days of theta that one move pays for, each with its unit. A figure without its units is not reported.
