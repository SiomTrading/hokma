"""Ian, the strategy researcher (SPEC-020: SKL-001, SKL-003, section 4.10): the report he hands back, his tools and two safety checks.

`claim_guard` refuses an output that says a backtest finished, or that tests pass, without a run of this same session that really ended in
state done. `attempt_budget` stops a specification after three failed attempts and demands the root cause in writing. `ResearchRun` puts both
in front of the tools: only the tools of the list below exist for Ian, the backtest tools run only for a spec whose tests passed, and every
run id the tools hand back is remembered as fresh. Nothing here sends an order, reaches a desk or talks to a broker.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from hokma.db import Database, get_database
from hokma.llm.jsonx import LLMParseError, extract_json
from hokma.quant import limits

MCP_PREFIX = "mcp__hokma__"
AGENT_SLUG = "strategy-researcher"
DEFAULT_MODEL = "opus"                      # the large model; the token budget is the one of the service (desks.token_budget)
DEFAULT_TIER = "deep"

ENGINE_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob", "Agent")      # he reads files, never writes one (no Write, Edit or Bash), and launches Sam and Ben
TOOLS: tuple[str, ...] = (
    "factory_list", "factory_describe", "factory_draft", "factory_propose",
    "source_ingest", "bank_create", "bank_list", "bank_get", "bank_set_status", "strategy_brief_write", "strategy_code_list", "strategy_code_get",
    "backtest_run", "backtest_status", "backtest_result", "backtest_optimize", "backtest_walkforward", "backtest_montecarlo", "backtest_evidence",
    "quant_call", "data_bars", "data_snapshot", "data_papers",
    "optimizer_verdict", "optimizer_verdict_get", "audit_thresholds_get", "library_list", "library_request",      # the verdict of the optimizer (SPEC-032): the limits are read here and edited only by the owner
    "task_show", "task_list", "task_claim", "task_heartbeat", "task_complete", "task_block", "task_comment", "journal_write", "journal_search", "ips_read",      # his tasks, the journal and the policy he reads (1.2.2)
)
RUN_TOOLS: tuple[str, ...] = ("backtest_run", "backtest_optimize", "backtest_walkforward", "backtest_montecarlo")
VERSIONED_TOOLS: tuple[str, ...] = (*RUN_TOOLS, "backtest_evidence")      # the tools that work on a version: only the latest saved one
FORBIDDEN_WORDS: tuple[str, ...] = ("exec", "order", "desk", "broker", "submit", "approve", "flatten", "premium_calibrate")

MAX_FAILED_ATTEMPTS = 3
MIN_ROOT_CAUSE_CHARS = 40
MAX_TEXT = 4_000
MAX_ITEMS = 50
STATUSES = ("in_progress", "proposed", "blocked")
BANK_STATUSES = ("idea", "draft", "testing", "ready", "not_working", "live", "off")      # the states of the bank (SPEC-030); the report names the one the bank gave
CLAIM_KINDS = ("backtest_done", "tests_pass")
ERROR_CODES = ("missing_tests", "attempt_budget_spent", "evaluator_changed", "stale_version")


class ResearchError(ValueError):
    """Base of the errors of the researcher. `code` is stable and is what the tools answer."""

    code = "research_error"


class MissingTests(ResearchError):
    code = "missing_tests"


class TestsFailed(ResearchError):
    code = "tests_failed"
    __test__ = False                      # a name that starts with Test is not a test class for pytest


class AttemptBudgetSpent(ResearchError):
    code = "attempt_budget_spent"


class StaleVersion(ResearchError):
    code = "stale_version"


class EvaluatorChanged(ResearchError):
    code = "evaluator_changed"


class ToolNotAllowed(ResearchError):
    code = "tool_not_allowed"


class BadReport(ResearchError):
    code = "bad_report"


class ClaimRejected(ResearchError):
    """The output claims more than the runs of this session show. `code` is the one of the first problem."""

    def __init__(self, problems: Sequence["Problem"]) -> None:
        self.problems = tuple(problems)
        self.code = self.problems[0].code if self.problems else "claim_rejected"
        super().__init__("; ".join(f"{p.code}: {p.detail}" for p in self.problems))


# ---------------------------------------------------------------------------------------------------- the report
@dataclass(frozen=True)
class SpecRef:
    strategy_id: str
    version: int
    spec_hash: str


@dataclass(frozen=True)
class RunRef:
    run_id: int
    kind: str


@dataclass(frozen=True)
class BankRef:
    """The strategy of the bank that this work produced: which one, which version and the state the bank gave it."""

    bank_id: int
    version: int
    status: str


@dataclass(frozen=True)
class Finding:
    """One thing Ian found. A number in `claim` or `value` needs a source: the run it came from or the quant module that computed it."""

    claim: str
    value: float | str | None = None
    run_id: int | None = None
    module: str | None = None


@dataclass(frozen=True)
class Claim:
    """A statement of completion: a backtest finished (with its run) or the tests of a spec pass (with its hash)."""

    kind: Literal["backtest_done", "tests_pass"]
    run_id: int | None = None
    spec_hash: str | None = None


@dataclass(frozen=True)
class StrategyReport:
    hypothesis: str
    spec: SpecRef | None
    runs: tuple[RunRef, ...]
    evidence: tuple[Finding, ...]
    risks: tuple[str, ...]
    next_step: str
    status: Literal["in_progress", "proposed", "blocked"] = "in_progress"
    claims: tuple[Claim, ...] = ()
    root_cause: str | None = None
    bank: BankRef | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "StrategyReport":
        """The report from a decoded JSON object. Unknown keys, wrong types and oversized text are `bad_report`."""
        allowed = {"hypothesis", "spec", "runs", "evidence", "risks", "next_step", "status", "claims", "root_cause", "bank"}
        if not isinstance(data, Mapping) or set(data) - allowed:
            raise BadReport(f"the report is an object with the keys {sorted(allowed)}")
        for key in ("hypothesis", "next_step"):
            _text(key, data.get(key), required=True)
        if data.get("status", "in_progress") not in STATUSES:
            raise BadReport(f"status is one of {', '.join(STATUSES)}")
        if data.get("root_cause") is not None:
            _text("root_cause", data["root_cause"], required=True)
        try:
            spec = None if data.get("spec") is None else SpecRef(**_only(data["spec"], ("strategy_id", "version", "spec_hash"), "spec"))
            bank = None if data.get("bank") is None else BankRef(**_only(data["bank"], ("bank_id", "version", "status"), "bank"))
            runs = tuple(RunRef(**_only(r, ("run_id", "kind"), "runs")) for r in _items(data, "runs"))
            evidence = tuple(Finding(**_only(f, ("claim", "value", "run_id", "module"), "evidence", optional=("value", "run_id", "module")))
                             for f in _items(data, "evidence"))
            claims = tuple(Claim(**_only(c, ("kind", "run_id", "spec_hash"), "claims", optional=("run_id", "spec_hash"))) for c in _items(data, "claims"))
        except TypeError as exc:
            raise BadReport(f"a part of the report has the wrong fields: {exc}") from None
        risks = tuple(_text("risks", r, required=True) for r in _items(data, "risks"))
        if spec is not None and (not isinstance(spec.version, int) or isinstance(spec.version, bool)):
            raise BadReport("spec.version is a whole number")
        if bank is not None:
            for number in (bank.bank_id, bank.version):
                if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                    raise BadReport("bank: bank_id and version are whole numbers from 1")
            if bank.status not in BANK_STATUSES:
                raise BadReport(f"bank.status is one of {', '.join(BANK_STATUSES)}")
        for run in runs:
            _run_id("runs", run.run_id)
        for finding in evidence:
            _text("evidence.claim", finding.claim, required=True)
            if finding.run_id is not None:
                _run_id("evidence", finding.run_id)
        for claim in claims:
            if claim.kind not in CLAIM_KINDS:
                raise BadReport(f"a claim is one of {', '.join(CLAIM_KINDS)}")
            if claim.run_id is not None:
                _run_id("claims", claim.run_id)
        return cls(data["hypothesis"], spec, runs, evidence, risks, data["next_step"], data.get("status", "in_progress"), claims,
                   data.get("root_cause"), bank)

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


def _only(value: Any, keys: tuple[str, ...], where: str, optional: tuple[str, ...] = ()) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - set(keys) or any(k not in value for k in keys if k not in optional):
        raise BadReport(f"{where}: each item is an object with the keys {', '.join(keys)}")
    return dict(value)


def _items(data: Mapping[str, Any], key: str) -> list[Any]:
    items = data.get(key, [])
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise BadReport(f"{key} is a list of at most {MAX_ITEMS} items")
    return items


def _text(name: str, value: Any, *, required: bool = False) -> str:
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > MAX_TEXT:
        raise BadReport(f"{name} is text of 1 to {MAX_TEXT} characters")
    return value


def _run_id(where: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BadReport(f"{where}: a run id is a whole number from 1")
    return value


def parse_report(text: str) -> StrategyReport:
    """The report that a model wrote: the first JSON object of its answer, checked. No JSON is `bad_report`."""
    try:
        data = extract_json(text)
    except LLMParseError:
        raise BadReport("the answer holds no JSON object") from None
    return StrategyReport.from_mapping(data)


# ---------------------------------------------------------------------------------------------------- SKL-001
class RunLedger:
    """The runs that this session started, by id with the last status it saw. An id that is not here is stale: it came from an earlier session."""

    def __init__(self) -> None:
        self._runs: dict[int, str] = {}
        self._tests: set[str] = set()

    def start(self, run_id: int, status: str = "queued") -> None:
        self._runs[_run_id("run ledger", run_id)] = status

    def update(self, run_id: int, status: str) -> None:
        if run_id in self._runs:
            self._runs[run_id] = status

    def status(self, run_id: int) -> str | None:
        return self._runs.get(run_id)

    def tests_passed(self, spec_hash: str) -> None:
        self._tests.add(spec_hash)

    def has_tests(self, spec_hash: str) -> bool:
        return spec_hash in self._tests


@dataclass(frozen=True)
class Problem:
    code: str
    detail: str


@dataclass(frozen=True)
class Verdict:
    valid: bool
    problems: tuple[Problem, ...] = ()

    def raise_for_problems(self) -> None:
        if not self.valid:
            raise ClaimRejected(self.problems)


_NOT_BEFORE = r"(?<!\bno\s)(?<!\bnot\s)(?<!n't\s)(?<!\bnever\s)"          # a negation right before the first term
_GAP = r"(?:(?!\bnot\b|n't\b|\bnever\b|\bfail\w*)[^.\n])"      # the words between two terms hold no negation: "did not finish" claims nothing
_FINISHED = re.compile(rf"{_NOT_BEFORE}\b(?:backtests?|back-tests?|runs?|optimi[sz]ations?|walk-?forward|monte carlo|simulations?)\b{_GAP}{{0,60}}?"
                       rf"\b(?:finished|completed|complete|done|ready|succeeded)\b|"
                       rf"{_NOT_BEFORE}\b(?:finished|completed|ran)\b{_GAP}{{0,40}}?\b(?:backtests?|runs?|simulations?)\b", re.I)
_TESTS_PASS = re.compile(rf"{_NOT_BEFORE}\btests?\b{_GAP}{{0,40}}?\b(?:pass|passed|passing|green)\b", re.I)
_DIGIT = re.compile(r"\d")


def db_status_of(run_id: int, *, db: Database | None = None) -> str | None:
    """The status of a run in the table of runs, or None when there is no such run."""
    with (db or get_database()).read() as u:
        row = u.session.one("SELECT status FROM backtest_runs WHERE id = :i", {"i": run_id})
    return None if row is None else str(row["status"])


def claim_guard(report: StrategyReport, ledger: RunLedger, *, status_of: Callable[[int], str | None] | None = None,
                budget: "AttemptBudget | None" = None) -> Verdict:
    """Whether the report claims only what this session proved (SKL-001).

    A run id counts when this session started it and its status now is done: a run id from an earlier session is `stale_run_id` even if that run
    is done. Saying a backtest finished needs one such run in the report; saying tests pass needs the tests of the spec to have run in this
    session. A number in a finding needs its run or its quant module. A blocked spec needs the root cause written."""
    read = status_of or db_status_of
    problems: list[Problem] = []
    cited = {r.run_id for r in report.runs} | {f.run_id for f in report.evidence if f.run_id is not None} | {c.run_id for c in report.claims if c.run_id is not None}
    good: set[int] = set()
    for run_id in sorted(cited):
        if ledger.status(run_id) is None:
            problems.append(Problem("stale_run_id", f"run {run_id} was not started in this session"))
        elif (state := read(run_id)) != "done":
            problems.append(Problem("run_not_done", f"run {run_id} is {state or 'unknown'}, not done"))
        else:
            good.add(run_id)
    listed = {r.run_id for r in report.runs} & good
    texts = [report.hypothesis, report.next_step, *(f.claim for f in report.evidence)]
    says_finished = any(_FINISHED.search(t) for t in texts) or any(c.kind == "backtest_done" for c in report.claims)
    if says_finished and not (listed or {c.run_id for c in report.claims if c.kind == "backtest_done"} & good):
        problems.append(Problem("unbacked_claim", "the report says a backtest finished and names no run of this session in state done"))
    for claim in report.claims:
        if claim.kind == "backtest_done" and (claim.run_id is None or claim.run_id not in good):
            problems.append(Problem("unbacked_claim", "a backtest_done claim needs the run id of a run of this session in state done"))
        if claim.kind == "tests_pass" and not (claim.spec_hash and ledger.has_tests(claim.spec_hash)):
            problems.append(Problem("unbacked_claim", "a tests_pass claim needs the hash of a spec whose tests ran in this session"))
    if any(_TESTS_PASS.search(t) for t in texts) and not (report.spec and ledger.has_tests(report.spec.spec_hash)) \
            and not any(c.kind == "tests_pass" for c in report.claims):
        problems.append(Problem("unbacked_claim", "the report says tests pass and the tests of the spec did not run in this session"))
    for finding in report.evidence:
        numeric = isinstance(finding.value, (int, float)) and not isinstance(finding.value, bool)
        if (numeric or _DIGIT.search(finding.claim)) and finding.run_id is None and not finding.module:
            problems.append(Problem("number_without_source", f"{finding.claim[:60]!r} has a number and no run or module behind it"))
    if report.spec is not None and (report.status == "blocked" or (budget is not None and budget.spent(report.spec.strategy_id))):
        if len((report.root_cause or "").strip()) < MIN_ROOT_CAUSE_CHARS:
            problems.append(Problem("root_cause_required", f"write the root cause in at least {MIN_ROOT_CAUSE_CHARS} characters before the spec is touched again"))
    elif report.status == "blocked" and len((report.root_cause or "").strip()) < MIN_ROOT_CAUSE_CHARS:
        problems.append(Problem("root_cause_required", f"a blocked report carries the root cause in at least {MIN_ROOT_CAUSE_CHARS} characters"))
    return Verdict(not problems, tuple(problems))


# ---------------------------------------------------------------------------------------------------- SKL-003
MIN_ROOT_CAUSE_RECORD = 20            # the shortest root cause that a new version may record (the column of the table has the same bounds)
MAX_ROOT_CAUSE_RECORD = 4_000


def clean_root_cause(text: Any) -> str:
    """The root cause to record on a version: real text of 20 to 4000 characters once trimmed. Anything else is `bad_report`."""
    if not isinstance(text, str) or not MIN_ROOT_CAUSE_RECORD <= len(text.strip()) <= MAX_ROOT_CAUSE_RECORD:
        raise BadReport(f"a root cause is text of {MIN_ROOT_CAUSE_RECORD} to {MAX_ROOT_CAUSE_RECORD} characters")
    return text.strip()


def window_start(session: Any, strategy_id: str) -> int:
    """The id of the last run of the table when the window opened: the save of the latest version that carries a recorded root cause, or 0 (every run)
    when none does. The anchor is written by `save_draft` from the table of runs itself, so no agent value decides it."""
    return int(session.scalar("SELECT window_run_id FROM strategy_specs WHERE strategy_id = :s AND root_cause IS NOT NULL ORDER BY id DESC LIMIT 1",
                              {"s": strategy_id}) or 0)


def failed_in(session: Any, strategy_id: str, since: int = 0) -> int:
    """The failed runs of a strategy created after the run with id `since`, whatever version number they carry, through an open session."""
    return int(session.scalar("SELECT COUNT(*) FROM backtest_runs WHERE strategy_id = :s AND status = 'failed' AND id > :i", {"s": strategy_id, "i": since}) or 0)


def failed_runs(strategy_id: str, since: int = 0, *, db: Database | None = None) -> int:
    """The failed runs of a strategy created after the run with id `since`, counted in the table of runs, which no agent can edit."""
    with (db or get_database()).read() as u:
        return failed_in(u.session, strategy_id, since)


def budget_start(strategy_id: str, *, db: Database | None = None) -> int:
    """The anchor of the window (`window_start`) read from the table of specs."""
    with (db or get_database()).read() as u:
        return window_start(u.session, strategy_id)


class AttemptBudget:
    """Three failed attempts on a strategy block it (SKL-003). The fourth is `attempt_budget_spent` and the output must carry the root cause.

    The attempts are counted per strategy across all its versions, where nobody can lower them (failed rows of `backtest_runs`), plus the ones
    reported with `fail` for a result that ran but was absurd. A new version does not reset the count: only a version saved with a recorded root
    cause does, and from the moment of that save forward: a run counts by its place in the table of runs, not by the version number it carries.
    The failures before it stay in the table and in the report."""

    def __init__(self, count: Callable[[str, int], int] | None = None, *, limit: int = MAX_FAILED_ATTEMPTS,
                 start: Callable[[str], int] | None = None, db: Database | None = None) -> None:
        if limit < 1:
            raise ValueError("the limit is at least 1")
        self.limit = limit
        self._count = count or (lambda strategy_id, since: failed_runs(strategy_id, since, db=db))
        self._start = start or ((lambda strategy_id: budget_start(strategy_id, db=db)) if count is None else (lambda _strategy_id: 0))
        self._reported: dict[tuple[str, int], int] = {}

    def fail(self, strategy_id: str, version: int) -> None:
        """One more failed attempt that the table of runs cannot show, such as a result that makes no sense."""
        key = (strategy_id, version)
        self._reported[key] = self._reported.get(key, 0) + 1

    def reported(self, strategy_id: str) -> int:
        """The failures reported with `fail` since the window of this strategy opened."""
        return sum(n for (s, _v), n in self._reported.items() if s == strategy_id)

    def restart(self, strategy_id: str) -> None:
        """The window of a strategy opened (a version saved with its root cause): the failures reported until now stop counting."""
        for key in [k for k in self._reported if k[0] == strategy_id]:
            del self._reported[key]

    def attempts(self, strategy_id: str) -> int:
        since = int(self._start(strategy_id))
        return int(self._count(strategy_id, since)) + self.reported(strategy_id)

    def spent(self, strategy_id: str) -> bool:
        return self.attempts(strategy_id) >= self.limit

    def check(self, strategy_id: str) -> None:
        if self.spent(strategy_id):
            raise AttemptBudgetSpent(f"{strategy_id} failed {self.attempts(strategy_id)} times: write the root cause in the report (root_cause) "
                                     f"and save the next version with it before any new attempt on this strategy")


def attempt_budget(count: Callable[[str, int], int] | None = None, *, limit: int = MAX_FAILED_ATTEMPTS,
                   start: Callable[[str], int] | None = None, db: Database | None = None) -> AttemptBudget:
    """The budget of attempts of a session (see `AttemptBudget`)."""
    return AttemptBudget(count, limit=limit, start=start, db=db)


# ---------------------------------------------------------------------------------------------------- the guarded tools
Handler = Callable[..., Any]
Gate = Callable[[str, int], str]


def _latest_version(strategy_id: str) -> int:
    from hokma.factory import spec

    return spec.load(strategy_id).version


def _default_gate(strategy_id: str, version: int) -> str:
    from hokma.factory import spec

    return spec.gate_backtest(strategy_id, version)


@dataclass
class ResearchRun:
    """One session of Ian: the tools he may call, in front of their handlers.

    A name outside `TOOLS` is refused (an order tool does not exist for him). A backtest tool needs the budget of attempts, sizes inside the limits
    of `hokma.quant.limits` and a spec whose signal and position size tests passed just now (`gate`, which runs them). The run id that a tool hands back
    becomes fresh for `claim_guard`. `accept` is the last step: the text he wrote must be a valid report that the runs back up."""

    handlers: Mapping[str, Handler]
    ledger: RunLedger = field(default_factory=RunLedger)
    budget: AttemptBudget = field(default_factory=AttemptBudget)
    status_of: Callable[[int], str | None] | None = None
    gate: Gate = _default_gate
    latest: Callable[[str], int] = _latest_version

    def tool(self, name: str, **args: Any) -> Any:
        if name not in TOOLS:
            raise ToolNotAllowed(f"{name} is not a tool of the strategy researcher")
        handler = self.handlers.get(name)
        if handler is None:
            raise ToolNotAllowed(f"{name} is not connected in this session")
        if name in VERSIONED_TOOLS:
            strategy_id, version = args.get("strategy_id"), args.get("version")
            if not isinstance(strategy_id, str) or isinstance(version, bool) or not isinstance(version, int):
                raise ResearchError("a backtest tool needs the strategy_id and the version of a saved spec")
            if (newest := self.latest(strategy_id)) != version:
                raise StaleVersion(f"{strategy_id} version {version} is not the latest saved version ({newest}): run only the latest")
        if name in RUN_TOOLS:
            limits.check_inputs({k: v for k, v in args.items() if isinstance(v, (int, float, list, tuple)) and not isinstance(v, bool)})
            self.budget.check(strategy_id)
            self.ledger.tests_passed(self.gate(strategy_id, version))
        result = handler(**args)
        run_id = result.get("run_id") if isinstance(result, Mapping) else None
        if isinstance(run_id, int) and not isinstance(run_id, bool):
            if name in RUN_TOOLS:
                self.ledger.start(run_id, str(result.get("status", "queued")))
            elif "status" in result:
                self.ledger.update(run_id, str(result["status"]))
        return result

    def accept(self, text: str) -> StrategyReport:
        """The report of the answer, or `ClaimRejected` (with its stable code) when the runs of this session do not back it."""
        report = parse_report(text)
        claim_guard(report, self.ledger, status_of=self.status_of, budget=self.budget).raise_for_problems()
        return report
