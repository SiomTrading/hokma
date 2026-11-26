---
name: bond-statistics
ref: rec-025
rows: [FT-102, FT-096]
lines: [RF]
summary: Describe a bond from its price, coupon and maturity with present value, yield, duration and convexity, and read the curve, spreads and central bank rates around it.
runnable: false
calls: [hokma.data.macro, hokma.quant.risk.fixed_income]
---
# Bond statistics

## Idea

A bond is a schedule of cash flows. The price is their present value at the yield, and everything else follows from the same schedule: the yield is the rate that makes the
present value equal to the price, the duration is the average time to the cash flows (the modified form is the price sensitivity to the yield), the convexity is the curvature that duration
misses. A par value of 1000 with a 5 percent coupon paid twice a year and five years to maturity, discounted at an 8 percent yield, is worth about 878.33: it trades at a discount because its coupon is
below the yield. Around the bond sit the curve (yields by maturity), the spreads (corporate over treasury, option adjusted) and the policy rates of the central banks.

## Steps

1. Cash flows: par value, coupon rate, payments per year, years to maturity. Say the day count and the settlement date.
2. Present value from the yield; yield from the price. Check by pricing again: the yield must return the price to the cent.
3. Duration (Macaulay and modified) and convexity from the same cash flows. The price change for a move of the yield is about minus the modified duration times the move, plus half the convexity times its square.
4. The curve and the rates (`hokma.data.macro.fixed_income`): government yields by maturity, the effective rates of the central banks, benchmark overnight rates. The slope (the long yield
   minus the short one) is the standard summary. The kinds `duration`, `yield_to_maturity` and `par_yield` describe bonds as they are today: for a past date the module answers
   `NO_DATA` and says why.
5. Spreads and the portfolio view (`hokma.quant.risk.fixed_income`): the spread against a treasury of the same maturity and the duration of a portfolio of bonds, weighted by value.

## Example

The call shape. The data module needs a provider key and the risk module is built by another block of the plan (H3), so this block only has to parse here:

```python illustrative
from datetime import datetime, timezone

from hokma.data import macro
from hokma.quant import registry

as_of = datetime(2025, 11, 10, tzinfo=timezone.utc)
curve = macro.fixed_income("government_bond_yield", as_of=as_of)
# Bond statistics are functions of hokma.quant.risk.fixed_income; read the inputs with registry.describe before the call.
stats = registry.call("risk.fixed_income_portfolio", positions=positions, yields=curve.value)
```

## Checks

- Price falls when the yield rises, always. A function that disagrees has the sign wrong.
- The 878.33 example above is the test for any bond calculator: five years, 5 percent coupon twice a year, 8 percent yield, 1000 of par.
- Duration is a first order measure; for large moves of the yield add convexity.
- A callable bond has a yield to worst (the lowest yield over its possible calls), not only a yield to maturity.
