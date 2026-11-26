"""The specification of a strategy and its tests first (SPEC-020: SKL-002).

A strategy spec is a short YAML text: which alpha, with which parameters, for which line, and the tests that say what the strategy must do
before any backtest runs. A signal test gives facts and the view that must come out; a position size test gives a size, a price, the equity
and the contract and the quantity that must come out. `require_tests` refuses a spec without both kinds (`missing_tests`) and runs them: a
failure stops the backtest (`tests_failed`). The tests are data, never code that an agent writes, so running them runs only the code of the factory.

`strategy_specs` rows are saved as `draft` and only `propose` moves one to `proposed`, which is where it stops: nothing here sends it anywhere.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import yaml

from hokma.contracts.identity import ASSET_TYPES, ContractSheet
from hokma.db import Database, get_database
from hokma.desks.core.hashing import content_hash
from hokma.desks.core.signal import LINES, SignalContext
from hokma.factory.orders import BadSize, Size, SizeType, to_contracts
from hokma.factory.parts import validate_parts
from hokma.factory.params import ParamError, validate
from hokma.factory.registry import Registry
from hokma.factory.strategy import BadStrategy
from hokma.factory.researcher import (AGENT_SLUG, MAX_FAILED_ATTEMPTS, AttemptBudget, AttemptBudgetSpent, MissingTests, ResearchError, TestsFailed,
                                      clean_root_cause, failed_in, window_start)

MAX_SPEC_BYTES = 32 * 1024
MAX_CASES = 50
MAX_FACTS = 8
MAX_FACT_VALUES = 5_000
SIGNAL_EXPECTS = ("long", "short", "flat", "abstain")
TOP_KEYS = {"strategy_id", "line", "alpha", "params", "hypothesis", "tests", "parts"}
SIGNAL_KEYS = {"name", "facts", "expect"}
SIZE_KEYS = {"name", "contract_id", "asset_type", "multiplier", "kind", "value", "price", "equity", "stop_distance", "expect_qty"}
TEST_TIME = datetime(2000, 1, 3, 15, 0, tzinfo=timezone.utc)
PROPOSER = AGENT_SLUG                  # who proposed is fixed here: an agent never writes it


class BadSpec(ResearchError):
    code = "bad_spec"


@dataclass(frozen=True)
class SignalCase:
    name: str
    facts: Mapping[str, tuple[float, ...]]
    expect: str


@dataclass(frozen=True)
class SizeCase:
    name: str
    contract_id: str
    asset_type: str
    multiplier: Decimal
    kind: str
    value: Decimal
    price: Decimal
    equity: Decimal
    stop_distance: Decimal | None
    expect_qty: Decimal


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    line: str
    alpha: str
    params: Mapping[str, Any]
    hypothesis: str
    signal_tests: tuple[SignalCase, ...]
    size_tests: tuple[SizeCase, ...]
    parts: Mapping[str, Any] | None = None        # the sizer, the exit rule and the execution of the composite; absent means the defaults of `parts`

    def canonical(self) -> dict[str, Any]:
        extra = {} if self.parts is None else {"parts": {k: [dict(f) for f in v] if k == "filters" else dict(v) for k, v in self.parts.items()}}   # filters is a list, and is there only when it names one
        return {**extra, "strategy_id": self.strategy_id, "line": self.line, "alpha": self.alpha, "params": dict(self.params), "hypothesis": self.hypothesis,
                "signal_tests": [{"name": c.name, "facts": {k: list(v) for k, v in c.facts.items()}, "expect": c.expect} for c in self.signal_tests],
                "size_tests": [{"name": c.name, "contract_id": c.contract_id, "asset_type": c.asset_type, "multiplier": str(c.multiplier), "kind": c.kind,
                                "value": str(c.value), "price": str(c.price), "equity": str(c.equity),
                                "stop_distance": None if c.stop_distance is None else str(c.stop_distance), "expect_qty": str(c.expect_qty)}
                               for c in self.size_tests]}

    @property
    def spec_hash(self) -> str:
        return content_hash(self.canonical())


@dataclass(frozen=True)
class SpecRow:
    id: int
    strategy_id: str
    version: int
    line: str
    spec_yaml: str
    spec_hash: str
    status: str


def _decimal(name: str, value: Any, *, positive: bool = True) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise BadSpec(f"{name} is a number")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise BadSpec(f"{name} is a number") from None
    if not number.is_finite() or (positive and number <= 0):
        raise BadSpec(f"{name} is a finite number above zero")
    return number


def _name(value: Any, where: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 80 or not value.isprintable():
        raise BadSpec(f"{where}: name is text of 1 to 80 characters")
    return value


def _signal_case(raw: Any) -> SignalCase:
    if not isinstance(raw, Mapping) or set(raw) != SIGNAL_KEYS:
        raise BadSpec(f"a signal test has the keys {sorted(SIGNAL_KEYS)}")
    facts = raw["facts"]
    if not isinstance(facts, Mapping) or not 1 <= len(facts) <= MAX_FACTS:
        raise BadSpec(f"facts is a mapping of 1 to {MAX_FACTS} series")
    series: dict[str, tuple[float, ...]] = {}
    for key, values in facts.items():
        if not isinstance(key, str) or not isinstance(values, list) or not 1 <= len(values) <= MAX_FACT_VALUES:
            raise BadSpec(f"fact {key!r} is a list of 1 to {MAX_FACT_VALUES} numbers")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
            raise BadSpec(f"fact {key!r} holds only finite numbers")
        series[key] = tuple(float(v) for v in values)
    if raw["expect"] not in SIGNAL_EXPECTS:
        raise BadSpec(f"expect is one of {', '.join(SIGNAL_EXPECTS)}")
    return SignalCase(_name(raw["name"], "signal test"), series, raw["expect"])


def _size_case(raw: Any) -> SizeCase:
    required = SIZE_KEYS - {"stop_distance"}
    if not isinstance(raw, Mapping) or not required <= set(raw) <= SIZE_KEYS:
        raise BadSpec(f"a position size test has the keys {sorted(required)} and optionally stop_distance")
    if raw["asset_type"] not in ASSET_TYPES:
        raise BadSpec(f"asset_type is one of {', '.join(ASSET_TYPES)}")
    if not isinstance(raw["contract_id"], str) or not 1 <= len(raw["contract_id"]) <= 80:
        raise BadSpec("contract_id is text of 1 to 80 characters")
    try:
        kind = SizeType.parse(raw["kind"]).value
    except BadSize as exc:
        raise BadSpec(str(exc)) from None
    stop = raw.get("stop_distance")
    return SizeCase(_name(raw["name"], "position size test"), raw["contract_id"], raw["asset_type"], _decimal("multiplier", raw["multiplier"]), kind,
                    _decimal("value", raw["value"]), _decimal("price", raw["price"]), _decimal("equity", raw["equity"]),
                    None if stop is None else _decimal("stop_distance", stop), _decimal("expect_qty", raw["expect_qty"], positive=False))


def parse_spec(text: str, registry: Registry | None = None) -> StrategySpec:
    """The spec of a YAML text, strictly: known keys only, an alpha of the registry with parameters its schema accepts, at most 50 tests of each kind.
    The tests may be absent here; `require_tests` is what refuses that."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_SPEC_BYTES:
        raise BadSpec(f"a spec is text of at most {MAX_SPEC_BYTES} bytes")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        raise BadSpec("the spec is not valid YAML") from None
    if not isinstance(data, Mapping) or set(data) - TOP_KEYS or not {"strategy_id", "line", "alpha"} <= set(data):
        raise BadSpec(f"a spec is a mapping with strategy_id, line, alpha and optionally {', '.join(sorted(TOP_KEYS - {'strategy_id', 'line', 'alpha'}))}")
    strategy_id, line = data["strategy_id"], data["line"]
    if not isinstance(strategy_id, str) or not 1 <= len(strategy_id) <= 80 or not strategy_id.isprintable():
        raise BadSpec("strategy_id is text of 1 to 80 characters")
    if line not in LINES:
        raise BadSpec(f"line is one of {', '.join(LINES)}")
    registry = registry or default_registry()
    try:
        entry = registry.get(data["alpha"])
        if entry.kind != "alpha" and not entry.id.startswith("u_"):          # a generated strategy (code) stands where an alpha does
            raise BadSpec(f"{data['alpha']} is a {entry.kind}: a spec names an alpha")
        params = validate(entry.cls.SCHEMA, {**entry.params, **(data.get("params") or {})})
        entry.cls(**params)                                                # an alpha that refuses its own parameters fails here, not in a test
    except KeyError as exc:
        raise BadSpec(str(exc.args[0])) from None
    except (ParamError, ValueError, TypeError) as exc:
        if isinstance(exc, BadSpec):
            raise
        raise BadSpec(str(exc)) from None
    hypothesis = data.get("hypothesis", "")
    if not isinstance(hypothesis, str) or len(hypothesis) > 2_000:
        raise BadSpec("hypothesis is text of at most 2000 characters")
    tests = data.get("tests") or {}
    if not isinstance(tests, Mapping) or set(tests) - {"signal", "position_size"}:
        raise BadSpec("tests has the keys signal and position_size")
    signal, size = tests.get("signal") or [], tests.get("position_size") or []
    if not isinstance(signal, list) or not isinstance(size, list) or len(signal) > MAX_CASES or len(size) > MAX_CASES:
        raise BadSpec(f"each kind of test is a list of at most {MAX_CASES} cases")
    try:
        chosen = None if data.get("parts") is None else validate_parts(data["parts"])
    except BadStrategy as exc:
        raise BadSpec(str(exc)) from None
    return StrategySpec(strategy_id, line, data["alpha"], params, hypothesis, tuple(_signal_case(c) for c in signal), tuple(_size_case(c) for c in size), chosen)


def default_registry(stages: tuple[str, ...] = ("generated", "owner_reviewed")) -> Registry:
    """The registry of the kit: the built-in alphas and the entries of the strategies folder, and the strategies written as code in the given stages."""
    from hokma.kit.integrity import app_root

    registry = Registry.with_builtins()
    registry.load_dir(app_root() / "kit" / "template" / "service" / "strategies")
    from hokma.factory.registry import add_user_rules

    add_user_rules(registry, stages=stages)                                   # the strategies written as code that are approved and intact
    return registry


def live_registry() -> Registry:
    """The registry that a desk or anything with real money resolves a strategy id from: a strategy written as code is in it only when the owner approved this very code."""
    return default_registry(("owner_reviewed",))


# ---------------------------------------------------------------------------------------------------- SKL-002
def _view(spec: StrategySpec, case: SignalCase, registry: Registry) -> str:
    alpha = registry.build(spec.alpha, spec.params)
    signal = alpha.signal(SignalContext(source="spec_test", desk_id=0, line=spec.line, symbol="TEST", as_of=TEST_TIME, horizon="days",
                                        snapshot_hash="spec_test", facts={k: list(v) for k, v in case.facts.items()}))   # type: ignore[arg-type]
    if signal.abstained or signal.value is None:
        return "abstain"
    return "long" if signal.value > 0 else "short" if signal.value < 0 else "flat"


def _quantity(case: SizeCase) -> Decimal:
    sheet = ContractSheet(case.contract_id, case.asset_type, None, None, case.multiplier, None, None, None, None, None, "USD", "spec_test", TEST_TIME)  # type: ignore[arg-type]
    quantity, _ = to_contracts(Size(SizeType(case.kind), case.value), sheet=sheet, price=case.price, equity=case.equity, stop_distance=case.stop_distance)
    return quantity


def require_tests(spec: StrategySpec, registry: Registry | None = None) -> str:
    """Check that a spec can go to backtest and return its hash (SKL-002).

    No signal test, or no position size test, is `missing_tests` and nothing runs. With both, every test runs first; one that fails is
    `tests_failed` and lists each failure, and the backtest does not run."""
    if spec.alpha.startswith("u_"):                                           # a generated strategy: its approved version and the green run of its own checks stand for the tests
        from hokma.factory import user_rules

        try:
            user_rules.require_green(spec.alpha)
        except user_rules.UserRuleError as exc:
            raise TestsFailed(exc.message) from None
        return spec.spec_hash
    missing = [kind for kind, cases in (("signal", spec.signal_tests), ("position_size", spec.size_tests)) if not cases]
    if missing:
        raise MissingTests(f"the spec {spec.strategy_id} has no {' and no '.join(missing)} test: write them first, see them fail, then write the strategy")
    registry = registry or default_registry()
    failures: list[str] = []
    for case in spec.signal_tests:
        try:
            got = _view(spec, case, registry)
        except Exception as exc:                                          # a test that cannot run is a failed test, with its reason
            failures.append(f"signal test {case.name}: raised {type(exc).__name__}: {str(exc)[:120]}")
            continue
        if got != case.expect:
            failures.append(f"signal test {case.name}: expected {case.expect}, got {got}")
    for case in spec.size_tests:
        try:
            got_qty = _quantity(case)
        except Exception as exc:
            failures.append(f"position size test {case.name}: raised {type(exc).__name__}: {str(exc)[:120]}")
            continue
        if got_qty != case.expect_qty:
            failures.append(f"position size test {case.name}: expected {case.expect_qty}, got {got_qty}")
    if failures:
        raise TestsFailed("the tests failed, so the backtest does not run: " + "; ".join(failures))
    return spec.spec_hash


# ---------------------------------------------------------------------------------------------------- the table
def _row(r: Mapping[str, Any]) -> SpecRow:
    return SpecRow(int(r["id"]), r["strategy_id"], int(r["version"]), r["line"], r["spec_yaml"], r["spec_hash"], r["status"])


def save_draft(text: str, *, root_cause: str | None = None, budget: AttemptBudget | None = None, registry: Registry | None = None,
               db: Database | None = None) -> SpecRow:
    """Save a spec as the next version of its strategy, status `draft`. The same text as the last version is not saved again.

    A new version of a strategy whose attempts are spent is `attempt_budget_spent` unless it carries a `root_cause` (real text of 20 to 4000
    characters), which is recorded on the new row and restarts the count from that moment, anchored on the last run of the table. The count is read in the same transaction as the write
    and holds the failed runs of the table plus the ones that `budget` (the session of Ian) has reported."""
    spec = parse_spec(text, registry)
    cause = None if root_cause is None else clean_root_cause(root_cause)
    with (db or get_database()).write() as u:
        last = u.session.one("SELECT id, strategy_id, version, line, spec_yaml, spec_hash, status FROM strategy_specs WHERE strategy_id = :s "
                             "ORDER BY version DESC LIMIT 1", {"s": spec.strategy_id})
        if last is not None and last["spec_hash"] == spec.spec_hash:
            return _row(last)
        version = 1 if last is None else int(last["version"]) + 1
        if cause is None and last is not None:
            failures = failed_in(u.session, spec.strategy_id, window_start(u.session, spec.strategy_id)) + (0 if budget is None else budget.reported(spec.strategy_id))
            if failures >= (MAX_FAILED_ATTEMPTS if budget is None else budget.limit):
                raise AttemptBudgetSpent(f"{spec.strategy_id} failed {failures} times: save the next version with the root cause (root_cause)")
        new_id = u.session.insert("strategy_specs", {"strategy_id": spec.strategy_id, "version": version, "line": spec.line, "spec_yaml": text,
                                                     "spec_hash": spec.spec_hash, "status": "draft", "proposed_by": None, "root_cause": cause,
                                                     "window_run_id": int(u.session.scalar("SELECT COALESCE(MAX(id), 0) FROM backtest_runs") or 0)})
    if cause is not None and budget is not None:
        budget.restart(spec.strategy_id)
    return SpecRow(new_id, spec.strategy_id, version, spec.line, text, spec.spec_hash, "draft")


def load(strategy_id: str, version: int | None = None, *, db: Database | None = None) -> SpecRow:
    """A saved spec: the given version, or the latest. `bad_spec` when there is none."""
    sql = "SELECT id, strategy_id, version, line, spec_yaml, spec_hash, status FROM strategy_specs WHERE strategy_id = :s"
    params: dict[str, Any] = {"s": strategy_id}
    if version is not None:
        sql, params["v"] = sql + " AND version = :v", version
    with (db or get_database()).read() as u:
        row = u.session.one(sql + " ORDER BY version DESC LIMIT 1", params)
    if row is None:
        raise BadSpec(f"no saved spec for {strategy_id}" + ("" if version is None else f" version {version}"))
    return _row(row)


def gate_backtest(strategy_id: str, version: int, *, registry: Registry | None = None, db: Database | None = None) -> str:
    """The door of every backtest tool of Ian: the saved spec of this version must have its tests, and they must pass. Returns the spec hash."""
    row = load(strategy_id, version, db=db)
    return require_tests(parse_spec(row.spec_yaml, registry), registry)


def propose(strategy_id: str, version: int, *, registry: Registry | None = None, db: Database | None = None) -> SpecRow:
    """Move a draft to `proposed` (the end of Ian's road: the judge of the line takes it from there). Nothing is sent anywhere else.

    The row is read, its tests run again and the status changes inside one transaction, and the update only touches a row that is still a draft."""
    from hokma.factory import user_rules

    try:
        user_rules.propose_gate(strategy_id, db=db)                           # a generated strategy is proposed only after the owner approved its code
    except user_rules.UserRuleError as exc:
        raise BadSpec(exc.message) from None
    with (db or get_database()).write() as u:
        row = u.session.one("SELECT id, strategy_id, version, line, spec_yaml, spec_hash, status FROM strategy_specs WHERE strategy_id = :s AND version = :v",
                            {"s": strategy_id, "v": version})
        if row is None:
            raise BadSpec(f"no saved spec for {strategy_id} version {version}")
        if row["status"] != "draft":
            raise BadSpec(f"only a draft can be proposed, this one is {row['status']}")
        require_tests(parse_spec(row["spec_yaml"], registry), registry)
        changed = u.session.execute("UPDATE strategy_specs SET status = 'proposed', proposed_by = :p WHERE id = :i AND status = 'draft'",
                                    {"p": PROPOSER, "i": row["id"]})
        if changed != 1:
            raise BadSpec("the spec changed while it was being proposed")
    return SpecRow(int(row["id"]), row["strategy_id"], int(row["version"]), row["line"], row["spec_yaml"], row["spec_hash"], "proposed")
