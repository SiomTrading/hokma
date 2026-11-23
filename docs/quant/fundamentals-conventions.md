# Conventions of the fundamentals module (FT-006)

This page is written before any comparison with another source. Every formula that the desks read has its convention here, the value
that `hokma.quant.fundamentals` gives, the value that the ratio library gives and the reason they differ. The function
`fundamentals.read_formulas` returns the same table from the statements of a company, so the page and the numbers cannot drift apart.

## 1. Input

`statements` is a table with one row per fiscal period, oldest first, and one column per line item. The columns are
`revenue, cost_of_revenue, gross_profit, sga_expenses, rd_expenses, operating_income, depreciation_amortization, interest_expense,
interest_income, income_before_tax, income_tax_expense, net_income, preferred_dividends, eps, cash, short_term_investments,
long_term_investments, receivables, inventory, current_assets, ppe, goodwill_intangibles, total_assets, accounts_payable,
short_term_debt, long_term_debt, total_debt, current_liabilities, total_liabilities, total_equity, retained_earnings,
preferred_equity, minority_interest, shares_outstanding, operating_cash_flow, capex, dividends_paid, repurchases, issuance, sbc,
income_taxes_paid, interest_paid, price` (all optional; a function names what it lacks).

| Rule | Meaning |
|---|---|
| Point in time | With a `filed` column and an `as_of` date, every row filed after the date is removed before anything is computed. The latest filing date is what the age gate of the profiles reads. |
| Missing | A line that is not reported is a missing column or NaN, never zero. Only `minority_interest`, `preferred_equity` and `long_term_investments` are optional inside a sum, and the result then carries the warning `assumed_zero:<line>`. |
| Signs | Cash flow lines keep the sign of the statement: `capex`, `dividends_paid` and `repurchases` are negative, `issuance` is positive. Formulas that need a magnitude take the absolute value, so a source that reports them as positive gives the same result. |
| Averages | An average balance is the mean of the closing balance of the period and of the one before. The first period has none and stays empty. |
| Losses | A ratio over earnings does not exist for a loss: P/E, PEG and growth over a base that is not positive are empty, never negative. |
| Bad data | A result with value None and the reason in the warnings. A wrong type or shape is the only error. |

## 2. Formulas that are read

| Formula | Convention of this module | Library convention | Why they differ |
|---|---|---|---|
| Enterprise value (FM-13) | Market capitalization + total debt + minority interest + preferred equity - (cash + short term investments), closing balance | Same sum with cash only | This module also deducts short term investments, which are cash in practice. |
| Buyback yield (FM-14) | (abs(repurchases) - issuance) / market capitalization | -(repurchases + issuance) / market capitalization, repurchases negative | Same value for negative repurchases; this form does not change sign if a source reports repurchases as positive. |
| Shareholder yield | Dividend yield + buyback yield, both over the same market capitalization | Same | None. |
| FCF after SBC (FM-15) | Operating cash flow - abs(capex) - stock based compensation | Free cash flow - stock based compensation | None once the free cash flow is built the same way. |
| ROIC, simplified (FM-16) | NOPAT / (total equity + total debt), closing balance. NOPAT = operating income x (1 - tax rate), tax rate = tax expense / income before tax clipped to [0, 1], zero for a pre tax loss | Same NOPAT over the average of equity plus debt over two periods | The denominator only: closing against average capital. Cash is not deducted in either. |
| P/E | Price / EPS, EPS = (net income - preferred dividends) / shares outstanding; empty for a loss | Price / EPS, negative for a loss | Losses give no P/E here. |
| PEG | P/E / (EPS growth in percent points); below 1 is cheap | The same formula takes percent points; the dividend adjusted form takes a fraction and multiplies by 100 | The library mixes units between two functions; each one is fed the unit it expects. |
| TTM (FM-18) | Four consecutive quarters, periods classified by duration, Q4 = year - nine months, no gaps, window refused when it ends more than 185 days before the cut-off | The routine of SVC-data (ED-026) is called, not copied | None. |

## 3. Scores

| Score | Convention |
|---|---|
| Altman Z | Model by name: `original` (market value of equity over total liabilities, safe above 2.99, distress below 1.81), `private` (book equity, 2.9 and 1.23), `non-manufacturing` and `emerging` (2.6 and 1.1). |
| Beneish M | Eight indexes against the prior period; above -1.78 flags a likely manipulator. The first period has no index and no score. |
| Piotroski F | Nine tests. A period is scored only if every input of it and of the period before exists and the total assets of two periods back exist. The library counts a missing input as a failed test; here that period gets no score. |
| Ohlson O, Zmijewski | Score and probability (logistic and normal). Ohlson needs two consecutive net income figures. |
| Springate, Grover, Fulmer H | Springate below 0.862 fails, Grover at or below -0.02 fails and at or above 0.01 is healthy, Fulmer H below 0 fails. A score whose input is missing is listed as skipped. |

## 4. Criteria of the investor profiles

Each profile function returns `criteria` in the shape of the Signal contract (name, value, threshold, passed, source) and a `lean`
in -1, 0 and +1 that follows its written rules. A required criterion that cannot be computed gives an empty result whose warnings are
`data_missing:<field>`: the profile abstains. The profiles follow published methods of analysis and make no claim of fidelity to any person.

| Profile | Thresholds with a source | Thresholds without a source (parameters printed in the result) |
|---|---|---|
| Quality owner (INV-001) | ROE average 15 percent, ROE floor 10 percent, debt to equity 0.5, margin of safety 25 percent | margin stability, ROE drop, debt rise, price over twice the value |
| Inversion veto (INV-002) | none | all of them; the veto counts only when `veto_enabled` is true |
| Safety margin (INV-003) | P/E 15, P/B 1.5, product 22.5, current ratio 2 (strict) and 1.5 (minimum), long term debt within net current assets. These are the classic defensive figures, cited from memory and fixed in a test | jump in EPS |
| Fair growth (INV-004) | fast 20 percent, stalwart from 10 percent, PEG below 1 adds, above 2 subtracts (1 and 2 add nothing) | the band from 12 to 20 percent is assigned to stalwart and the result says so |
| Inflection (INV-005) | none | acceleration, priced in, recognized move; the default is to wait |
| Contrarian gap (INV-006) | value gap above 30 percent, catalyst with a date on or after the cut-off | bubble ratio |
| Disruption (INV-007) | confidence never above 69 | R&D intensity, runway and dilution thresholds have no default: without them the lean is neutral |

Owner earnings (SHF-004) = net income + depreciation and amortization - abs(capex) - change in (current assets - current liabilities).
The value is owner earnings / (discount rate - growth rate) with 10 and 3 percent by default; the buy-below value keeps the 25 percent
margin. The assumptions are printed in `QuantResult.params`.

The theme of INV-007 comes only from `kit/template/desk/data/themes.csv`. Market size, first mover advantage and the team are declared
unknown and are never estimated.
