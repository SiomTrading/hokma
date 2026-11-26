"""The nine questions to the owner that the run cannot answer by itself (SPEC-032: 4.2 and 5.3).

Nine checks of the audit need a fact that no backtest holds: whether the data was point in time, whether the universe kept the names that died, whether a
holdout was kept. Each one is a question here. A question has a scope: `data` answers belong to the contract the strategy trades (so they serve every
other strategy of the same contract) and `strategy` answers belong to one version of one strategy.

A question is in one of four states for a strategy: `ask` (nobody answered; a suggestion may be shown), `answered` (the owner said yes, no or na),
`na` (the rule of 5.3 says it does not apply to this strategy: it is not asked) or `auto`. The answers are rows of `audit_answers`, which accepts inserts only:
the answer in force is the last row of (scope, subject, question), and changing an answer is another row. A suggestion is only shown; it becomes an
answer when the owner confirms it. Nothing here calculates a statistic: it reads the saved spec of the strategy (`factory.spec`, read only) and the saved
run of H4, and it never writes anything but the answers. The actor of an answer comes from the code that calls (the route puts 'owner'), never from a body.

`evidence_fields` turns the states into the `AuditEvidence` fields: yes is True, no is False, and na or ask is None (the audit reads None as unknown; the
verdict decides what to do with a check that does not apply).
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from hokma.db import Database, get_database
from hokma.db.timeutil import from_db_time
from hokma.factory import spec as factory_spec

ANSWERS = ("yes", "no", "na")
MAX_NOTE = 1_000
# Facts that an alpha reads and that are not made of the bars (fundamentals, ranks, options): a strategy that reads one is not "prices and volume only".
OTHER_SOURCE_FACTS = ("iv_rv_ratio", "shares_held")
_ACTOR = re.compile(r"^[a-z][a-z0-9_:-]{0,79}$")
_KEY = re.compile(r"^[a-z][a-z_]{0,39}$")


class QuestionError(ValueError):
    """The questions of a strategy cannot be read or answered. `code` is stable: bad_input, no_spec (no saved spec of that version) or no_run (it has no finished run)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class Question:
    key: str                         # section 5.3
    scope: Literal["strategy", "data"]
    evidence_field: str              # the AuditEvidence field it fills
    check: int


QUESTIONS: tuple[Question, ...] = (
    Question("point_in_time", "data", "point_in_time", 1),
    Question("survivorship_free", "data", "survivorship_free", 2),
    Question("corporate_actions", "data", "corporate_actions", 3),
    Question("gaps_checked", "data", "gaps_checked", 5),
    Question("in_sample_tuning", "strategy", "in_sample_tuning", 7),
    Question("holdout_untouched", "strategy", "holdout_untouched", 8),
    Question("impact_modeled", "strategy", "impact_modeled", 11),
    Question("borrow_costs_modeled", "strategy", "borrow_costs_modeled", 12),
    Question("capacity_checked", "strategy", "capacity_checked", 14),
)
_BY_KEY = {q.key: q for q in QUESTIONS}


@dataclass(frozen=True)
class QuestionState:
    key: str
    subject: str                     # strategy: "{strategy_id}@{version}"; data: the contract id
    status: Literal["ask", "answered", "na", "auto"]
    suggestion: Literal["yes", "no"] | None
    suggestion_reason: str | None    # a message key: verdict.suggest.{question}.{reason}
    answer: Literal["yes", "no", "na"] | None
    answered_at: datetime | None
    answer_id: int | None
    note: str | None = None
    scope: Literal["strategy", "data"] = "strategy"
    na_reason: str | None = None     # when status is na: a message key, verdict.na.{reason}


# ---- what the saved spec and the saved run say ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Context:
    strategy_id: str
    version: int
    strategy_subject: str
    run_id: int
    backtest: Any                    # the BacktestSpec of the latest finished run
    alpha_facts: tuple[str, ...] | None
    shorts: bool
    walk_forward_choice: Mapping[str, Any] | None


def _database(db: Database | None, services: Any) -> Database:
    if db is not None:
        return db
    return services.database() if services is not None else get_database()


def _check_ids(strategy_id: Any, version: Any) -> None:
    if not isinstance(strategy_id, str) or not 1 <= len(strategy_id) <= 80:
        raise QuestionError("bad_input", "strategy_id is text of 1 to 80 characters")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise QuestionError("bad_input", "version is a whole number of 1 or more")


def _context(strategy_id: str, version: int, db: Database) -> _Context:
    from hokma.backtest import results, runner
    from hokma.factory.researcher import ResearchError

    _check_ids(strategy_id, version)
    try:
        row = factory_spec.load(strategy_id, version, db=db)
        parsed = factory_spec.parse_spec(row.spec_yaml)
    except ResearchError as exc:
        raise QuestionError("no_spec", str(exc)[:300]) from exc
    with db.read() as u:
        run = u.session.one("SELECT id, spec_json FROM backtest_runs WHERE kind = 'run' AND status IN ('done', 'account_blown') AND strategy_id = :s "
                            "AND strategy_version = :v AND parent_id IS NULL ORDER BY id DESC LIMIT 1", {"s": strategy_id, "v": version})
        walk = u.session.one("SELECT summary_json FROM backtest_runs WHERE kind = 'walkforward' AND status IN ('done', 'account_blown') AND strategy_id = :s "
                             "AND strategy_version = :v ORDER BY id DESC LIMIT 1", {"s": strategy_id, "v": version})
    if run is None:
        raise QuestionError("no_run", f"{strategy_id} version {version} has no finished run: run it first")
    backtest = runner.spec_from_canonical(json.loads(run["spec_json"]))
    facts = getattr(factory_spec.default_registry().get(parsed.alpha).cls, "FACTS", None)
    chosen = None
    if walk is not None and walk["summary_json"]:
        windows = (json.loads(walk["summary_json"]).get("result") or {}).get("windows") or []
        chosen = windows[-1].get("chosen") if windows else None
    shorts = any(t.side == "short" for t in results.load_trades(db, int(run["id"])))
    return _Context(strategy_id, version, f"{strategy_id}@{version}", int(run["id"]), backtest, tuple(facts) if isinstance(facts, tuple) else None, shorts, chosen)


def _not_applicable(question: Question, ctx: _Context) -> str | None:
    """The reason (a message key suffix) why the question does not apply to this strategy, or None when it does."""
    if question.key == "survivorship_free" and len(ctx.backtest.instruments) == 1:
        return "single_instrument"
    if question.key == "impact_modeled" and ctx.backtest.costs.impact != "none":
        return "impact_in_spec"
    if question.key == "borrow_costs_modeled" and not ctx.shorts and not ctx.backtest.costs.cfd_swap:
        return "no_shorts_no_financing"
    return None


def _suggestion(question: Question, subject: str, ctx: _Context, services: Any) -> tuple[str | None, str | None]:
    """A suggestion that is only shown: (yes or no, reason key suffix), or (None, None)."""
    key = question.key
    if key == "point_in_time":
        from hokma.factory.fundamental_facts import RANK_FACTS, VALUE_FACTS

        if ctx.alpha_facts is not None and not set(ctx.alpha_facts) & {*VALUE_FACTS, *RANK_FACTS, *OTHER_SOURCE_FACTS}:
            return "yes", "prices_only"
    elif key == "corporate_actions":
        if subject.startswith("CONT:"):
            return "yes", "continuous_series"
    elif key == "gaps_checked" and services is not None and len(ctx.backtest.instruments) == 1:
        from hokma.backtest.runner import StageError
        from hokma.data.quality import inspect_bar_quality

        try:
            data = services.load(ctx.backtest)
        except (StageError, ValueError, LookupError, OSError):               # the bars cannot be read now (no connection, no data): the question stays open with no suggestion
            return None, None
        quality = inspect_bar_quality([{"date": b.ts.isoformat(), "open": b.open, "high": b.high, "low": b.low, "close": b.close} for b in data.bars])
        if quality.excluded_rows == 0:
            return "yes", "no_bars_excluded"
    elif key == "in_sample_tuning" and ctx.walk_forward_choice:
        if all(ctx.backtest.params.get(name) == value for name, value in ctx.walk_forward_choice.items()):
            return "yes", "walk_forward_choice"
    return None, None


# ---- answers --------------------------------------------------------------------------------------------------------------------------

def _when(value: Any) -> datetime:
    return from_db_time(value) if isinstance(value, str) else value


def _subjects(question: Question, ctx: _Context) -> tuple[str, ...]:
    return ctx.backtest.instruments if question.scope == "data" else (ctx.strategy_subject,)


def _last(db: Database, question: Question, subject: str) -> Mapping[str, Any] | None:
    with db.read() as u:
        return u.session.one("SELECT id, answer, note, created_at FROM audit_answers WHERE scope = :s AND subject = :j AND question_key = :k "
                             "ORDER BY id DESC LIMIT 1", {"s": question.scope, "j": subject, "k": question.key})


def _state(question: Question, subject: str, ctx: _Context, db: Database, services: Any) -> QuestionState:
    reason = _not_applicable(question, ctx)
    if reason is not None:
        return QuestionState(question.key, subject, "na", None, None, None, None, None, None, question.scope, reason)
    last = _last(db, question, subject)
    if last is not None:
        return QuestionState(question.key, subject, "answered", None, None, last["answer"], _when(last["created_at"]), int(last["id"]), last["note"], question.scope)
    suggested, why = _suggestion(question, subject, ctx, services)
    return QuestionState(question.key, subject, "ask", suggested, None if why is None else f"verdict.suggest.{question.key}.{why}", None, None, None, None, question.scope)


def states(strategy_id: str, version: int, *, services: Any = None) -> list[QuestionState]:
    """The state of every question for a version of a strategy, in the order of `QUESTIONS`; a data question has one state for each contract the run trades.

    `services` is the `Services` of the backtest: it gives the database and, for the question of the data gaps, the bars of the run. Without it
    the default database is read and that suggestion is not made. `no_spec` and `no_run` when the strategy has no saved spec or no finished run."""
    db = _database(None, services)
    ctx = _context(strategy_id, version, db)
    return [_state(q, subject, ctx, db, services) for q in QUESTIONS for subject in _subjects(q, ctx)]


def answer(strategy_id: str, version: int, key: str, value: str, *, note: str | None, actor: str, db: Database | None = None) -> QuestionState:
    """Record the answer of the owner: a new row, never a change of an old one. A data answer is written for each contract of the run, so it serves every
    strategy of those contracts; a strategy answer is for this version only. The same answer and note that are already in force write nothing.

    Returns the state of the question (for the first contract when there are several). `bad_input` for an unknown question, a value that is not yes, no
    or na, a long note, an actor that is not a name given by the code, or a question that does not apply to this strategy."""
    if not isinstance(key, str) or key not in _BY_KEY:
        raise QuestionError("bad_input", f"key is one of {', '.join(q.key for q in QUESTIONS)}")
    if not isinstance(value, str) or value not in ANSWERS:
        raise QuestionError("bad_input", f"value is one of {', '.join(ANSWERS)}")
    if note is not None and (not isinstance(note, str) or len(note.strip()) > MAX_NOTE):
        raise QuestionError("bad_input", f"note is text of at most {MAX_NOTE} characters")
    if not isinstance(actor, str) or not _ACTOR.match(actor):
        raise QuestionError("bad_input", "actor is a lowercase name given by the code that calls")
    note = (note or "").strip() or None
    database = _database(db, None)
    ctx = _context(strategy_id, version, database)
    question = _BY_KEY[key]
    reason = _not_applicable(question, ctx)
    if reason is not None:
        raise QuestionError("bad_input", f"the question {key} does not apply to this strategy ({reason})")
    with database.write() as u:
        for subject in _subjects(question, ctx):
            last = u.session.one("SELECT answer, note FROM audit_answers WHERE scope = :s AND subject = :j AND question_key = :k ORDER BY id DESC LIMIT 1",
                                 {"s": question.scope, "j": subject, "k": key})
            if last is None or (last["answer"], last["note"]) != (value, note):
                u.session.insert("audit_answers", {"scope": question.scope, "subject": subject, "question_key": key, "answer": value, "note": note,
                                                   "answered_by": actor})
    return _state(question, _subjects(question, ctx)[0], ctx, database, None)


def evidence_fields(states: Sequence[QuestionState]) -> dict[str, bool | None]:
    """The `AuditEvidence` fields of the nine questions: yes is True, no is False, na and ask are None.

    A question with several contracts is False when any answer is no, None while any is still asked, True when all of those that apply are yes."""
    out: dict[str, bool | None] = {}
    for question in QUESTIONS:
        mine = [s for s in states if s.key == question.key]
        answers = [s.answer for s in mine if s.status == "answered" and s.answer != "na"]
        if "no" in answers:
            out[question.evidence_field] = False
        elif any(s.status == "ask" for s in mine) or not answers:
            out[question.evidence_field] = None
        else:
            out[question.evidence_field] = True
    return out
