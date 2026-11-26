"""The bridge from what H4 saved to the facts of the audit (SPEC-032: 4.3, 5.4, 2.3).

`gather` finds the latest saved run, walk forward, optimization and Monte Carlo of a version of a strategy (the same search as
`walkforward._latest`) and the evidence bundle of H1 that `walkforward.evidence` builds from them. `build_evidence` fills `AuditEvidence` from the
bundle, the answers of the owner and the limits of the line. Nothing here calculates a statistic: Sharpe, PBO, DSR, the cost in units of Sharpe and
the regime label come from `hokma.quant`, `hokma.backtest.walkforward` and `hokma.desks.swo.regime`; what this file adds is the choice of which saved
series goes to which function (the walk forward of the house is read again with the same function that wrote it, to get its per bar series).

What runs the engine here, and only here: the neighbors of the parameters (`neighbor_sharpes`) and the one run of the ideal point (`point_run`). Each
one is a trial of the ledger (hypothesis `{strategy}:neighbors` and `{strategy}:ideal`), and a point that already has a finished run of the same
spec, data and parameters is reused, so auditing twice never raises the count. A run that failed for a stage is a known failure and is not run again.

A note is a stable code (`no_walk_forward`, `data_changed`, `no_optimization`, ...) with an optional detail after a colon: the screen translates it.
No module of this file reaches a broker or a connector: the services are the ones of the backtest.
"""
from __future__ import annotations

import dataclasses
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import pandas as pd

from hokma.backtest import trials
from hokma.desks.swo.regime import derive_regime
from hokma.factory import audit
from hokma.factory import spec as factory_spec
from hokma.factory.questions import QUESTIONS
from hokma.factory.researcher import ResearchError
from hokma.quant import metrics
from hokma.quant.errors import BadInput
from hokma.quant.metrics.returns import bars_per_year

if TYPE_CHECKING:
    from hokma.backtest.optimize import OptimizationResult
    from hokma.backtest.spec import BacktestSpec
    from hokma.factory.params import ParamSchema
    from hokma.quant.validation.evidence import EvidenceBundle

REGIME_FILTERS = ("regime_adx", "regime_vol")
VALIDATION = {"walk_forward": "walk_forward", "expanding": "walk_forward", "cpcv": "cpcv"}      # walkforward.SCHEMES values -> audit.VALIDATION_SCHEMES
ANSWER_FIELDS = tuple(q.evidence_field for q in QUESTIONS)
_CODED = re.compile(r"^[a-z][a-z0-9_]*(:|$)")
_FINISHED = ("done", "account_blown")
NEIGHBOR_PROGRESS: ContextVar[Callable[[int, int], None] | None] = ContextVar("hokma_neighbor_progress", default=None)
"""Who is told how far the neighbors are: `hook(done, total)` is called before the first neighbor and after each one by the code that runs in this context
(the verdict job of the screen sets it, to show "Probando vecinos k de n"). Nothing else reads it and it changes nothing about what runs."""


class BridgeError(ValueError):
    """The facts of a strategy cannot be gathered. `code` is stable: bad_input, no_spec (no saved spec of that version) or no_run (it has no finished run)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class BridgeInputs:
    strategy_id: str
    version: int
    line: str
    run_id: int
    walkforward_id: int | None
    optimize_id: int | None
    montecarlo_id: int | None
    uses_regimes: bool
    bundle: "EvidenceBundle | None"            # walkforward.evidence(...); None with the reason in notes
    optimization: "OptimizationResult | None"
    notes: tuple[str, ...]                     # stable codes: no_walk_forward, no_optimization, data_changed, ...


# ---- small helpers ------------------------------------------------------------------------------------------------------------------

def resolve_services(services: Any = None) -> Any:
    """The services given, or the ones of the backtest tools (the registry of the kit and the default database, bars and curve folder)."""
    if services is not None:
        return services
    from hokma.kit import backtest_tools

    return backtest_tools.services()


def _check_ids(strategy_id: Any, version: Any) -> None:
    if not isinstance(strategy_id, str) or not 1 <= len(strategy_id) <= 80:
        raise BridgeError("bad_input", "strategy_id is text of 1 to 80 characters")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise BridgeError("bad_input", "version is a whole number of 1 or more")


def hypothesis_of(strategy_id: str, suffix: str) -> str:
    """`{strategy}:{suffix}` held inside the length that the ledger accepts."""
    text = f"{strategy_id}:{suffix}"
    return text if len(text) <= trials.MAX_ID else f"{strategy_id[: trials.MAX_ID - len(suffix) - 1]}:{suffix}"


def _stat(stats: Mapping[str, Any], name: str) -> float | None:
    block = stats.get(name)
    value = block.get("value") if isinstance(block, Mapping) else getattr(block, "value", None)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _coded(texts: Sequence[str]) -> list[str]:
    return [t for t in texts if isinstance(t, str) and _CODED.match(t)]


def _note_code(exc: Exception, known: tuple[str, ...], default: str) -> str:
    head = str(exc).strip("'\"").split(":")[0]
    return head if head in known else default


def _row(db: Any, run_id: int) -> Mapping[str, Any]:
    with db.read() as u:
        row = u.session.one("SELECT id, spec_json, data_hash, summary_json, status FROM backtest_runs WHERE id = :i", {"i": int(run_id)})
    if row is None:
        raise BridgeError("no_run", f"the run {run_id} does not exist")
    return row


def base_spec(inputs: BridgeInputs, *, services: Any = None) -> "BacktestSpec":
    """The spec of the run that the verdict judges, as it was saved."""
    from hokma.backtest import runner

    return runner.spec_from_canonical(json.loads(_row(resolve_services(services).database(), inputs.run_id)["spec_json"]))


# ---- regimes (5.4) ------------------------------------------------------------------------------------------------------------------

def _filter_names(filters: Any) -> tuple[str, ...]:
    if filters is None:
        return ()
    if isinstance(filters, str):
        return (filters,)
    names: list[str] = []
    if isinstance(filters, Mapping):
        names += [str(k) for k in filters]
        items: Sequence[Any] = list(filters.values())
    elif isinstance(filters, (list, tuple)):
        items = filters
    else:
        return ()
    for item in items:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, Mapping):
            names += [str(item[k]) for k in ("kind", "name", "type", "id") if isinstance(item.get(k), str)]
    return tuple(names)


def declares_regimes(spec: "factory_spec.StrategySpec", registry: Any = None) -> bool:
    """True only when the spec says so: a regime filter (`regime_adx`, `regime_vol`) in the key `filters` of its pieces, or the parameter `uses_regimes: true`
    of its alpha in the registry. A spec whose pieces have no `filters` key is the empty list: no declaration, the check does not apply."""
    parts = spec.parts if isinstance(spec.parts, Mapping) else {}
    if any(name in REGIME_FILTERS for name in _filter_names(parts.get("filters"))):
        return True
    if spec.params.get("uses_regimes") is True:
        return True
    try:
        entry = (registry or factory_spec.default_registry()).get(spec.alpha)
    except KeyError:
        return False
    return entry.params.get("uses_regimes") is True


# ---- gather -------------------------------------------------------------------------------------------------------------------------

def _parsed(strategy_id: str, version: int, db: Any) -> tuple[Any, Any]:
    try:
        row = factory_spec.load(strategy_id, version, db=db)
        return row, factory_spec.parse_spec(row.spec_yaml)
    except ResearchError as exc:
        raise BridgeError("no_spec", str(exc)[:300]) from exc


def line_of(strategy_id: str, version: int, *, services: Any = None) -> str:
    """The line of the saved spec of a version (DT, SWC, SWO, LP or RF), before anything is run. `no_spec` when it has none."""
    _check_ids(strategy_id, version)
    return _parsed(strategy_id, version, resolve_services(services).database())[0].line


def gather(strategy_id: str, version: int, *, services: Any = None, bundle: bool = True) -> BridgeInputs:
    """The saved facts of a version of a strategy. `no_spec` and `no_run` when it has no saved spec or no finished run. A missing walk forward or
    optimization is not an error: the bundle is None, the note says why, and the audit leaves what depends on it as unknown.
    `bundle=False` skips the evidence bundle (it rebuilds the returns of every tried point): the callers that only need the ids and the optimization use it."""
    from hokma.backtest import optimize as backtest_optimize
    from hokma.backtest import walkforward
    from hokma.backtest.runner import StageError
    from hokma.backtest.spec import SpecError

    _check_ids(strategy_id, version)
    services = resolve_services(services)
    db = services.database()
    row, parsed = _parsed(strategy_id, version, db)
    run = walkforward._latest(db, "run", strategy_id, version)
    if run is None:
        raise BridgeError("no_run", f"{strategy_id} version {version} has no finished run: run it first")
    walk = walkforward._latest(db, "walkforward", strategy_id, version)
    opt = walkforward._latest(db, "optimize", strategy_id, version)
    carlo = walkforward._latest(db, "montecarlo", strategy_id, parent_id=run["id"])
    uses = declares_regimes(parsed)
    notes: list[str] = []
    found: "EvidenceBundle | None" = None
    if walk is None:
        notes.append("no_walk_forward")
    elif bundle:
        try:
            found = walkforward.evidence(strategy_id, version, services=services)
        except (walkforward.WalkForwardError, BadInput, StageError, SpecError) as exc:
            notes.append(_note_code(exc, ("no_walk_forward", "data_changed", "no_run"), "evidence_unavailable"))
    optimization = None
    if opt is None:
        notes.append("no_optimization")
    else:
        try:
            optimization = backtest_optimize.load(db, int(opt["id"]))
        except backtest_optimize.SearchError:
            notes.append("optimization_unreadable")
    if uses:
        notes.append("regimes_from_instrument_bars")
    if found is not None:
        notes += _coded(found.warnings)
    elif run["summary_json"]:
        notes += _coded(json.loads(run["summary_json"]).get("warnings") or [])
    return BridgeInputs(strategy_id, version, row.line, int(run["id"]), None if walk is None else int(walk["id"]), None if opt is None else int(opt["id"]),
                        None if carlo is None else int(carlo["id"]), uses, found, optimization, tuple(dict.fromkeys(notes)))


# ---- runs of the engine: neighbors and the ideal point ------------------------------------------------------------------------------

def _params_text(params: Mapping[str, Any]) -> str | None:
    try:
        return json.dumps(dict(params), sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return None


_SAVED = ("SELECT r.id AS run_id, r.status AS run_status, r.summary_json AS summary_json FROM backtest_trials t JOIN backtest_runs r ON r.id = t.run_id "
          "WHERE t.strategy_id = :s AND t.params_json = :p AND r.kind = 'run' AND r.spec_hash = :h "
          "AND ((r.status IN ('done', 'account_blown') AND r.data_hash = :d) OR r.status = 'failed') ORDER BY t.id DESC LIMIT 1")


def _saved(db: Any, spec: "BacktestSpec", data_hash: str) -> tuple[int | None, float | None] | None:
    """The finished run of the same strategy, parameters, spec and data that the ledger already holds: (run id, Sharpe), or (None, None) for a run that
    failed in a stage (it fails again the same way). None when there is no such run."""
    text = _params_text(spec.params)
    if text is None:
        return None
    with db.read() as u:
        row = u.session.one(_SAVED, {"s": spec.strategy_id, "p": text, "h": spec.fingerprint(), "d": data_hash})
    if row is None:
        return None
    if row["run_status"] == "failed":
        return None, None
    return int(row["run_id"]), _stat(json.loads(row["summary_json"]).get("stats") or {}, "sharpe")


def _run(services: Any, spec: "BacktestSpec", hypothesis: str, parent_id: int) -> Any:
    """`Services.run` with the hypothesis of the ledger (the label that the trial is recorded under) and the run it derives from: a run with a parent is a
    derived run, and the search of the latest run of a version (`walkforward._latest`) leaves it out, so a neighbor never becomes the run that is judged."""
    from hokma.backtest import runner

    strategy = None if services.strategy is None else services.strategy(spec.params)
    return runner.run(spec, strategy=strategy, registry=services.registry, source=services.source, fetch=services.fetch, catalog=services.catalog,
                      db=services.db, curve_dir=services.curve_dir, hypothesis_id=hypothesis, parent_id=parent_id)


def _schema(db: Any, services: Any, spec: "BacktestSpec") -> "ParamSchema | None":
    """The schema that holds the moves of the parameters inside their ranges: the one of the strategy in the registry, else the one of the alpha of the saved spec."""
    try:
        if services.registry is not None:
            return getattr(services.registry.get(spec.strategy_id).cls, "SCHEMA", None)
    except KeyError:
        pass
    try:
        parsed = factory_spec.parse_spec(factory_spec.load(spec.strategy_id, spec.strategy_version, db=db).spec_yaml)
        return getattr(factory_spec.default_registry().get(parsed.alpha).cls, "SCHEMA", None)
    except (ResearchError, KeyError):
        return None


def neighbor_sharpes(inputs: BridgeInputs, *, spread: float, services: Any = None, params: Mapping[str, Any] | None = None) -> dict[str, float]:
    """The annual Sharpe of each parameter set one step away (`audit.parameter_neighbors`, at most `audit.MAX_NEIGHBORS`), by label.

    The base is the saved run of the verdict, or the same spec with `params` (the ideal point). A neighbor that has a finished run of the same spec and data
    in the ledger is read, not run; the others run once and are recorded as trials of `{strategy}:neighbors`. A neighbor with no Sharpe (it failed, or its
    run is too short for one) is left out. `BadInput` when `spread` is not a share between 0 and 1."""
    services = resolve_services(services)
    db = services.database()
    run = _row(db, inputs.run_id)
    from hokma.backtest import runner

    spec = runner.spec_from_canonical(json.loads(run["spec_json"]))
    if params is not None:
        spec = dataclasses.replace(spec, params=dict(params))
    out: dict[str, float] = {}
    around = list(audit.parameter_neighbors(spec.params, spread, _schema(db, services, spec)))
    report = NEIGHBOR_PROGRESS.get()
    if report is not None:
        report(0, len(around))
    for done, neighbor in enumerate(around, start=1):
        moved = dataclasses.replace(spec, params=dict(neighbor.params))
        found = _saved(db, moved, run["data_hash"])
        if found is None:
            result = _run(services, moved, hypothesis_of(spec.strategy_id, "neighbors"), inputs.run_id)
            sharpe = _stat(result.stats, "sharpe") if result.status in _FINISHED else None
        else:
            sharpe = found[1]
        if sharpe is not None:
            out[neighbor.label] = sharpe
        if report is not None:
            report(done, len(around))
    return out


def point_run(inputs: BridgeInputs, params: Mapping[str, Any], *, services: Any = None) -> tuple[int | None, float | None]:
    """(run id, annual Sharpe) of the base spec with `params`: the finished run that the ledger holds, or one run recorded as a trial of `{strategy}:ideal`.
    (None, None) when that point fails in a stage."""
    services = resolve_services(services)
    db = services.database()
    run = _row(db, inputs.run_id)
    from hokma.backtest import runner

    spec = dataclasses.replace(runner.spec_from_canonical(json.loads(run["spec_json"])), params=dict(params))
    found = _saved(db, spec, run["data_hash"])
    if found is not None:
        return found
    result = _run(services, spec, hypothesis_of(spec.strategy_id, "ideal"), inputs.run_id)
    if result.status not in _FINISHED:
        return None, None
    return int(result.run_id), _stat(result.stats, "sharpe")


# ---- Sharpe in sample and out of sample (2.3 point 2) -------------------------------------------------------------------------------

def _sharpe(series: pd.Series, periods_per_year: int) -> float | None:
    try:
        value = metrics.compute(series, ["sharpe"], periods_per_year=periods_per_year)["sharpe"].value
    except BadInput:
        return None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _mean(values: Sequence[float | None]) -> float | None:
    seen = [v for v in values if v is not None]
    return sum(seen) / len(seen) if seen else None


def sample_sharpes(inputs: BridgeInputs, *, services: Any = None) -> tuple[float | None, float | None]:
    """(Sharpe in sample, Sharpe out of sample) of the walk forward. The walk forward optimizes the pnl, so its windows hold gains, not Sharpe ratios: the
    per bar pnl of its points is rebuilt on the same bars (`walkforward.pnl_matrix`) and read again with `walkforward.run` and the saved settings.
    Out of sample: the Sharpe of `oos_pnl` (the tests laid end to end; for cpcv, whose tests overlap, the mean of the Sharpe of each test set). In sample:
    the mean of the Sharpe of the chosen point on the train rows of each window. (None, None) with no walk forward, with changed data, or when the saved
    choices cannot be reproduced. Both use `quant.metrics`, annualized with the bars of the year of the run."""
    from hokma.backtest import walkforward

    if inputs.walkforward_id is None or inputs.bundle is None:
        return None, None
    services = resolve_services(services)
    row = _row(services.database(), inputs.walkforward_id)
    from hokma.backtest import runner

    spec = runner.spec_from_canonical(json.loads(row["spec_json"]))
    data = services.load(spec)
    if data.data_hash != row["data_hash"]:
        return None, None
    body = json.loads(row["summary_json"])
    saved, points = body["result"], body["points"]
    pnl, _failed = walkforward.pnl_matrix(spec, services, data, points)
    again = walkforward.run(points, pnl, scheme=saved["scheme"], n_folds=saved["n_folds"], purge=saved["purge"], embargo=saved["embargo"],
                            objective=saved["objective"], n_test_folds=body.get("n_test_folds"))
    if [dict(w.chosen) for w in again.windows] != [w["chosen"] for w in saved["windows"]]:
        return None, None
    folds = walkforward.windows(len(pnl), scheme=saved["scheme"], n_folds=saved["n_folds"], purge=saved["purge"], embargo=saved["embargo"],
                                n_test_folds=body.get("n_test_folds"))
    capital = float(spec.capital_usd)
    ppy = bars_per_year(data.sheet.asset_type, spec.bar, session=spec.session)
    in_sample = _mean([_sharpe(pd.Series(pnl[fold.train, w.column] / capital), ppy) for fold, w in zip(folds, again.windows)])
    if again.oos_pnl is not None:
        return in_sample, _sharpe(pd.Series(list(again.oos_pnl)) / capital, ppy)
    return in_sample, _mean([_sharpe(pd.Series(pnl[fold.test, w.column] / capital), ppy) for fold, w in zip(folds, again.windows)])


# ---- regimes: Sharpe by label -------------------------------------------------------------------------------------------------------

def _daily_bars(bars: Sequence[Any]) -> pd.DataFrame:
    """The daily open, high, low and close of the bars of the run (the series `derive_regime` reads)."""
    index = pd.DatetimeIndex([pd.Timestamp(b.ts) for b in bars])
    index = (index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")).normalize().tz_localize(None)
    frame = pd.DataFrame({name: [float(getattr(b, name)) for b in bars] for name in ("open", "high", "low", "close")}, index=index)
    grouped = frame.groupby(level=0)
    return pd.DataFrame({"open": grouped["open"].first(), "high": grouped["high"].max(), "low": grouped["low"].min(), "close": grouped["close"].last()})


def regime_sharpes(inputs: BridgeInputs, *, services: Any = None) -> dict[str, float] | None:
    """The annual Sharpe of the run in each regime, only for a strategy that declares regimes. Each calendar month of the run takes the label that
    `derive_regime` gives from the daily bars of the same instrument closed before the month; a month with no label does not count. The returns of the months of
    a label are laid together and measured with `quant.metrics`. Fewer than two labels with a Sharpe: None (the check stays unknown)."""
    from hokma.backtest import results, walkforward

    if not inputs.uses_regimes:
        return None
    services = resolve_services(services)
    db = services.database()
    from hokma.backtest import runner

    spec = runner.spec_from_canonical(json.loads(_row(db, inputs.run_id)["spec_json"]))
    daily = _daily_bars(services.load(spec).bars)
    returns = results.equity_returns(walkforward.load_result(db, inputs.run_id, curve_dir=services.curves()).equity)
    by_label: dict[str, list[pd.Series]] = {}
    for (year, month), part in returns.groupby([returns.index.year, returns.index.month]):
        label, _facts = derive_regime(daily, datetime(int(year), int(month), 1, tzinfo=timezone.utc))
        if label is not None:
            by_label.setdefault(label, []).append(part)
    found = {label: _sharpe(pd.concat(parts), 252) for label, parts in sorted(by_label.items())}
    kept = {label: value for label, value in found.items() if value is not None}
    return kept if len(kept) >= 2 else None


# ---- the evidence -------------------------------------------------------------------------------------------------------------------

def build_evidence(inputs: BridgeInputs, *, thresholds: Mapping[str, Any], answers: Mapping[str, bool | None], services: Any = None) -> audit.AuditEvidence:
    """The `AuditEvidence` of a version: the fill table of 4.3. What is missing stays None and the audit reads it as unknown. `thresholds` are the values of
    the set of the line (the neighbor spread and the border switch are read here) and `answers` the fields of `questions.evidence_fields`. `bad_input` when the
    ledger holds no trial of the strategy."""
    from hokma.backtest import walkforward

    services = resolve_services(services)
    db = services.database()
    unknown = sorted(set(answers) - set(ANSWER_FIELDS))
    if unknown:
        raise BridgeError("bad_input", f"unknown answer fields: {', '.join(unknown)}")
    spread = thresholds.get("neighbor_spread")
    if isinstance(spread, bool) or not isinstance(spread, (int, float)):
        raise BridgeError("bad_input", "the thresholds hold no neighbor_spread")
    if trials.count(inputs.strategy_id, db=db) < 1:
        raise BridgeError("bad_input", f"{inputs.strategy_id} has no trial in the ledger: run a backtest first")
    bundle = inputs.bundle
    sample_in, sample_out = sample_sharpes(inputs, services=services) if bundle is not None else (None, None)
    neighbors = neighbor_sharpes(inputs, spread=float(spread), services=services)
    cost = (bundle.parameters.get("cost_in_sharpe") or {}) if bundle is not None else {}
    cost_sharpe, net_sharpe = cost.get("cost_sharpe"), cost.get("net_sharpe")
    gross = None if cost_sharpe is None or net_sharpe is None else net_sharpe + cost_sharpe
    scheme = None
    if bundle is not None:
        saved = (bundle.parameters.get("walk_forward_run") or {}).get("scheme")
        scheme = VALIDATION.get(walkforward.SCHEMES.get(saved, "")) if saved is not None else None
    edge = None if inputs.optimization is None or not thresholds.get("edge_warns") else tuple(inputs.optimization.edge_of_space)
    return audit.AuditEvidence(
        n_trials=trials.count(inputs.strategy_id, db=db), **{name: answers.get(name) for name in ANSWER_FIELDS},
        leak_checked=None if bundle is None else bundle.leakage_free, validation_scheme=scheme,
        multiple_testing_applied=True if bundle is not None and bundle.dsr is not None else None,
        dsr=None if bundle is None else bundle.dsr, pbo=None if bundle is None else bundle.pbo, is_sharpe=sample_in, oos_sharpe=sample_out,
        neighbor_sharpes=neighbors or None, regime_sharpes=regime_sharpes(inputs, services=services) if inputs.uses_regimes else None,
        cost_sharpe=cost_sharpe, gross_sharpe=gross, edge_of_space=edge, uses_regimes=inputs.uses_regimes)
