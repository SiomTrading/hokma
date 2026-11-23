---
name: assumption-stress-test
version: 1.0.0
description: >
  Use when the owner or another agent wants to know what breaks if a belief behind a trade or a desk is wrong, or how the book
  behaves in a severe market. Trigger phrases: "what if the edge disappears", "stress this assumption", "what if volatility stays high",
  "what is the worst case", "test the thesis", "what would early warning look like".
identity: A risk reader who isolates one assumption at a time, hunts for the evidence against it and measures the damage when it fails, with the risk module and not by feel.
patterns:
  - Name the assumption in one sentence and say which result of the desk depends on it. Examples are that an edge persists in high volatility, that two positions are uncorrelated, or that a short strike will not be reached.
  - Isolate it. Change that assumption alone and keep every other input as the snapshot gives it.
  - Look for the evidence against it before looking for support. Read the data of the snapshot and of the tools for the periods and the conditions in which it failed, and say how many observations there are.
  - Model the failure as a fall of 30, 50 and 80 percent in the exposed asset, with the stress call of the risk module, and report the loss of the book in dollars and as a share of equity for each.
  - Run a sensitivity on the one input that carries the assumption, for example the correlation shift or the volatility multiplier, and report the point at which the loss passes the desk limit.
  - Write the early warning as an observable figure and the threshold at which it would be read, taken from the desk limits or marked as a proposal for the owner.
  - Say what would have to be true for the assumption to hold, and how long the desk has been observing it.
avoid:
  - Stressing several assumptions at once. The result then says nothing about any of them.
  - Accepting an assumption because it has held recently. A recent run is not a test.
  - Writing a probability, a historical frequency or a loss figure that no tool returned.
  - Inventing a desk limit or a warning threshold and presenting it as the desk's.
  - Treating a stress result as a forecast. It is a measure of exposure.
  - Using the language of a company, such as revenue, hiring or runway. The subject is the book and its positions.
---

# Assumption stress test

Every position leans on beliefs. This skill takes one belief, tries to break it and says how much the book loses if it breaks.

## 1. Isolate the assumption

1. Write it as a sentence that can be false: "the edge persists when volatility is high".
2. List the positions and results that depend on it, from the back office snapshot and the reports of the desk.
3. If more than one assumption is in play, test them in separate passes.

## 2. Look for the counter evidence

1. Read what the tools return for the conditions in which the assumption would fail: high volatility, a gap, a break in correlation.
2. Count the observations. A handful of cases is a handful of cases, say so.
3. State plainly any case where the assumption failed before.

## 3. Model the failure

1. Call `quant_call risk.stress` with a scenario of shocks to the exposed asset of minus 30, minus 50 and minus 80 percent, one call per size.
2. For an options position add `quant_call options.position_greeks` and read the revaluation stress table.
3. Report the loss in dollars and as a share of equity for each size, and whether it passes the loss limit of the desk.

## 4. Sensitivity

1. Pick the single input that carries the assumption.
2. Vary it over a range with the stress call, for example the volatility multiplier or the correlation shift.
3. Report the value at which the loss passes the limit of the desk. That value is the margin the assumption has.

## 5. Early warning

1. Write one observable figure that would move first if the assumption were failing.
2. Say where it is read from and how often.
3. Give the threshold only if the desk limits hold one. Otherwise label it a proposal for the owner.

## 6. Report

1. The assumption, the positions that rest on it and how long it has been observed.
2. The evidence against it.
3. The loss table for the three sizes.
4. The margin from the sensitivity and the early warning.
5. Data gaps. Hand the figures to the arbiter; do not decide the order.
