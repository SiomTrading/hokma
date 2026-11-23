# Portfolio recipes (SK-119)

Recipes of the portfolio module, one per type of optimization. Each recipe is a short sequence of registered functions
(`portfolio.*`, called by `quant_call`) with the inputs that matter and the check that tells you the result can be trusted. They are
written from the functions of this module, not copied from anywhere. The desk that uses a recipe takes its own limits and its own
objective; the recipe does not change.

Common rules of every recipe:

- Inputs are tables with dates as rows and assets as columns. A row with a missing value is dropped and the result says so in
  `warnings`. A table that ends up empty gives a result with value `None` and the reason, never an exception.
- A solver that does not converge returns `status: "failed"`, the reason, `weights: None` and a labelled baseline under
  `fallback`. The baseline is not an answer to the question; it is the number to compare against.
- Every candidate is compared with the baselines of recipe 1 before it is trusted: a candidate that does not beat inverse volatility
  or equal weights on the held-out period is not used.

## 1. Baselines

Use: the bar every other recipe must clear.

1. `portfolio.baselines` with `kind = "inverse_volatility"` and `kind = "equal_weighted"`.
2. `portfolio.baselines` with `kind = "random"` and a fixed `seed`, or `portfolio.rand_weights_dirichlet`, many times, to see how
   a random portfolio of the same assets behaves.
3. `portfolio.population` with the candidate and the baselines as `candidates`, ranked by `sharpe_ratio` (or the measure the desk
   uses). `dominance` lists the pairs where one portfolio is better in every measure.

Check: weights sum to one; the same `seed` gives the same weights.

## 2. Minimum risk with limits

Use: capital split between option structures or between assets when the desk wants the least risk for a given budget.

1. `portfolio.objectives` lists the objectives, the 19 risk measures and the solvers that are installed.
2. `portfolio.mean_risk` with `objective = "min_risk"`, the `risk_measure` of the desk, `min_weights` and `max_weights` (one number or
   one per asset) and `budget = 1`.
3. `portfolio.contribution` on the result: the share of each asset in the risk. A share far above the weight means that asset drives
   the portfolio.

Check: `risk` of the result is not above the risk of the baselines; no weight outside its bounds.

## 3. Return or utility with practical constraints

Use: when the desk has views on return and needs the costs of trading to count.

1. `portfolio.mean_risk_constrained` with `objective = "max_utility"` and `risk_aversion`, or `"max_return"` with `max_risk`
   (for example `{"cvar": 0.03}`).
2. Add `previous_weights` with `transaction_costs` and `max_turnover` to limit rotation; `management_fees` for holding costs;
   `threshold_long` for a minimum position; `cardinality` for the number of assets (needs a solver that handles integers, for example
   with a linear risk measure such as `mean_absolute_deviation`).
3. If the result is `failed`, loosen one constraint at a time. The reason names the constraint that made it infeasible.

Check: the realized turnover against `previous_weights` is below `max_turnover`.

## 4. Tail risk that survives model error

Use: strategies with fat tails, such as long option structures, where the history under-states the worst case.

1. `portfolio.robust_cvar` with `cvar_beta` (0.95 by default) and `wasserstein_ball_radius` (how far from the history the true
   distribution may be).
2. `portfolio.uncertainty_set` with `target = "mu"` and the weights of the result: `worst_case_return_loss` is the return that
   can disappear if the mean is wrong by as much as the data allow.
3. Compare with `portfolio.mean_risk` using `risk_measure = "cvar"` on the same data: the robust result should hold less in the
   assets whose tail is thin only in the sample.

Check: widening the radius moves the weights towards the more diversified portfolio, never away from it.

## 5. Views and scenarios

Use: an analyst or the trader has an opinion that must be mixed with the market, not replace it.

1. `portfolio.black_litterman` with `views` such as `{"assets": {"AAA": 1, "BBB": -1}, "value": 0.001}` and a confidence per
   view. Confidence 1 makes the posterior mean equal the view; lower values keep it near the history.
2. `portfolio.entropy_pooling` for views that are inequalities or about variance, for example `{"assets": {"AAA": 1}, "relation": ">=",
   "value": 0.001}`. `relative_entropy` is the price of the views: the larger it is, the further the views are from what happened.
3. Feed `mu` and `covariance` to recipe 2 or 3 through the same assets.

Check: with no view that binds, `relative_entropy` is zero; `effective_number_of_scenarios` far below the number of rows means a
few days carry the whole view.

## 6. Covariance from implied volatility

Use: the desk trades options and the market's volatility is a better forecast than the realized one.

1. `portfolio.implied_covariance` with the returns and a table of implied volatilities with the same dates and assets.
2. `portfolio.variance` with `method = "ew"` and a half life as the realized alternative.
3. Compare the volatilities of both: a large gap is the volatility risk premium the desk is paying or earning.

Check: the correlation in the result is the realized correlation of the whole history; only the volatilities come from the
implied ones, forecast from a regression over the last `window_size` rows.

## 7. Factors

Use: explain returns and risk with a few common drivers.

1. `portfolio.time_series_factor_model` when the factors are return series: `regressor = "ols"` first, `"ridge"` if the factors are
   close to each other.
2. `portfolio.characteristics_factor_model` when the factors are built from stock characteristics (value, momentum, quality): the
   `factors` list names the descriptors of each factor. After earnings, a surprise descriptor plus momentum gives the factors to
   choose stocks.
3. `portfolio.factor_model` builds the same container from parts you already have (loadings, premia, covariance) and returns the
   implied mean and covariance, so a stress can change one number and see the effect.

Check: `implied_covariance` is positive semidefinite; its diagonal is not below the idiosyncratic variances.

## 8. Alpha

Use: a forecast of return per asset from descriptors, before any weights.

1. `portfolio.alpha_fixed_weighted` when the desk knows how much each descriptor counts (signed `weights`, a `forecast_scale`).
2. `portfolio.alpha_ew_sharpe_optimal` when the weights should be estimated from past idiosyncratic returns (needs the fields
   `idio_returns` and `idio_variances`).
3. `portfolio.alpha_predictor` when a regression (`ridge`, `linear` or `lasso`) should learn the map from descriptors to return.
4. `portfolio.alpha_evaluation` with the specs of the candidates as `estimators`: IC (Spearman and Pearson), the return of a
   long-short portfolio built from the forecast and the calibration slope. With two or more estimators it also returns the comparison.
5. `portfolio.cs_spearman_correlation` and `portfolio.cs_pearson_correlation` give the IC per date for any forecast table.

Check: the mean IC is positive across dates and its hit rate is above one half; `signal_lag` is at least 1 so the forecast never sees the
return it is judged on.

## 9. Selection before optimizing

Use: too many assets, or assets that do not belong.

1. `portfolio.drop_zero_variance` removes flat series.
2. `portfolio.select` with `method = "non_dominated"` or `"k_extremes"` to shrink the universe; `method = "non_expiring"` with
   `expirations`, `lookahead_days` and `as_of` to keep only contracts that are alive and not about to expire.

Check: `kept` plus `dropped` is the whole universe; a contract that expires inside the look-ahead window is in `dropped`.

## 10. Mixing the views of a desk

Use: five or more profiles give a view on the same company and one number is needed.

1. Collect the `Signal` objects of the profiles for one symbol.
2. `portfolio.blend_signals` with the signals and the history of past views (`desk_signals`): abstentions are excluded and profiles
   whose past views have a correlation above 0.8 are merged and count as one.
3. Read `warnings` and `groups`: they say who was excluded and who was merged.

Check: if every source abstained the result is empty with that reason; a blend never has a value outside [-1, 1].
