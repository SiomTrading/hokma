# Derivative pricing in SVC-options

This note is the recipe of the options module (`hokma.quant.options`): which model prices which style of option, with which
inputs, and how far its answer can be trusted. It is written for whoever reads a result of an options function or adds one.
Every function is registered as `options.<name>` and is called through `quant_call`; nothing here is called by an agent
any other way.

## Conventions

| Item | Rule |
|---|---|
| Rates, dividend yield, volatility | Decimals. 4 percent is 0.04 and a volatility of 25 percent is 0.25. A volatility above 20 is refused as a unit mistake. |
| Time | Years. From a date it is the minutes to 16:00 New York of the expiration date over 525,600. |
| Prices and greeks | Per share. Dollars appear only where a quantity and the contract multiplier are given. |
| Contract multiplier | Always read from the contract sheet and passed in. No function has a default, and a missing multiplier gives an empty result with the reason. |
| Bad data | An empty, NaN or out-of-domain input returns a result with `value = None` and the reason in `warnings`. A wrong type (a string where a number belongs) is a bad input error. |
| Size | `n_steps` is capped at 5,000, `n_simulations` at 100,000, `time_steps` at 1,000, `n_strikes` at 2,000. A value above a cap is refused, never clipped. |

## Which model for which style

| Style or need | Model | Function | Notes |
|---|---|---|---|
| European call or put on a stock or index | Black-Scholes with a continuous dividend yield | `black_scholes`, `d1_d2` | Intrinsic value at expiry. The reference for every other price. |
| American option on a stock (all US equity options) | Binomial tree, Leisen-Reimer by default for accuracy | `binomial_tree` | American exercise by backward induction, the exercise boundary and the early exercise premium are returned. Other parametrizations: Cox-Ross-Rubinstein, Jarrow-Rudd and Tian. |
| American option, fast screen | Bjerksund-Stensland 2002 | `bjerksund_stensland` | A lower bound of the exact American price that may understate it by about 1 percent. Use the tree when the premium decides the trade. |
| Stock paying cash dividends before expiry | Escrowed dividend model in the tree | `binomial_tree` with `dividends` | The tree runs on spot minus the present value of the dividends. Exercise values add back the dividends still to come. |
| Assignment risk of a short call before an ex-dividend date | Necessary condition and extrinsic value test | `early_assignment_risk` | The dividend must exceed strike * (1 - exp(-rate * years to the next payment)): necessary, not sufficient. In practice the call is at risk when its extrinsic value is below the dividend. |
| Structure with no closed form | Monte Carlo under geometric Brownian motion | `monte_carlo` | Seeded, with the standard error. |
| Currency option | Garman-Kohlhagen | `garman_kohlhagen` | With the foreign rate equal to the risk-free rate it imitates Black-76 on a forward equal to spot. The module has no explicit Black-76, and the limitation is returned in the warnings. |
| Binary, barrier and Asian options | Closed forms | `binary`, `barrier_asian` | Registered for the module only. No agent of the stock options desk asks for them and no structure uses them. |
| Implied volatility | Inversion by root finding | `implied_volatility` | `model='black_scholes'` inverts the European formula; `model='american'` inverts the Bjerksund-Stensland price, the right choice for equity puts and dividend payers. A price outside the no-arbitrage bounds returns None. |
| Smile of one expiry | Raw SVI fit | `svi` | At least 5 points. Returns the parameters, the fit error and the static arbitrage check. |
| Surface | One SVI smile per expiry plus a calendar check | `surface`, `svi_calendar` | The calendar check runs over the log-moneyness the quotes cover, because a fitted smile is not reliable far outside it. |
| Probabilities implied by the market | Second derivative of the SVI call price | `risk_neutral_density` | Returns mass, mean, standard deviation, the probability below spot and below given levels. Negative density is reported as arbitrage. |

Bond pricing belongs to the fixed income line and is not part of this module.

## Greeks

First order greeks, gamma, vanna, the second order greeks and the third order greeks are closed forms of the Black-Scholes
model. The units are part of the result: vega per volatility point, theta per calendar day, rho per 1.00 of rate, charm per
year. A position is aggregated in dollars by `position_greeks` with the multiplier of the contract sheet, and it revalues the
whole position for spot and volatility shocks instead of extrapolating the greeks.

## What is checked

* A price from a known volatility inverts back to that volatility.
* Put-call parity is exact for European prices; for American options the function checks the no-arbitrage bounds instead.
* A knock-in plus the knock-out with the same barrier equals the plain option.
* A tree converges to Black-Scholes, and the convergence table can be produced instead of quoted.
* A flat smile gives a lognormal density.
