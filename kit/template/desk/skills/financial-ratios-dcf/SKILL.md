---
name: financial-ratios-dcf
version: 1.0.0
description: >
  Use before rating a stock, when the owner or another agent asks for the ratios of a company, a fair value, or a discounted cash
  flow. Trigger phrases: "what are the ratios", "is the DCF sound", "what is it worth", "run a DCF", "check the valuation",
  "are the margins healthy".
identity: A fundamentals analyst who asks the ratio module for every figure, and refuses a valuation whose own inputs are not reasonable.
patterns:
  - Ask the fundamentals module for each ratio group (profitability, liquidity, solvency, efficiency, valuation, growth) from the statements public at the cut-off, and report the period each figure belongs to.
  - Ask for the discount rate and the cost of equity from the module, with the inputs it used, instead of choosing a rate.
  - Before any discounted cash flow, check the inputs for reason, then call the intrinsic value function, and report the check next to the value.
  - Reject a model whose terminal growth is at or above the discount rate, the value it would give has no meaning.
  - Say what share of the value sits in the terminal period and flag the model when that share dominates.
  - Ask for a second reading of the value (a different method, or a different growth path) and report both next to the market price taken from the snapshot.
  - Look at the sign and the trend of free cash flow, and at debt against the cash that pays it, before rating a stock.
avoid:
  - Computing a ratio, a rate or a value yourself, every figure comes from the module.
  - Using a terminal growth rate at or above the discount rate, or above the long run growth of the economy the company sells into without a stated reason.
  - Reporting a single point value with no range and no second method.
  - Mixing a trailing figure with a forward figure in one ratio, or periods that are not the same fiscal period.
  - Using budget variance or a forecast of the company own plan, they are not part of this skill.
  - Writing a figure that no tool returned.
---

# Financial ratios and discounted cash flow

This skill turns the statements of a company into ratios and a fair value range, and refuses to rate a stock on a model
that is not sound. The numbers come from the fundamentals module; this skill decides which to ask for and when to say no.

## 1. Ratios

1. Read the statements public at the cut-off with the fundamentals tool: income, balance and cash flow, annual and quarterly.
2. Ask the module for the ratio groups: `fundamentals.profitability`, `fundamentals.liquidity`, `fundamentals.solvency`, `fundamentals.efficiency`, `fundamentals.valuation`, `fundamentals.growth`. Add `fundamentals.dupont` when the return on equity needs explaining.
3. Report each ratio with its fiscal period. A ratio the module could not compute is reported as unavailable, not as zero.

## 2. Rate and cash flow

1. Ask for `fundamentals.wacc` or `fundamentals.cost_of_equity` with the inputs they need. Report the inputs.
2. Take the cash flow base from the module (`fundamentals.ttm`, `fundamentals.owner_earnings`), not from a headline.

## 3. Check before you value

A model is rejected, with the reason, when:

- terminal growth is at or above the discount rate;
- terminal growth is above the long run growth of the economy the company sells into, and no reason is written;
- the cash flow base is negative and the model assumes it turns positive with no dated evidence;
- the share of the value in the terminal period dominates, which makes the answer a statement about the terminal rate.

## 4. Value

1. Call `fundamentals.intrinsic` with the method and the checked inputs. The function returns no value when the rate does not exceed the growth rate; treat that as a rejection.
2. Ask for a second reading with another method or another growth path.
3. Put the range next to the price in the snapshot and say which side of the range the price is on.

## 5. Report

1. Ratios by group with periods.
2. The rate, its inputs and the checks that passed or failed.
3. The value range, the terminal share and the price from the snapshot.
4. What you could not compute, as data gaps.
