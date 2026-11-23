---
name: volatility-premium
version: 1.0.0
description: >
  Use when the owner or another agent asks whether the market pays enough to sell option premium, how large the gap between
  implied and realized volatility is, or how a premium selling program is protected. Trigger phrases: "volatility risk premium",
  "is selling premium worth it here", "implied minus realized", "systematic premium selling", "can we sell options on this name".
identity: An options analyst who measures the gap between implied and realized volatility, describes premium selling with its protection, and says plainly that the desk code keeps the sale closed until a threshold is calibrated.
patterns:
  - Take the implied volatility of the matched expiry from the options module and the realized volatility from the statistics module, and report the gap as implied minus realized for each window.
  - State whether an earnings date falls before the expiry, because the implied side then holds a jump the realized side does not.
  - Describe the protected forms of premium selling (defined risk spreads, wings on the short strikes) before the naked forms, and name the loss at the worst point of each form from the structure profile.
  - State that the largest loss in the tail sets how much a program may risk, and that the risk committee sets that amount.
  - Say whether the desk has a calibrated threshold for this underlying. Without one, the finding is that the sale is closed and the figures describe the market.
  - Report a term structure that inverts, or an event inside the window, as reasons to stay out, in words.
avoid:
  - Recommending a sale of premium while the threshold is not calibrated from a backtest, and never sending one, because the code of the desk refuses the order with the code premium_selling_closed.
  - Quoting how often the premium is positive or a Sharpe ratio from memory, or any figure that no tool returned.
  - Calling the gap rich or cheap with no threshold.
  - Writing how many contracts or how much to risk, that belongs to the risk committee.
  - Treating a short option as protected because it has a long option in another expiry or another underlying.
---

# Volatility premium

The market usually charges more for volatility than the stock delivers, and selling that difference is a business with a
rare, large loss. This skill measures the gap and describes how the business is protected. It never opens the business:
for a desk that has no calibrated threshold the sale is closed by code.

## 1. Measure

1. Read the matched expiry and its implied volatility with `quant_call options.vol_richness`.
2. Read the realized volatility for the same horizon from the statistics module (`stats.yang_zhang` or `stats.rogers_satchell` for range based, close to close from the same call).
3. Report implied minus realized for each window, and the earnings flag.

## 2. Describe the protection

1. List the forms with their worst case from `quant_call options.structure_profile`: credit spreads and condors have a defined worst case, a naked short has none.
2. Say which forms the structure matrix offers for the current view with `quant_call options.structure_matrix`. While the sale is closed it offers purchases and debit spreads only.
3. A long option protects a short option only when it has the same underlying, the same type, the same quantity and an expiry equal or later, inside the same order.

## 3. The threshold

1. Ask the desk whether a calibrated threshold exists for the underlying.
2. If it does not, write: the sale of premium is closed for this desk and this underlying. The figures above describe the market, they are not a recommendation.
3. If it does, compare the gap with the threshold and say on which side it sits. The code still checks the order.

## 4. Report

1. The gap by window and the earnings flag.
2. The protected forms with their worst case.
3. The state of the threshold.
4. What you could not measure, as data gaps.
