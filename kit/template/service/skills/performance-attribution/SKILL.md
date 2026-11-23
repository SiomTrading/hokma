---
name: performance-attribution
version: 1.0.0
description: >
  Use when the owner asks why a portfolio beat or trailed its benchmark: by sector, by factor, over several periods, or for fixed income. Trigger phrases: "attribution", "why did we beat the index", "allocation versus selection", "factor attribution", "link the periods".
identity: "A performance analyst who splits the active return into effects that add up exactly and links periods correctly."
patterns:
  - "Use Brinson-Fachler by sector: allocation, selection and interaction, which add up exactly to the active return, the portfolio return minus the benchmark return."
  - "Link several periods with Carino scaling; never add the effects of different periods."
  - "Handle the case where the accumulated returns are equal with the limit of the factor, not a division by zero."
  - "For factors, use active exposures (portfolio minus benchmark): the factors explain the portfolio return minus the benchmark return, not the portfolio return minus the risk free rate."
  - "For fixed income, scale the income effect by the time of the period."
avoid:
  - "Comparing the lines of Hokma with each other: they do not share a benchmark."
  - "Attribution of options."
  - "Summing single period effects without linking."
  - "Typing any number: the code computes every effect."
---
## Procedure

1. Check the inputs: weights add up to one on each side, and sectors are the same on both sides.
2. Compute the single period effects by sector.
3. If there are several periods, link them. Report the linked effects and the residual, which is zero up to the precision of the logarithm.
4. For factors, give the active exposure, the factor return and the contribution of each factor, and the specific part left over.
5. For fixed income, give income, duration, spread and selection.
6. Present the table: effects by sector and the total, which equals the active return.
