---
name: volatility-richness
version: 1.0.0
description: >
  Use before buying or selling option premium, when the owner or another agent asks whether implied volatility is high or low
  for an expiry. Trigger phrases: "is implied volatility rich", "is premium cheap here", "implied against realized",
  "how steep is the skew", "should I buy or sell vol on this name".
identity: An options analyst who compares what the market charges for volatility with what the stock actually did, and refuses to call it rich or cheap without a calibrated threshold.
patterns:
  - Match the expiry first, take the implied volatility at the forward of the expiry the structure uses, never a blend of expiries.
  - Compare it with close to close realized volatility over 20, 60 and 90 days and show the three ratios side by side.
  - Read the 25 delta risk reversal (call minus put volatility) as the skew and the butterfly (average wing minus at-the-money) as the curvature, and say which way each leans.
  - Report the classification exactly as the tool returns it; without a calibration from a backtest the answer is "uncalibrated" and that is the finding.
  - Say whether an earnings date falls before the expiry, and treat the comparison as contaminated when it does.
avoid:
  - Calling volatility rich or cheap, or naming a regime (low, normal, elevated, crisis), without a threshold calibrated in a backtest and its reference.
  - Comparing an implied volatility that includes an earnings jump with a realized volatility that does not, and drawing a conclusion from the gap.
  - Using a realized volatility from fewer closes than its window, or from closes that are not daily.
  - Reading a risk reversal or butterfly when the quoted strikes do not reach the 25 delta wings, report it as unavailable.
  - Writing any volatility from memory instead of the tool.
---

# Volatility richness

Premium is only worth buying or selling relative to what the stock is likely to deliver. This skill puts the market's
price of volatility next to the volatility the stock has realized, and describes the shape of the smile. It does not
decide to trade: a threshold decides, and the threshold comes from a backtest.

## 1. Gather the inputs

1. Read the chain of the expiry with the chain tool (`quant_call options.option_chain`, or the chain the data tool returned). Note the source and whether it is delayed.
2. Read the daily closes of the underlying (at least 91 closes for the 90 day window) with the bars tool, oldest first.
3. Read the calendar for the next earnings date of the company.
4. Take the rate and the dividend yield the desk uses, and the as-of time of the chain.

## 2. Compute

Call `quant_call options.vol_richness` with the chain rows, spot, rate, dividend yield, as-of time, the expiration, the closes and the earnings date. If the stats module already produced realized volatilities, pass them as `realized_vol` instead of the closes. Pass `calibration` only when the desk holds one from a backtest, with its reference.

## 3. Read the answer

| Field | What it says |
|---|---|
| `atm_iv` | Implied volatility at the forward of the matched expiry. |
| `realized_vol` and `iv_over_realized` | Realized volatility over 20, 60 and 90 days and the implied over each. A window with too few closes is empty and the warning says so. |
| `risk_reversal_25d` | Call minus put volatility at 25 delta. Negative means puts cost more: downside is priced. |
| `butterfly_25d` | Average of the two wings minus at-the-money. Positive means the wings are expensive against the center. |
| `earnings_in_window` | True when an earnings date falls before the expiry. |
| `classification` | `uncalibrated` unless a complete calibration was given. Never rich or cheap by itself. |

## 4. Report

1. State the matched expiry, the implied volatility, the three realized figures and the three ratios.
2. State the skew and the curvature with their signs, or say they are unavailable.
3. State the classification and its reason. When it is "uncalibrated" say that the desk has no calibrated threshold yet and that the figures describe, they do not recommend.
4. When the earnings flag is true, say the implied volatility holds the earnings jump and that the realized figures do not, so the gap is not evidence of richness.
5. Hand the figures to the options structures skill; do not choose the structure here.
