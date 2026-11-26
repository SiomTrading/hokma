---
name: ml-signal
ref: rec-018
rows: [AS-014, B-37, V-28, ECT-069]
lines: [DT, SWC, SWO, LP]
summary: A learned signal done safely, with labels that cannot leak, features that look only backward and a model that is fit again on every window and scored only on the window after it.
runnable: true
calls: []
---
# A model signal without leakage

## Idea

A model is a very flexible way to fit the past, which is exactly why it fools its author. Three habits keep it honest. The label looks at the future on
purpose, so it is a target and never a feature, and it is measured from the open of the next bar, the first price a decision at the close can really
get. The features look only backward and the module checks that they do. And the model is never fit once: it is fit again on every training window and
scored only on the window after it, with a gap (a purge and an embargo) so that no training row shares a price with a test row.

## Steps

1. Label: `hokma.factory.labels.forward_return(open, horizon)` (or `direction_label`). Row t is the return from the open of t+1 to the open of t+1+horizon.
2. Features: `hokma.factory.features` (`returns`, `rolling_zscore`, `fractional_diff`, `amihud`). Each one at row t uses rows up to t only; `features.build` checks it.
3. Guard: `labels.check_features(features, labels)` raises when a label (or a name of one) is in the features.
4. Walk forward: `hokma.backtest.walkforward.refit` fits on a train view and scores on a test view. The purge is at least the horizon plus one bar; the embargo
   adds a margin after each test block. A fit that reaches for a test row raises `LeakageError`.
5. Report the out of sample score of every window, not only the mean. A model that wins in one window and loses in the rest is one episode.
6. Turn the model output into an order through a strategy (see signal-plus-exit); a model never talks to a broker.

## Example

A linear model (least squares from `hokma.quant.validation.stat_tests`) on two features of the real daily NQ bars, fit again on four rolling windows. The score is the
share of test days where the sign of the prediction matches the sign of the label. The sample is seventy bars: the code is the template, not a finding.

```python runnable
import numpy as np

from hokma.backtest import walkforward
from hokma.factory import features, labels
from hokma.quant.validation import stat_tests

close = np.array([float(b.close) for b in BARS_DAILY])
open_ = np.array([float(b.open) for b in BARS_DAILY])

label = labels.forward_return(open_, 1)                                           # open[t+2] / open[t+1] - 1
table = {"ret": features.returns(close), "z": features.rolling_zscore(close, 5)}
labels.check_features(table, {"fwd_return": label})                              # the features hold no label
try:
    labels.check_features({"fwd_return": label.target()}, {"fwd_return": label})
    raise AssertionError("a label among the features must be refused")
except labels.LeakError as error:
    assert error.code == "label_leak"
data = {**table, "y": label.target()}


def design(view):
    feats, y = np.column_stack([view.get("ret"), view.get("z")]), view.get("y")
    ok = np.isfinite(feats).all(axis=1) & np.isfinite(y)
    return feats[ok], y[ok]


def fit(view):
    feats, y = design(view)
    return stat_tests.ols(y, feats)["beta"]


def score(beta, view):
    feats, y = design(view)
    return float(np.mean(np.sign(beta[0] + feats @ beta[1:]) == np.sign(y)))


result = walkforward.refit(data, fit=fit, score=score, scheme="rolling", n_folds=4, purge=2, embargo=1)
assert result.leakage_free and len(result.windows) >= 3
assert all(0.0 <= w.score <= 1.0 for w in result.windows) and 0.0 <= result.mean_score <= 1.0
print([round(w.score, 2) for w in result.windows], "mean", round(result.mean_score, 3))
OK = True
```

## Checks

- The label for the last two rows is empty: no future exists for them, and they are never used.
- The purge is at least the horizon plus one. With a horizon of 5 bars and a purge of 2, a training row shares prices with the test window and the score is inflated.
- Compare the model with a rule you can write in one line (the sign of yesterday's return). A model that does not beat it is not worth keeping.
- Feature selection happens inside the training window too (`features.select_in_train`); choosing features on the whole sample is leakage.
- Costs enter only when the signal becomes orders. A sign hit rate before costs is not a result.
