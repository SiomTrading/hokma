"""Walk forward, refit by window and the evidence for the judge (SPEC-020, section 4.2: V-24, B-37 and `evidence`).

The partitions are the ones of `hokma.quant.validation.cv.split` (purge and embargo included): there is no second splitter here. A walk forward
picks, in each window, the combination that did best on the TRAINING rows alone and measures it on the test rows that follow; the out of sample
curve is the tests laid one after another. The backtest does not approve anything: `evidence` gathers what the judge needs, with the number of
trials counted by the ledger.

`walk_forward(spec, space, ...)` and `evidence(strategy_id, version)` are the public entry points: they load the strategy, the bars and the saved
rows themselves. `run`, `refit` and `assemble` work on the matrices and results that they are given."""
from __future__ import annotations

import dataclasses
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd

from hokma.backtest import costs as backtest_costs
from hokma.backtest import results
from hokma.backtest.optimize import Ledger, ParamSpace, DbLedger, resolve_ledger, run_one, sweep_points
from hokma.backtest.placebo import MonteCarloResult
from hokma.backtest.results import BacktestResult, EquityCurve
from hokma.backtest.spec import SpecError
from hokma.quant import limits, metrics
from hokma.quant.validation import cv
from hokma.quant.validation.evidence import EvidenceBundle, bundle

SCHEMES = {"rolling": "walk_forward", "anchored": "expanding", "cpcv": "cpcv"}
OBJECTIVES = ("net_pnl",)
PERIODS_PER_YEAR = 252
MAX_SERIES = 64


class WalkForwardError(ValueError):
    """The walk forward cannot run as asked. `code` is stable: bad_walkforward, leakage."""

    code = "bad_walkforward"


class LeakageError(WalkForwardError):
    """A training step touched a row of the test window, or a partition has a train row inside a purge or embargo zone."""

    code = "leakage"


def windows(n: int, *, scheme: Literal["rolling", "anchored", "cpcv"], n_folds: int, purge: int, embargo: int, n_test_folds: int | None = None) -> list[cv.Fold]:
    """The train and test partitions of `n` rows, from `quant.validation.split` (SK-034, SK-035), checked for leakage before they are used.
    rolling: a train window followed by a test window of the same size, moved forward; anchored: the train window grows from the first row;
    cpcv: every combination of `n_test_folds` groups is a test set (the train rows near a test block are purged and embargoed)."""
    if scheme not in SCHEMES:
        raise WalkForwardError(f"bad_walkforward: scheme must be one of {', '.join(SCHEMES)}")
    n_folds = limits.as_int("n_folds", n_folds)
    limits.check_param("n_folds", n_folds)
    limits.check_param("n_observations", n)
    folds = cv.split(n, scheme=SCHEMES[scheme], n_folds=n_folds, purge=purge, embargo=embargo, n_test_folds=n_test_folds)
    problems = cv.leakage_violations(folds, purge=purge, embargo=embargo)
    if problems:
        raise LeakageError(f"leakage: {problems[0]}")
    return folds


# --------------------------------------------------------------------------------------------------------------
# V-24: optimize in the sample, evaluate outside, window by window
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Window:
    index: int
    train: tuple[int, int]                         # first row and one past the last row of the train rows
    train_rows: int
    test_blocks: tuple[tuple[int, int], ...]       # the contiguous blocks of test rows (one for rolling and anchored)
    column: int                                    # the combination picked with the train rows alone: its position in `points`
    chosen: Mapping[str, Any]
    in_sample: float                               # its objective on the train rows
    out_of_sample: float                           # its objective on the test rows


@dataclass(frozen=True)
class WalkForwardResult:
    scheme: str
    objective: str
    n_folds: int
    purge: int
    embargo: int
    windows: tuple[Window, ...]
    oos_rows: tuple[int, ...] | None               # rolling and anchored: the test rows in order; cpcv: None (its tests overlap)
    oos_pnl: tuple[float, ...] | None              # the out of sample curve: the pnl of each window's choice on its test rows, tests laid end to end
    oos_total: float | None
    efficiency: float | None                       # mean out of sample pnl per row over mean in sample pnl per row of the chosen combinations
    leakage_free: bool
    warnings: tuple[str, ...]
    run_id: int | None = None                      # the row of backtest_runs that `walk_forward` wrote (None for a result that was not saved)

    def to_dict(self) -> dict[str, Any]:
        return {"scheme": self.scheme, "objective": self.objective, "n_folds": self.n_folds, "purge": self.purge, "embargo": self.embargo,
                "windows": [{"index": w.index, "train": list(w.train), "train_rows": w.train_rows, "test_blocks": [list(b) for b in w.test_blocks],
                             "chosen": dict(w.chosen), "in_sample": w.in_sample, "out_of_sample": w.out_of_sample} for w in self.windows],
                "oos_total": self.oos_total, "efficiency": self.efficiency, "leakage_free": self.leakage_free, "warnings": list(self.warnings),
                "run_id": self.run_id}


def run(points: Sequence[Mapping[str, Any]], pnl: np.ndarray, *, scheme: Literal["rolling", "anchored", "cpcv"], n_folds: int, purge: int, embargo: int,
        objective: str = "net_pnl", n_test_folds: int | None = None) -> WalkForwardResult:
    """Optimize on each train window and evaluate outside it (V-24). `pnl` has one row per bar and one column per combination of `points`
    (the per bar result of each one: `sweep.sweep_signals(..., per_bar=True).pnl`, or the increments of engine equity curves). The choice of a window
    reads only its train rows: the pnl of the test rows never enters it. The efficiency is the mean out of sample result per row over the mean in
    sample result per row of the choices; it is reported, and a threshold on it belongs to the judge."""
    if objective not in OBJECTIVES:
        raise WalkForwardError(f"bad_walkforward: the objective is one of {', '.join(OBJECTIVES)}")
    data = np.asarray(pnl, dtype=float)
    if data.ndim != 2 or not len(points) or data.shape[1] != len(points):
        raise WalkForwardError(f"bad_walkforward: the pnl is a matrix of bars by combinations ({len(points)} combinations), got shape {data.shape}")
    if not np.isfinite(data).all():
        raise WalkForwardError("bad_walkforward: the pnl has a value that is not finite")
    limits.check_param("n_combinations", len(points))
    folds = windows(len(data), scheme=scheme, n_folds=n_folds, purge=purge, embargo=embargo, n_test_folds=n_test_folds)
    out: list[Window] = []
    for i, fold in enumerate(folds):
        train_scores = data[fold.train].sum(axis=0)
        pick = int(np.argmax(train_scores))                                   # the first one on a tie
        out.append(Window(i, (int(fold.train.min()), int(fold.train.max()) + 1), len(fold.train), fold.test_blocks, pick, dict(points[pick]),
                          float(train_scores[pick]), float(data[fold.test, pick].sum())))
    rows = oos = total = None
    if scheme != "cpcv":
        rows = tuple(int(r) for fold in folds for r in fold.test)
        oos = tuple(float(v) for fold, w in zip(folds, out) for v in data[fold.test, w.column])
        total = float(sum(oos))
    in_mean = float(np.mean([w.in_sample / w.train_rows for w in out]))
    out_mean = float(np.mean([w.out_of_sample / sum(b - a for a, b in w.test_blocks) for w in out]))
    efficiency = out_mean / in_mean if in_mean > 0 else None
    return WalkForwardResult(scheme, objective, limits.as_int("n_folds", n_folds), purge, embargo, tuple(out), rows, oos, total, efficiency, True,
                             () if efficiency is not None else ("in_sample_not_positive",))


# --------------------------------------------------------------------------------------------------------------
# B-37: a model that is fit again on each window
# --------------------------------------------------------------------------------------------------------------
class View:
    """The rows of one window that a step is allowed to see: the arrays are cut to those rows, read only, and `row` refuses any other position."""

    def __init__(self, role: Literal["train", "test"], data: Mapping[str, np.ndarray], positions: np.ndarray) -> None:
        self.role = role
        self._data = data
        self._positions = np.asarray(positions)
        self._positions.setflags(write=False)

    @property
    def positions(self) -> np.ndarray:
        return self._positions

    def get(self, name: str) -> np.ndarray:
        """The named series restricted to this window, as a read only copy."""
        if name not in self._data:
            raise WalkForwardError(f"bad_walkforward: there is no series named {name!r}")
        out = np.array(self._data[name][self._positions])
        out.setflags(write=False)
        return out

    def row(self, position: int) -> dict[str, Any]:
        """The values of every series at one row. A row outside this window is a leak: LeakageError, and the run stops."""
        if position not in set(self._positions.tolist()):
            raise LeakageError(f"leakage: row {position} is not a {self.role} row of this window")
        return {name: values[position] for name, values in self._data.items()}


@dataclass(frozen=True)
class RefitWindow:
    index: int
    train_rows: int
    test_blocks: tuple[tuple[int, int], ...]
    score: float


@dataclass(frozen=True)
class RefitResult:
    scheme: str
    windows: tuple[RefitWindow, ...]
    mean_score: float
    leakage_free: bool


def refit(data: Mapping[str, Sequence[float]], *, fit: Callable[[View], Any], score: Callable[[Any, View], float],
          scheme: Literal["rolling", "anchored", "cpcv"] = "rolling", n_folds: int, purge: int, embargo: int, n_test_folds: int | None = None) -> RefitResult:
    """Fit a model again on each window and score it on the window after it (B-37). `fit` receives only a train `View` and returns the model;
    `score(model, view)` receives only a test `View`. A fit that reaches for a test row (`view.row(...)`) raises LeakageError, and the
    partitions themselves are checked for leakage first."""
    if not data or len(data) > MAX_SERIES:
        raise WalkForwardError(f"bad_walkforward: the data is between 1 and {MAX_SERIES} series")
    arrays = {str(k): np.asarray(v) for k, v in data.items()}
    sizes = {len(v) for v in arrays.values()}
    if len(sizes) != 1:
        raise WalkForwardError("bad_walkforward: every series has the same number of rows")
    folds = windows(sizes.pop(), scheme=scheme, n_folds=n_folds, purge=purge, embargo=embargo, n_test_folds=n_test_folds)
    done = []
    for i, fold in enumerate(folds):
        model = fit(View("train", arrays, fold.train))
        done.append(RefitWindow(i, len(fold.train), fold.test_blocks, float(score(model, View("test", arrays, fold.test)))))
    return RefitResult(scheme, tuple(done), float(np.mean([w.score for w in done])), True)


# --------------------------------------------------------------------------------------------------------------
# The evidence for the judge
# --------------------------------------------------------------------------------------------------------------
def returns_matrix(pnl: np.ndarray, points: Sequence[Mapping[str, Any]], *, capital_usd: float, index: Any = None) -> pd.DataFrame:
    """The per bar pnl of each combination as returns of the capital: the matrix, one column per tried combination, that PBO and the deflated
    Sharpe need. The columns are named after their parameters."""
    data = np.asarray(pnl, dtype=float)
    if data.ndim != 2 or data.shape[1] != len(points) or capital_usd <= 0:
        raise WalkForwardError("bad_walkforward: a matrix of bars by combinations and a capital above zero")
    names = ["|".join(f"{k}={v}" for k, v in sorted(p.items())) for p in points]
    return pd.DataFrame(data / capital_usd, columns=names, index=index)


def _cost_in_sharpe(result: BacktestResult, returns: pd.Series) -> tuple[dict[str, Any], tuple[str, ...]]:
    """The cost of the strategy in units of Sharpe (`hokma.backtest.costs.cost_in_sharpe`), from its trades, its costs and its own equity."""
    closed = [t for t in result.trades if t.exit_at is not None]
    stamps = result.equity.timestamps
    years = (max(stamps) - min(stamps)).total_seconds() / (365.25 * 86400)
    capital = float(result.equity.equity_usd[0])
    sharpe = result.stats["sharpe"].value if "sharpe" in result.stats else metrics.compute(returns, ["sharpe"])["sharpe"].value
    try:
        if not closed or years <= 0:
            raise SpecError("bad_spec: the run has no closed trade or no time")
        per_trip = sum(float(v) for v in result.costs_usd.values()) / len(closed) / capital
        vol = float(returns.std(ddof=1)) * math.sqrt(PERIODS_PER_YEAR)
        trips = len(closed) / years
        cost = backtest_costs.cost_in_sharpe(round_trips_per_year=trips, cost_per_round_trip=per_trip, annual_volatility=vol)
        net = None if sharpe is None else float(sharpe)
        judged = backtest_costs.cost_in_sharpe(round_trips_per_year=trips, cost_per_round_trip=per_trip, annual_volatility=vol,
                                               gross_sharpe=None if net is None else net + cost.cost_sharpe)
    except SpecError as exc:
        return {"cost_sharpe": None, "net_sharpe": None}, (f"cost_in_sharpe_unavailable:{str(exc).split(':')[0]}",)
    return {"cost_sharpe": cost.cost_sharpe, "net_sharpe": judged.net_sharpe, "round_trips_per_year": trips, "cost_per_round_trip": per_trip,
            "annual_volatility": vol}, judged.warnings


def _plain(value: Any) -> dict[str, Any]:
    """A result of this module (or its saved form) as plain values."""
    return dict(value.to_dict() if hasattr(value, "to_dict") else value)


def assemble(strategy_id: str, version: int, *, matrix: pd.DataFrame, result: BacktestResult, ledger: Ledger | None = None,
             walk_forward: "WalkForwardResult | Mapping[str, Any] | None" = None, monte_carlo: "MonteCarloResult | Mapping[str, Any] | None" = None,
             purge: int = 5, embargo: int = 2, n_blocks: int = 16, n_folds: int = 10, n_test_folds: int = 2, wf_folds: int = 5) -> EvidenceBundle:
    """What the judge receives: the H1 evidence bundle (CPCV selection study, PBO, deflated Sharpe, walk forward and the leakage check) built
    from the returns `matrix` of the tried combinations, with the `n_trials` of the trial ledger (failed runs included: never a number that the
    caller passes), plus, in `parameters` and `warnings`, the cost in units of Sharpe, the ambiguous bars, the walk forward of the optimization
    and the Monte Carlo when they are given. It decides nothing: each tribunal fixes its own thresholds."""
    if not strategy_id or not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise WalkForwardError("bad_walkforward: a strategy id and a version of 1 or more")
    counted = resolve_ledger(ledger).count(strategy_id)
    columns = int(matrix.shape[1])
    trials = max(counted, columns)
    base = bundle(matrix, n_trials=trials, n_blocks=n_blocks, n_folds=n_folds, n_test_folds=n_test_folds, purge=purge, embargo=embargo, wf_folds=wf_folds)
    returns = results.equity_returns(result.equity)
    cost, cost_warnings = _cost_in_sharpe(result, returns)
    warnings = list(base.warnings)
    if counted < columns:
        warnings.append(f"trials_below_columns:{counted}<{columns}")
    warnings += list(cost_warnings)
    if result.ambiguous_bars:
        warnings.append(f"ambiguous_bars:{result.ambiguous_bars}")
    warnings += [w for w in result.warnings if w not in warnings]
    extra: dict[str, Any] = {"strategy_id": strategy_id, "strategy_version": version, "ledger_trials": counted, "cost_in_sharpe": cost,
                             "ambiguous_bars": result.ambiguous_bars, "ambiguous_bar_dates": [d.isoformat() for d in result.ambiguous_bar_dates]}
    if walk_forward is not None:
        extra["walk_forward_run"] = _plain(walk_forward)
    if monte_carlo is not None:
        extra["monte_carlo"] = _plain(monte_carlo)
    return dataclasses.replace(base, warnings=tuple(warnings), parameters={**base.parameters, **extra})


# --------------------------------------------------------------------------------------------------------------
# The public entry points (spec 4.2)
# --------------------------------------------------------------------------------------------------------------
def folds_for(n: int, scheme: Literal["rolling", "anchored", "cpcv"], train: int, test: int) -> tuple[int, int | None, tuple[str, ...]]:
    """What the splitter of H1 (`quant.validation.cv.split`) needs to cut `n` bars in windows of `train` and `test` bars: `(n_folds, n_test_folds, notes)`.

    The splitter cuts one size, `n // (n_folds + 1)`, for the train window and the test window of rolling and anchored (the train window of anchored
    only starts there and grows). The test window is what is measured out of sample, so it sets the count: `n_folds = n // test - 1`, and the window
    that results is the one the splitter uses; when it is not the `train` or the `test` that was asked, a note says what each became. For cpcv the
    `n // test` groups are the folds and each test set holds `n_folds - train // test` of them (at least 1, at most one less than all), so the
    train rows are about `train` bars. A number of bars that cannot hold three windows is refused."""
    if scheme not in SCHEMES:
        raise WalkForwardError(f"bad_walkforward: scheme must be one of {', '.join(SCHEMES)}")
    train, test = limits.as_int("train_size", train), limits.as_int("test_size", test)
    limits.check_param("train_size", train)
    limits.check_param("test_size", test)
    if train < 1 or test < 1:
        raise WalkForwardError("bad_walkforward: train and test are windows of at least one bar")
    groups = n // test
    if groups < 3:
        raise WalkForwardError(f"bad_walkforward: {n} bars hold fewer than 3 windows of {test} bars")
    if scheme == "cpcv":
        k = min(max(groups - max(train // test, 1), 1), groups - 1)
        return groups, k, (f"cpcv_groups:{groups}x{test}:test_groups:{k}",)
    block = n // groups                                   # n // (n_folds + 1) with n_folds = groups - 1
    notes = tuple(f"{name}_window_equalized:{asked}->{block}" for name, asked in (("test", test), ("train", train)) if asked != block)
    return groups - 1, None, notes


def pnl_matrix(spec: Any, services: Any, data: Any, points: Sequence[Mapping[str, Any]]) -> tuple[np.ndarray, dict[int, str]]:
    """The result of each point on each bar, in USD: rows are bars, columns are `points`. A strategy without stops or targets goes through the fast
    sweep (net of costs, marked to market); one that has them goes through the engine, and its column is the change of its equity curve bar by
    bar. A point whose strategy fails has a column of zeros and its message in the second value, by position."""
    from hokma.backtest.runner import StageError

    limits.check_param("n_combinations", len(points))
    bars = data.bars
    try:
        protective = bool(services.driver(spec, data, points[0]).protective_orders)
    except StageError:                                         # a strategy that does not build fails each point the same way below
        protective = False
    if not protective:
        swept, failed = sweep_points(spec, services, data, points, per_bar=True)
        return np.asarray(swept.pnl, dtype=float), failed
    columns, failed = [], {}
    capital = float(spec.capital_usd)
    for i, point in enumerate(points):
        try:
            driver = services.driver(spec, data, point)
            out = run_one(dataclasses.replace(spec, params={**spec.params, **dict(point)}), driver, bars)
            if driver.failure is not None:
                raise driver.failure
        except (StageError, SpecError) as error:                # a point that cannot run is a failed trial
            failed[i] = str(error)[:300]
            columns.append(np.zeros(len(bars)))
            continue
        curve = np.array([float(v) for _t, v in out.equity[: len(bars)]])
        columns.append(np.diff(curve, prepend=capital))
    return np.column_stack(columns), failed


def _record_trials(db: Any, spec: Any, points: Sequence[Mapping[str, Any]], pnl: np.ndarray, failed: Mapping[int, str]) -> None:
    """Each point that was executed is one row of the trial ledger, once; its objective is its total result."""
    ledger = DbLedger(db)
    for i, point in enumerate(points):
        if i in failed:
            ledger.record(spec.strategy_id, spec.strategy_id, None, point, "failed", None, error=failed[i])
        else:
            ledger.record(spec.strategy_id, spec.strategy_id, None, point, "done", float(pnl[:, i].sum()))


def walk_forward(spec: Any, space: ParamSpace, *, scheme: Literal["rolling", "anchored", "cpcv"], train: int, test: int, purge: int, embargo: int,
                 services: Any = None) -> WalkForwardResult:
    """Walk forward (rolling or anchored) or combinatorial purged validation (cpcv) of every valid point of `space` (spec 4.2). `train` and `test`
    are window sizes in bars of the spec, mapped to the splitter of H1 by `folds_for` (the notes say when a window had to be equalized); `purge` and
    `embargo` are bars. Each window chooses its point with its train bars alone and is measured on its test bars. The strategy comes from the
    registry and the bars from the data feed; each point is executed once and is one row of the trial ledger. The result is saved (kind walkforward)
    with the points, so `evidence` can rebuild their returns."""
    from hokma.backtest import runner

    purge, embargo = limits.as_int("window", purge), limits.as_int("window", embargo)
    limits.check_param("window", purge)
    limits.check_param("window", embargo)
    services = services or runner.Services()
    db = services.database()
    data = services.load(spec)
    n_folds, n_test_folds, notes = folds_for(len(data.bars), scheme, train, test)
    points = space.combinations()
    pnl, failed = pnl_matrix(spec, services, data, points)
    result = run(points, pnl, scheme=scheme, n_folds=n_folds, purge=purge, embargo=embargo, n_test_folds=n_test_folds)
    result = dataclasses.replace(result, warnings=result.warnings + notes + tuple(f"failed_point:{i}" for i in sorted(failed)))
    _record_trials(db, spec, points, pnl, failed)
    body = {"result": result.to_dict(), "space": space.to_dict(), "points": [dict(p) for p in points], "train": limits.as_int("train_size", train),
            "test": limits.as_int("test_size", test), "n_test_folds": n_test_folds}
    return dataclasses.replace(result, run_id=runner.save_row(db, "walkforward", spec, body, data_hash=data.data_hash))


def _latest(db: Any, kind: str, strategy_id: str, version: int | None = None, parent_id: int | None = None) -> dict[str, Any] | None:
    sql, params = "SELECT * FROM backtest_runs WHERE kind = :k AND status IN ('done', 'account_blown')", {"k": kind}
    if parent_id is not None:
        sql, params["p"] = sql + " AND parent_id = :p", int(parent_id)
    else:
        sql, params["s"], params["v"] = sql + " AND strategy_id = :s AND strategy_version = :v", strategy_id, int(version or 0)
        if kind == "run":                                                   # a run derived from another (a neighbor, the ideal point) is not the run of the version
            sql += " AND parent_id IS NULL"
    with db.read() as u:
        return u.session.one(sql + " ORDER BY id DESC LIMIT 1", params)


def load_result(db: Any, run_id: int, *, curve_dir: Any) -> BacktestResult:
    """A saved run as a `BacktestResult`: its summary, its trades and its equity curve (read back only if its file has the stored hash)."""
    with db.read() as u:
        row = u.session.one("SELECT summary_json FROM backtest_runs WHERE id = :i", {"i": int(run_id)})
    if row is None or not row["summary_json"]:
        raise WalkForwardError(f"bad_walkforward: run {run_id} has no saved result")
    body = {**json.loads(row["summary_json"]), "trades": [], "equity": {"timestamps": [], "equity_usd": []}}
    saved = results.from_dict(body)
    curve: EquityCurve = results.load_curve(db, run_id, curve_dir=curve_dir)
    return dataclasses.replace(saved, trades=results.load_trades(db, run_id), equity=curve)


def evidence(strategy_id: str, version: int, *, services: Any = None) -> EvidenceBundle:
    """What the judge of a line receives about a strategy (spec 4.2): the H1 bundle (CPCV selection study, PBO, deflated Sharpe, walk forward and the
    leakage check) from the returns of the points of the latest saved walk forward, with the `n_trials` of the ledger; plus the cost in units of Sharpe
    and the ambiguous bars of the latest finished run of that strategy and version, the out of sample result of the walk forward and the latest Monte
    Carlo of that run. The returns are rebuilt from the saved points on the same bars: if the data is no longer the walk forward's, nothing is built.
    A missing run or walk forward is refused with its code; a missing Monte Carlo is a warning. The backtest approves nothing."""
    from hokma.backtest import runner

    if not strategy_id or not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise WalkForwardError("bad_walkforward: a strategy id and a version of 1 or more")
    services = services or runner.Services()
    db = services.database()
    run_row = _latest(db, "run", strategy_id, version)
    walk_row = _latest(db, "walkforward", strategy_id, version)
    if run_row is None:
        raise WalkForwardError(f"no_run: {strategy_id} version {version} has no finished run: run it first")
    if walk_row is None:
        raise WalkForwardError(f"no_walk_forward: {strategy_id} version {version} has no walk forward: its tried points are needed for PBO and the deflated Sharpe")
    spec = runner.spec_from_canonical(json.loads(walk_row["spec_json"]))
    data = services.load(spec)
    if data.data_hash != walk_row["data_hash"]:
        raise WalkForwardError("data_changed: the bars are not the ones of the saved walk forward: its returns cannot be rebuilt as evidence")
    summary = json.loads(walk_row["summary_json"])
    points = summary["points"]
    pnl, _failed = pnl_matrix(spec, services, data, points)
    matrix = returns_matrix(pnl, points, capital_usd=float(spec.capital_usd), index=pd.DatetimeIndex([b.ts for b in data.bars]))
    carlo_row = _latest(db, "montecarlo", strategy_id, parent_id=run_row["id"])
    carlo = None if carlo_row is None else json.loads(carlo_row["summary_json"])
    bundle_ = assemble(strategy_id, version, matrix=matrix, result=load_result(db, run_row["id"], curve_dir=services.curves()), ledger=DbLedger(db),
                       walk_forward={**summary["result"], "run_id": walk_row["id"]}, monte_carlo=carlo)
    if carlo is None:
        bundle_ = dataclasses.replace(bundle_, warnings=bundle_.warnings + ("monte_carlo_missing",))
    return bundle_
