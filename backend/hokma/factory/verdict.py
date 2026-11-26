"""The verdict of the optimizer: a suggestion about a version of a strategy, never a gate (SPEC-032: 4.4, 5.2, 5.5, 5.6, 5.7, 8 and 9.2).

`evaluate` joins what the other modules give: the limits of the line (`thresholds`), the questions to the owner (`questions`), the facts of H4 (`evidence_bridge`)
and the 24 checks (`audit`). Its result is one row of `strategy_verdicts`, which accepts inserts only: taking a check out, putting it back and recalculating are
new rows that point at the one they come from (`parent_id`). The audit verdict (`audit_verdict`) is the one of the audit before the owner took anything out and
never changes; `verdict` is the same rule (`audit.verdict_for`) over the checks that are left.

Taking a check out or putting it back (`exclude`, `include`) runs nothing: it reads the report of the parent and applies `verdict_for` to what is left.
`recompute` reuses the evidence that was saved; it runs the engine only for the neighbors of a new spread. Checks that do not apply (a question that the owner
answered "does not apply", or that the rules of `questions` say does not apply to this strategy) enter as exclusions made by `system`, with their reason.

The three sentences, the changes suggested to the creator and the table of section 8 are data: each sentence is a message key with parameters and each change an
`action` code, rendered from `catalog/es.json` and `catalog/en.json` in the language of the reader. No number is written by an agent. The ideal result is the
centre of the widest plateau of the last optimization, adjusted to the grid, and only when it holds its neighbors and the walk forward.

Nothing in Hokma reads `strategy_verdicts.verdict` to stop an action (C1): the verdict is shown, and SPEC-030 reads it as advice. No road to a broker lives here.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from hokma import i18n
from hokma.db import Database, get_database
from hokma.db.timeutil import from_db_time
from hokma.factory import audit, evidence_bridge, questions, thresholds
from hokma.factory.questions import QuestionState

SEVERITY = ("fail", "unknown", "warn", "pass", "na")
VERDICTS = ("go", "review", "needs_evidence", "no_go")
CAUSES = ("evaluate", "exclude", "include", "thresholds", "answers")
SYSTEM = "system"
MAX_REASON = 500
IDEAL_REASONS = ("plateau_fails_neighbors", "walk_forward_fails", "no_optimization", "plateaus_unavailable")
_ACTOR = re.compile(r"^[a-z][a-z0-9_:-]{0,79}$")
_CODE = re.compile(r"^([a-z][a-z_]*):")
_PERCENT = re.compile(r"(\d+(?:\.\d+)?%)")
_NUMBER = re.compile(r"(\d+(?:\.\d+)?)")
CHECK_BY_KEY = {c.key: c for c in audit.CHECKLIST}
KEY_OF_CHECK = {c.number: c.key for c in audit.CHECKLIST}
QUESTION_OF_CHECK = {q.check: q.key for q in questions.QUESTIONS}
CHECK_OF_QUESTION = {q.key: KEY_OF_CHECK[q.check] for q in questions.QUESTIONS}


class VerdictError(ValueError):
    """The verdict cannot be made or changed. `code` is stable: bad_input or no_verdict (there is no verdict with that id)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


# ---- the table of section 8, as data ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ActionRule:
    checks: tuple[int, ...]
    statuses: tuple[str, ...]
    cause: str | None            # the code that the rule of the audit writes at the start of its note; None when the status alone decides
    action: str                  # the stable code that SPEC-030 reads; its text is `verdict.action.{action}` in the catalogs


QUESTION_CHECKS = (1, 2, 3, 5, 7, 8, 11, 12, 14)
ACTION_TABLE: tuple[ActionRule, ...] = (
    ActionRule(QUESTION_CHECKS, ("unknown",), None, "answer_question"),
    ActionRule((1,), ("fail",), None, "use_point_in_time_data"),
    ActionRule((2,), ("fail",), None, "include_delisted"),
    ActionRule((3,), ("fail",), None, "apply_corporate_actions"),
    ActionRule((4,), ("fail", "unknown"), None, "fix_lookahead"),
    ActionRule((5,), ("fail",), None, "clean_data"),
    ActionRule((6,), ("unknown", "fail"), None, "count_trials"),
    ActionRule((7,), ("fail",), None, "tune_in_sample"),
    ActionRule((8,), ("fail",), None, "reserve_holdout"),
    ActionRule((9,), ("fail",), None, "lengthen_history"),
    ActionRule((10,), ("fail",), "no_costs", "charge_costs"),
    ActionRule((10,), ("fail",), "cost_share", "reduce_turnover"),
    ActionRule((11,), ("fail",), None, "model_impact"),
    ActionRule((12,), ("fail",), None, "charge_financing"),
    ActionRule((13,), ("fail",), None, "use_house_fill"),
    ActionRule((15,), ("unknown", "fail"), None, "run_walk_forward"),
    ActionRule((16,), ("fail",), None, "deflate_sharpe"),
    ActionRule((17,), ("fail",), None, "beat_chance"),
    ActionRule((18,), ("fail",), None, "raise_dsr"),
    ActionRule((19,), ("warn", "fail"), None, "reduce_overfit"),
    ActionRule((20,), ("fail",), None, "seek_plateau"),
    ActionRule((20,), ("warn",), "edge", "widen_range"),
    ActionRule((21,), ("fail",), None, "regime_filter"),
    ActionRule((22,), ("fail",), "sharpe_high", "audit_for_bug"),
    ActionRule((22,), ("fail",), "top_trade", "diversify_trades"),
    ActionRule((22,), ("fail",), "oos_ratio", "stabilize_oos"),
    ActionRule((23,), ("fail",), None, "split_costs"),
    ActionRule((24,), ("unknown",), None, "report_drawdown"),
)
ACTIONS = tuple(dict.fromkeys(rule.action for rule in ACTION_TABLE))
NO_IDEAL_ACTION = "seek_plateau"


def action_key(action: str, *, with_ideal: bool = True) -> str:
    """The catalog key of the text of an action (`seek_plateau` has a second text for the case that no ideal point exists)."""
    return f"verdict.action.{action}" if with_ideal or action != NO_IDEAL_ACTION else f"verdict.action.{action}_no_ideal"


# ---- values -------------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Exclusion:
    key: str
    reason: str | None
    by: str
    at: datetime


@dataclass(frozen=True)
class Suggestion:
    check: int
    key: str
    status: str
    action: str                       # action code of section 8
    message: str                      # message key; the text is rendered in the language of the reader
    params: Mapping[str, Any]


@dataclass(frozen=True)
class Ideal:
    status: Literal["found", "not_found"]
    params: Mapping[str, Any] | None
    plateau_size: int | None
    plateau_mean: float | None
    run_id: int | None                # the confirming engine run of the centre
    reason: str | None                # stable code when not_found


@dataclass(frozen=True)
class Verdict:
    id: int
    strategy_id: str
    strategy_version: int
    line: str
    verdict: Literal["go", "review", "needs_evidence", "no_go"]
    audit_verdict: Literal["go", "review", "needs_evidence", "no_go"]
    report: audit.AuditReport
    excluded: tuple[Exclusion, ...]
    sentences: tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]      # {"key": ..., "params": {...}}
    suggestions: tuple[Suggestion, ...]
    questions: tuple[QuestionState, ...]
    ideal: Ideal
    thresholds: thresholds.ThresholdSet
    run_id: int
    walkforward_id: int | None
    optimize_id: int | None
    montecarlo_id: int | None
    created_at: datetime
    notes: tuple[str, ...] = ()
    cause: str = "evaluate"
    parent_id: int | None = None
    actor: str = ""

    def to_dict(self, *, language: str = "en") -> dict[str, Any]:
        """The contract of section 9.2, with the sentences and the texts of the changes written in `language` (es or en; anything else is en)."""
        language = language if language in i18n.LANGUAGES else "en"
        gone = {e.key for e in self.excluded}
        phase = {c.key: c.phase for c in audit.CHECKLIST}
        return {
            "verdict_id": self.id, "strategy_id": self.strategy_id, "strategy_version": self.strategy_version, "line": self.line,
            "verdict": self.verdict, "audit_verdict": self.audit_verdict, "advisory": True,
            "sentences": [render_sentence(s, self.suggestions, language) for s in self.sentences],
            "checks": [{"number": i.number, "key": i.key, "phase": phase[i.key], "status": i.status, "note": i.note, "excluded": i.key in gone}
                       for i in self.report.items],
            "excluded": [{"key": e.key, "reason": e.reason, "by": e.by, "at": _iso(e.at)} for e in self.excluded],
            "open_questions": [{"key": q.key, "scope": q.scope, "subject": q.subject} for q in self.questions if q.status == "ask"],
            "suggestions": [{"check": s.check, "key": s.key, "status": s.status, "action": s.action, "text": render_suggestion(s, language),
                             "params": dict(s.params)} for s in self.suggestions],
            "ideal": {"status": self.ideal.status, "params": None if self.ideal.params is None else dict(self.ideal.params),
                      "plateau_size": self.ideal.plateau_size, "run_id": self.ideal.run_id, "reason": self.ideal.reason},
            "thresholds": {"line": self.thresholds.line, "version": self.thresholds.version},
            "runs": {"run": self.run_id, "walkforward": self.walkforward_id, "optimize": self.optimize_id, "montecarlo": self.montecarlo_id},
            "notes": list(self.notes), "created_at": _iso(self.created_at)}


# ---- small helpers ------------------------------------------------------------------------------------------------------------------

def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value)
    return datetime.fromisoformat(text.replace("Z", "+00:00")) if "T" in text else from_db_time(text)


def _actor(actor: Any) -> str:
    if not isinstance(actor, str) or not _ACTOR.match(actor):
        raise VerdictError("bad_input", "actor is a lowercase name given by the code that calls")
    return actor


def _reason(reason: Any) -> str | None:
    if reason is None:
        return None
    if not isinstance(reason, str) or len(reason.strip()) > MAX_REASON:
        raise VerdictError("bad_input", f"reason is text of at most {MAX_REASON} characters")
    return reason.strip() or None


def _keys(keys: Any) -> list[str]:
    if isinstance(keys, str) or not isinstance(keys, Sequence) or not keys:
        raise VerdictError("bad_input", "keys is a list of check keys")
    unknown = sorted({str(k) for k in keys if not isinstance(k, str) or k not in CHECK_BY_KEY})
    if unknown:
        raise VerdictError("bad_input", f"unknown checks: {', '.join(unknown)}; the keys are {', '.join(CHECK_BY_KEY)}")
    return list(dict.fromkeys(keys))


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _evidence_dict(evidence: audit.AuditEvidence) -> dict[str, Any]:
    body = dataclasses.asdict(evidence)
    if body["edge_of_space"] is not None:
        body["edge_of_space"] = list(body["edge_of_space"])
    return body


def _evidence_from(body: Mapping[str, Any]) -> audit.AuditEvidence:
    data = dict(body)
    if data.get("edge_of_space") is not None:
        data["edge_of_space"] = tuple(data["edge_of_space"])
    return audit.AuditEvidence(**data)


def _report_from(body: Mapping[str, Any]) -> audit.AuditReport:
    phases = tuple(audit.PhaseReport(p["phase"], tuple(audit.CheckResult(i["number"], i["key"], i["text"], i["status"], i["note"]) for i in p["items"]))
                   for p in body["phases"])
    return audit.AuditReport(phases, body["verdict"], body.get("oos_to_is"))


def _state_dict(state: QuestionState) -> dict[str, Any]:
    body = dataclasses.asdict(state)
    body["answered_at"] = None if state.answered_at is None else _iso(state.answered_at)
    return body


def _state_from(body: Mapping[str, Any]) -> QuestionState:
    data = dict(body)
    data["answered_at"] = None if data.get("answered_at") is None else _when(data["answered_at"])
    return QuestionState(**data)


def _exclusion_dict(e: Exclusion) -> dict[str, Any]:
    return {"key": e.key, "reason": e.reason, "by": e.by, "at": _iso(e.at)}


def _exclusion_from(body: Mapping[str, Any]) -> Exclusion:
    return Exclusion(body["key"], body.get("reason"), body["by"], _when(body["at"]))


def _codes(note: str) -> set[str]:
    """The codes that the rules of the audit write at the start of their notes (4.5): one per part of the note."""
    return {m.group(1) for part in note.split("; ") if (m := _CODE.match(part))}


def _first_percent(note: str, after: str) -> str:
    found = _PERCENT.search(note[note.find(after):]) if after in note else None
    return found.group(1) if found else "-"


def _pct(value: float) -> str:
    return f"{value:.0%}"


# ---- the suggestions (section 8) --------------------------------------------------------------------------------------------------------

def _params_text(params: Mapping[str, Any] | None) -> str:
    return ", ".join(f"{k}={v}" for k, v in (params or {}).items())


def _suggestion_params(rule: ActionRule, item: audit.CheckResult, evidence: audit.AuditEvidence, values: Mapping[str, Any], ideal: Ideal,
                       optimization: Any) -> dict[str, Any]:
    action, note = rule.action, item.note
    if action == "answer_question":
        return {"question": QUESTION_OF_CHECK[item.number]}
    if action == "reduce_turnover":
        return {"pct": _first_percent(note, "costs take"), "max": _first_percent(note, "limit is")}
    if action == "beat_chance":
        return {"n": evidence.n_trials}
    if action == "reduce_overfit":
        return {"pbo": evidence.pbo, "limit": values["pbo_reject"] if item.status == "fail" else values["pbo_warn"]}
    if action == "seek_plateau":
        return {"ideal": _params_text(ideal.params)} if ideal.status == "found" else {}
    if action == "widen_range":
        edge = tuple(evidence.edge_of_space or ())
        best = None if optimization is None or optimization.best is None else optimization.best.params
        return {"params": ", ".join(edge), "value": ", ".join(str(best[n]) for n in edge) if best else "-"}
    if action == "regime_filter":
        return {"list": ", ".join(sorted(k for k, v in (evidence.regime_sharpes or {}).items() if v <= 0))}
    if action == "audit_for_bug":
        found = _NUMBER.search(note[note.find("annual Sharpe"):]) if "annual Sharpe" in note else None
        return {"value": found.group(1) if found else "-"}
    if action == "diversify_trades":
        return {"pct": _first_percent(note, "carries")}
    if action == "stabilize_oos":
        return {"pct": _first_percent(note, "out-of-sample Sharpe is")}
    return {}


def build_suggestions(report: audit.AuditReport, evidence: audit.AuditEvidence, values: Mapping[str, Any], ideal: Ideal, optimization: Any) -> list[Suggestion]:
    """Every change that the table of section 8 gives for the checks that are not green, the most serious first (fail, unknown, warn; then the number of the check).
    The cause of a row is the code at the start of the note of the rule of the audit, never free text."""
    out: list[Suggestion] = []
    for item in sorted(report.items, key=lambda i: (SEVERITY.index(i.status), i.number)):
        if item.status not in ("fail", "unknown", "warn"):
            continue
        codes = _codes(item.note)
        for rule in ACTION_TABLE:
            if item.number in rule.checks and item.status in rule.statuses and (rule.cause is None or rule.cause in codes):
                params = _suggestion_params(rule, item, evidence, values, ideal, optimization)
                out.append(Suggestion(item.number, item.key, item.status, rule.action,
                                      action_key(rule.action, with_ideal=bool(params) or rule.action != NO_IDEAL_ACTION), params))
    return out


# ---- rendering ----------------------------------------------------------------------------------------------------------------------

def _say(key: str, language: str, **params: Any) -> str:
    with i18n.use_language(language):
        return i18n.t(key, **params)


def _plain(value: Any, language: str) -> Any:
    if isinstance(value, bool) or value is None:
        return "-" if value is None else value
    if isinstance(value, float):
        text = f"{value:.2f}"
        return text.replace(".", ",") if language == "es" else text
    if isinstance(value, (list, tuple)):
        return ", ".join(str(_plain(v, language)) for v in value)
    return value


def render_suggestion(suggestion: Suggestion, language: str = "en") -> str:
    """The text of a suggested change in `language`. The name of a question is its text in the catalog; every other parameter is a number or a name."""
    params = {k: (_say(f"verdict.question.{v}", language) if k == "question" else _plain(v, language)) for k, v in suggestion.params.items()}
    return _say(suggestion.message, language, **params)


def render_sentence(sentence: Mapping[str, Any], suggestions: Sequence[Suggestion], language: str = "en") -> str:
    """One of the three sentences in `language`: its key with its parameters. A parameter `check` is shown as the name of the check, and a parameter
    `action` as the text of that change (the one of the suggestion that has it)."""
    params = {k: _plain(v, language) for k, v in sentence["params"].items() if k not in ("check", "action")}
    if "check" in sentence["params"] and "action" not in sentence["params"]:
        params["name"] = _say(f"verdict.check.{sentence['params']['check']}", language)
    if "action" in sentence["params"]:
        wanted = [s for s in suggestions if s.action == sentence["params"]["action"] and s.check == sentence["params"].get("check")]
        params["text"] = render_suggestion(wanted[0], language) if wanted else ""
    return _say(sentence["key"], language, **params)


# ---- the three sentences (5.6) -------------------------------------------------------------------------------------------------------

def _strongest(report: audit.AuditReport, gone: set[str], evidence: audit.AuditEvidence, values: Mapping[str, Any]) -> dict[str, Any]:
    """The check that passes with the most margin (distance to its limit over the limit) among the deflated Sharpe, the probability of overfitting, the neighbors
    and the out of sample share. The numbers are the ones of the evidence, of the limits and of the notes of the audit: nothing is calculated again."""
    by_key = {i.key: i for i in report.items}

    def passes(key: str) -> bool:
        return key in by_key and key not in gone and by_key[key].status == "pass"

    found: list[tuple[float, dict[str, Any]]] = []
    if passes("deflated_sharpe") and evidence.dsr is not None:
        found.append(((evidence.dsr - values["min_dsr"]) / values["min_dsr"],
                      {"key": "verdict.sentence.strongest.deflated_sharpe", "params": {"value": evidence.dsr, "limit": values["min_dsr"]}}))
    if passes("pbo") and evidence.pbo is not None:
        found.append(((values["pbo_warn"] - evidence.pbo) / values["pbo_warn"],
                      {"key": "verdict.sentence.strongest.pbo", "params": {"value": evidence.pbo, "limit": values["pbo_warn"]}}))
    if passes("parameter_neighbors"):
        loses = re.search(r"loses (-?\d+(?:\.\d+)?)%", by_key["parameter_neighbors"].note)
        if loses:
            drop = float(loses.group(1)) / 100.0
            found.append(((values["max_neighbor_drop"] - drop) / values["max_neighbor_drop"],
                          {"key": "verdict.sentence.strongest.parameter_neighbors", "params": {"value": _pct(drop), "limit": _pct(values["max_neighbor_drop"])}}))
    if passes("red_flags") and report.oos_to_is is not None:
        found.append(((report.oos_to_is - values["min_oos_to_is"]) / values["min_oos_to_is"],
                      {"key": "verdict.sentence.strongest.out_of_sample", "params": {"value": _pct(report.oos_to_is), "limit": _pct(values["min_oos_to_is"])}}))
    return max(found, key=lambda f: f[0])[1] if found else {"key": "verdict.sentence.strongest.none", "params": {}}


def build_sentences(report: audit.AuditReport, gone: set[str], verdict: str, states: Sequence[QuestionState], suggestions: Sequence[Suggestion],
                    evidence: audit.AuditEvidence, values: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """What it suggests, what is most solid, and the next step. `suggestions` are the ones of the checks that are still in; the order of gravity is fail, unknown,
    warn and then the number of the check."""
    live = [i for i in report.items if i.key not in gone]
    ranked = sorted(live, key=lambda i: (SEVERITY.index(i.status), i.number))
    wanted = {"no_go": "fail", "review": "warn", "needs_evidence": "unknown"}.get(verdict)
    of_kind = [i for i in ranked if i.status == wanted]
    main = of_kind[0].key if of_kind else None
    if verdict == "go":
        first = {"key": "verdict.sentence.go", "params": {"n": sum(1 for i in live if i.status != "na")}}
    elif verdict == "needs_evidence":
        asked = sum(1 for s in states if s.status == "ask" and CHECK_OF_QUESTION[s.key] not in gone)
        first = ({"key": "verdict.sentence.needs_evidence", "params": {"q": asked, "k": len(of_kind)}} if asked
                 else {"key": "verdict.sentence.needs_evidence_other", "params": {"k": len(of_kind), "check": main}})
    else:
        first = {"key": f"verdict.sentence.{verdict}", "params": {"k": len(of_kind), "check": main}}
    ordered = sorted((s for s in suggestions if s.key not in gone), key=lambda s: (SEVERITY.index(s.status), s.check))
    if verdict == "go" or not ordered:
        third: dict[str, Any] = {"key": "verdict.sentence.next_go", "params": {}}
    elif ordered[0].action == "answer_question":
        third = {"key": "verdict.sentence.next_answer", "params": {}}
    else:
        third = {"key": "verdict.sentence.next", "params": {"action": ordered[0].action, "check": ordered[0].check}}
    return first, _strongest(report, gone, evidence, values), third


# ---- the ideal result (5.7) ---------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Pending:
    """An ideal point that was found and run, waiting for the walk forward check that needs the report."""
    params: Mapping[str, Any]
    size: int
    mean: float
    run_id: int
    neighbors_ok: bool


def _snap(space: Any, centre: Mapping[str, float]) -> dict[str, Any]:
    """The centre of a plateau (it can fall between two values of the grid) as the nearest value of each axis; the lower one on a tie. A point that the
    constraints of the space refuse is replaced by the valid point of the grid that is nearest to the centre, in units of the range of each axis."""
    def nearest(axis: Any) -> Any:
        return min(axis.values, key=lambda v: (abs(float(v) - centre[axis.name]), float(v)))

    point = {axis.name: nearest(axis) for axis in space.axes}
    if space.valid(point):
        return point
    span = {a.name: (float(a.values[-1]) - float(a.values[0])) or 1.0 for a in space.axes}
    return min(space.combinations(), key=lambda p: sum(((float(p[n]) - centre[n]) / span[n]) ** 2 for n in span))


def _not_found(reason: str, pending: _Pending | None = None, size: int | None = None, mean: float | None = None) -> Ideal:
    if pending is not None:
        return Ideal("not_found", dict(pending.params), pending.size, pending.mean, pending.run_id, reason)
    return Ideal("not_found", None, size, mean, None, reason)


def _ideal_stage1(inputs: evidence_bridge.BridgeInputs, values: Mapping[str, Any], config: audit.AuditConfig, services: Any) -> Ideal | _Pending:
    """The part of the ideal that runs the engine: the centre of the widest plateau, its one run (reused when the ledger has it) and its neighbors."""
    from hokma.backtest import surface, walkforward

    if inputs.optimization is None:
        return _not_found("no_optimization")
    try:
        found = surface.plateaus(inputs.optimization)
    except surface.SearchError:
        return _not_found("plateaus_unavailable")
    if not found:
        return _not_found("plateaus_unavailable")
    widest = max(found, key=lambda p: (p.size, p.mean_value))
    point = _snap(inputs.optimization.space, widest.center)
    base = evidence_bridge.base_spec(inputs, services=services)
    full = {**base.params, **point}
    run_id, _sharpe = evidence_bridge.point_run(inputs, full, services=services)
    if run_id is None:
        return Ideal("not_found", point, widest.size, widest.mean_value, None, "plateau_fails_neighbors")
    neighbors = evidence_bridge.neighbor_sharpes(inputs, spread=float(values["neighbor_spread"]), services=services, params=full)
    result = walkforward.load_result(services.database(), run_id, curve_dir=services.curves())
    held = False
    if result.status == "done":
        report = audit.audit(result, dataclasses.replace(base, params=full), audit.AuditEvidence(neighbor_sharpes=neighbors or None), config)
        held = {i.number: i for i in report.items}[20].status == "pass"
    return _Pending(point, widest.size, widest.mean_value, run_id, held)


def _ideal_stage2(pending: Ideal | _Pending, report: audit.AuditReport, oos_total: float | None, values: Mapping[str, Any]) -> Ideal:
    """The walk forward part, from the report: the check of the scheme is green, the out of sample total is above zero (a combinatorial validation has none: its
    tests overlap, and the ratio of the report decides) and the ratio of out of sample to in sample reaches the minimum."""
    if isinstance(pending, Ideal):
        return pending
    if not pending.neighbors_ok:
        return _not_found("plateau_fails_neighbors", pending)
    scheme = {i.number: i for i in report.items}[15].status == "pass"
    ratio = report.oos_to_is
    if not (scheme and (oos_total is None or oos_total > 0) and ratio is not None and ratio >= values["min_oos_to_is"]):
        return _not_found("walk_forward_fails", pending)
    return Ideal("found", dict(pending.params), pending.size, pending.mean, pending.run_id, None)


# ---- storage ------------------------------------------------------------------------------------------------------------------------

def _row(db: Database, verdict_id: Any) -> Mapping[str, Any]:
    if isinstance(verdict_id, bool) or not isinstance(verdict_id, int):
        raise VerdictError("bad_input", "verdict_id is a whole number")
    with db.read() as u:
        row = u.session.one("SELECT * FROM strategy_verdicts WHERE id = :i", {"i": verdict_id})
    if row is None:
        raise VerdictError("no_verdict", f"there is no verdict {verdict_id}")
    return row


def _threshold_set(db: Database, line: str, set_id: int) -> thresholds.ThresholdSet:
    return next(s for s in thresholds.history(line, db=db) if s.id == int(set_id))


def _verdict(row: Mapping[str, Any], db: Database) -> Verdict:
    report_body = json.loads(row["report_json"])
    context = report_body.get("context") or {}
    gone = tuple(_exclusion_from(e) for e in json.loads(row["excluded_json"]))
    hidden = {e.key for e in gone}
    ideal = json.loads(row["ideal_json"])
    return Verdict(
        int(row["id"]), row["strategy_id"], int(row["strategy_version"]), row["line"], row["verdict"], row["audit_verdict"], _report_from(report_body), gone,
        tuple(json.loads(row["sentences_json"])),                                                                                 # type: ignore[arg-type]
        tuple(Suggestion(s["check"], s["key"], s["status"], s["action"], s["message"], s["params"]) for s in json.loads(row["suggestions_json"])
              if s["key"] not in hidden), tuple(_state_from(q) for q in context.get("questions", [])),
        Ideal(ideal["status"], ideal.get("params"), ideal.get("plateau_size"), ideal.get("plateau_mean"), ideal.get("run_id"), ideal.get("reason")),
        _threshold_set(db, row["line"], row["thresholds_id"]), int(row["run_id"]), row["walkforward_id"], row["optimize_id"], row["montecarlo_id"],
        _when(row["created_at"]), tuple(context.get("notes", [])), row["cause"], row["parent_id"], row["actor"])


def _inputs_hash(identity: Mapping[str, Any], evidence: audit.AuditEvidence, answer_ids: Sequence[int], owner_exclusions: Sequence[Exclusion]) -> str:
    """The same saved facts, limits, answers and exclusions give the same hash: it names the input of a verdict, not the moment it was made."""
    body = {**identity, "answer_ids": sorted(answer_ids), "excluded": sorted([e.key, e.reason, e.by] for e in owner_exclusions), "evidence": _evidence_dict(evidence)}
    return hashlib.sha256(_json(body).encode("utf-8")).hexdigest()


def _insert(db: Database, *, strategy_id: str, version: int, line: str, parent_id: int | None, cause: str, run_ids: Mapping[str, int | None], thresholds_id: int,
            answer_ids: Sequence[int], excluded: Sequence[Exclusion], evidence: audit.AuditEvidence, report: audit.AuditReport, context: Mapping[str, Any],
            sentences: Sequence[Mapping[str, Any]], suggestions: Sequence[Suggestion], ideal: Ideal, inputs_hash: str, actor: str) -> Verdict:
    gone = {e.key for e in excluded}
    final = audit.verdict_for(i.status for i in report.items if i.key not in gone)
    with db.write() as u:
        new_id = u.session.insert("strategy_verdicts", {
            "strategy_id": strategy_id, "strategy_version": version, "line": line, "parent_id": parent_id, "cause": cause, "run_id": run_ids["run"],
            "walkforward_id": run_ids["walkforward"], "optimize_id": run_ids["optimize"], "montecarlo_id": run_ids["montecarlo"], "thresholds_id": thresholds_id,
            "answer_ids_json": _json(sorted(answer_ids)), "excluded_json": _json([_exclusion_dict(e) for e in excluded]), "evidence_json": _json(_evidence_dict(evidence)),
            "audit_verdict": report.verdict, "verdict": final, "report_json": _json({**report.to_dict(), "context": dict(context)}),
            "sentences_json": _json(list(sentences)), "suggestions_json": _json([dataclasses.asdict(s) for s in suggestions]),
            "ideal_json": _json(dataclasses.asdict(ideal)), "inputs_hash": inputs_hash, "actor": actor})
        row = u.session.one("SELECT * FROM strategy_verdicts WHERE id = :i", {"i": new_id})
    return _verdict(row, db)                                                                                                   # type: ignore[arg-type]


def _system_exclusions(states: Sequence[QuestionState], report: audit.AuditReport, now: datetime) -> list[Exclusion]:
    """The checks that do not apply: a question that the rules say does not apply to this strategy, or that the owner answered "does not apply". They are taken
    out only when the check is not already green or not applicable by itself."""
    status = {i.key: i.status for i in report.items}
    out: list[Exclusion] = []
    for question in questions.QUESTIONS:
        mine = [s for s in states if s.key == question.key]
        key = CHECK_OF_QUESTION[question.key]
        if not mine or status.get(key) in (None, "pass", "na"):
            continue
        if all(s.status == "na" for s in mine):
            out.append(Exclusion(key, f"not_applicable:{mine[0].na_reason}", SYSTEM, now))
        elif all(s.status in ("answered", "na") and (s.status == "na" or s.answer == "na") for s in mine):
            out.append(Exclusion(key, "owner_said_na", SYSTEM, now))
    return out


def _answer_ids(states: Sequence[QuestionState]) -> list[int]:
    return sorted({s.answer_id for s in states if s.answer_id is not None})


def _oos_total(inputs: evidence_bridge.BridgeInputs) -> float | None:
    run = None if inputs.bundle is None else inputs.bundle.parameters.get("walk_forward_run")
    value = None if run is None else run.get("oos_total")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _context(states: Sequence[QuestionState], notes: Sequence[str], oos_total: float | None) -> dict[str, Any]:
    return {"questions": [_state_dict(s) for s in states], "notes": list(notes), "oos_total": oos_total}


# ---- evaluate -----------------------------------------------------------------------------------------------------------------------

def latest(strategy_id: str, version: int, *, db: Database | None = None) -> Verdict | None:
    """The newest verdict of a version of a strategy (an evaluation or a change of it), or None when it has none."""
    database = db or get_database()
    with database.read() as u:
        row = u.session.one("SELECT * FROM strategy_verdicts WHERE strategy_id = :s AND strategy_version = :v ORDER BY id DESC LIMIT 1",
                            {"s": strategy_id, "v": version})
    return None if row is None else _verdict(row, database)


def get(verdict_id: int, *, db: Database | None = None) -> Verdict:
    """The verdict with this id. `no_verdict` when there is none; `bad_input` when the id is not a whole number."""
    database = db or get_database()
    return _verdict(_row(database, verdict_id), database)


def evaluate(strategy_id: str, version: int, *, actor: str, services: Any = None) -> Verdict:
    """The verdict of a version of a strategy, from its saved run, walk forward, optimization and Monte Carlo, the limits of its line and the answers of the owner.

    Everything that runs the engine happens first (the neighbors of the parameters, the ideal point and its neighbors), so the evidence bundle that follows counts
    every trial and an evaluation made again over the same saved facts gives the same input hash and the same verdict. The exclusions in force in the last verdict
    of the version are kept; the ones that come from answers are made again from the answers. Errors keep their module's code: `no_spec`, `no_run`,
    `no_thresholds` (the line has no limits), `bad_input`."""
    actor = _actor(actor)
    services = evidence_bridge.resolve_services(services)
    db = services.database()
    current = thresholds.current(evidence_bridge.line_of(strategy_id, version, services=services), db=db)       # a line with no limits stops before anything runs
    light = evidence_bridge.gather(strategy_id, version, services=services, bundle=False)
    values = current.values
    config = thresholds.to_config(values)
    states = questions.states(strategy_id, version, services=services)
    evidence_bridge.neighbor_sharpes(light, spread=float(values["neighbor_spread"]), services=services)
    stage = _ideal_stage1(light, values, config, services)
    inputs = evidence_bridge.gather(strategy_id, version, services=services)
    evidence = evidence_bridge.build_evidence(inputs, thresholds=values, answers=questions.evidence_fields(states), services=services)
    from hokma.backtest import walkforward

    result = walkforward.load_result(db, inputs.run_id, curve_dir=services.curves())
    report = audit.audit(result, evidence_bridge.base_spec(inputs, services=services), evidence, config)
    ideal = _ideal_stage2(stage, report, _oos_total(inputs), values)
    before = latest(strategy_id, version, db=db)
    now = datetime.now(timezone.utc)
    owner = [e for e in (() if before is None else before.excluded) if e.by != SYSTEM]
    excluded = owner + _system_exclusions(states, report, now)
    gone = {e.key for e in excluded}
    suggestions = build_suggestions(report, evidence, values, ideal, inputs.optimization)
    final = audit.verdict_for(i.status for i in report.items if i.key not in gone)
    sentences = build_sentences(report, gone, final, states, suggestions, evidence, values)
    identity = {"strategy_id": strategy_id, "version": version, "run_id": inputs.run_id, "walkforward_id": inputs.walkforward_id, "optimize_id": inputs.optimize_id,
                "montecarlo_id": inputs.montecarlo_id, "thresholds_id": current.id}
    return _insert(db, strategy_id=strategy_id, version=version, line=inputs.line, parent_id=None, cause="evaluate",
                   run_ids={"run": inputs.run_id, "walkforward": inputs.walkforward_id, "optimize": inputs.optimize_id, "montecarlo": inputs.montecarlo_id},
                   thresholds_id=current.id, answer_ids=_answer_ids(states), excluded=excluded, evidence=evidence, report=report,
                   context=_context(states, inputs.notes, _oos_total(inputs)), sentences=sentences, suggestions=suggestions, ideal=ideal,
                   inputs_hash=_inputs_hash(identity, evidence, _answer_ids(states), owner), actor=actor)


# ---- exclude, include and recompute -------------------------------------------------------------------------------------------------

def _child(db: Database, row: Mapping[str, Any], cause: str, actor: str, excluded: Sequence[Exclusion]) -> Verdict:
    """A verdict that differs from its parent only by the checks that are out: the report, the evidence and the ideal are the parent's, and nothing runs."""
    report_body = json.loads(row["report_json"])
    report, evidence = _report_from(report_body), _evidence_from(json.loads(row["evidence_json"]))
    context = report_body.get("context") or {}
    states = [_state_from(q) for q in context.get("questions", [])]
    values = _threshold_set(db, row["line"], row["thresholds_id"]).values
    suggestions = [Suggestion(s["check"], s["key"], s["status"], s["action"], s["message"], s["params"]) for s in json.loads(row["suggestions_json"])]
    gone = {e.key for e in excluded}
    final = audit.verdict_for(i.status for i in report.items if i.key not in gone)
    sentences = build_sentences(report, gone, final, states, suggestions, evidence, values)
    owner = [e for e in excluded if e.by != SYSTEM]
    identity = {"strategy_id": row["strategy_id"], "version": int(row["strategy_version"]), "run_id": int(row["run_id"]), "walkforward_id": row["walkforward_id"],
                "optimize_id": row["optimize_id"], "montecarlo_id": row["montecarlo_id"], "thresholds_id": int(row["thresholds_id"])}
    return _insert(db, strategy_id=row["strategy_id"], version=int(row["strategy_version"]), line=row["line"], parent_id=int(row["id"]), cause=cause,
                   run_ids={"run": int(row["run_id"]), "walkforward": row["walkforward_id"], "optimize": row["optimize_id"], "montecarlo": row["montecarlo_id"]},
                   thresholds_id=int(row["thresholds_id"]), answer_ids=json.loads(row["answer_ids_json"]), excluded=excluded, evidence=evidence, report=report,
                   context=context, sentences=sentences, suggestions=suggestions, ideal=Ideal(**json.loads(row["ideal_json"])),
                   inputs_hash=_inputs_hash(identity, evidence, json.loads(row["answer_ids_json"]), owner), actor=actor)


def exclude(verdict_id: int, keys: Sequence[str], *, reason: str | None, actor: str, db: Database | None = None) -> Verdict:
    """Take checks out of the verdict (C1). Nothing runs: the new verdict is the report of its parent with the verdict of the checks that are left. The reason is
    optional and is kept with who took the check out and when. A check that is already out is left as it was; when every key was already out the parent comes back."""
    actor, reason, keys = _actor(actor), _reason(reason), _keys(keys)
    database = db or get_database()
    row = _row(database, verdict_id)
    current = [_exclusion_from(e) for e in json.loads(row["excluded_json"])]
    out = {e.key for e in current}
    now = datetime.now(timezone.utc)
    added = [Exclusion(k, reason, actor, now) for k in keys if k not in out]
    if not added:
        return _verdict(row, database)
    return _child(database, row, "exclude", actor, current + added)


def include(verdict_id: int, keys: Sequence[str], *, actor: str, db: Database | None = None) -> Verdict:
    """Put checks back. Nothing runs. A check that the system took out because it does not apply cannot be put back from here: change the answer. A check that is
    not out is `bad_input`."""
    actor, keys = _actor(actor), _keys(keys)
    database = db or get_database()
    row = _row(database, verdict_id)
    current = [_exclusion_from(e) for e in json.loads(row["excluded_json"])]
    by_key = {e.key: e for e in current}
    missing = [k for k in keys if k not in by_key]
    if missing:
        raise VerdictError("bad_input", f"these checks are not out of the verdict: {', '.join(missing)}")
    automatic = [k for k in keys if by_key[k].by == SYSTEM]
    if automatic:
        raise VerdictError("bad_input", f"{', '.join(automatic)} do not apply to this strategy: change the answer of the question instead")
    return _child(database, row, "include", actor, [e for e in current if e.key not in keys])


def _inputs_of(row: Mapping[str, Any], evidence: audit.AuditEvidence, services: Any) -> evidence_bridge.BridgeInputs:
    from hokma.backtest import optimize

    optimization = None
    if row["optimize_id"] is not None:
        try:
            optimization = optimize.load(services.database(), int(row["optimize_id"]))
        except optimize.SearchError:
            optimization = None
    return evidence_bridge.BridgeInputs(row["strategy_id"], int(row["strategy_version"]), row["line"], int(row["run_id"]), row["walkforward_id"], row["optimize_id"],
                                        row["montecarlo_id"], bool(evidence.uses_regimes), None, optimization, ())


def recompute(verdict_id: int, *, cause: Literal["thresholds", "answers"], actor: str, services: Any = None) -> Verdict:
    """The verdict again with the limits of the line now (`thresholds`) or the answers of the owner now (`answers`), over the evidence that was saved: the audit runs
    again and no backtest does, except that a new neighbor spread runs the neighbors that the ledger does not have (it can take a while) and the ideal point is
    judged again with the new limits. The exclusions of the parent stay. When nothing changed the parent comes back."""
    if cause not in ("thresholds", "answers"):
        raise VerdictError("bad_input", "cause is thresholds or answers")
    actor = _actor(actor)
    services = evidence_bridge.resolve_services(services)
    db = services.database()
    row = _row(db, verdict_id)
    report_body = json.loads(row["report_json"])
    context = report_body.get("context") or {}
    evidence = _evidence_from(json.loads(row["evidence_json"]))
    old = _threshold_set(db, row["line"], row["thresholds_id"])
    inputs = _inputs_of(row, evidence, services)
    excluded = [_exclusion_from(e) for e in json.loads(row["excluded_json"])]
    states = [_state_from(q) for q in context.get("questions", [])]
    notes = list(context.get("notes", []))
    answer_ids = json.loads(row["answer_ids_json"])
    ideal = Ideal(**json.loads(row["ideal_json"]))
    current = old
    pending: Ideal | _Pending | None = None
    if cause == "thresholds":
        current = thresholds.current(row["line"], db=db)
        if current.id == old.id:
            return _verdict(row, db)
        values = current.values
        config = thresholds.to_config(values)
        if values["neighbor_spread"] != old.values["neighbor_spread"]:
            evidence = dataclasses.replace(evidence, neighbor_sharpes=evidence_bridge.neighbor_sharpes(inputs, spread=float(values["neighbor_spread"]),
                                                                                                      services=services) or None)
        edge = None if inputs.optimization is None or not values["edge_warns"] else tuple(inputs.optimization.edge_of_space)
        evidence = dataclasses.replace(evidence, edge_of_space=edge)
        pending = _ideal_stage1(inputs, values, config, services)
    else:
        values, config = old.values, thresholds.to_config(old.values)
        states = questions.states(row["strategy_id"], int(row["strategy_version"]), services=services)
        fields = questions.evidence_fields(states)
        evidence = dataclasses.replace(evidence, **{name: fields[name] for name in evidence_bridge.ANSWER_FIELDS})
        answer_ids = _answer_ids(states)
        if answer_ids == json.loads(row["answer_ids_json"]) and [_state_dict(s) for s in states] == context.get("questions"):
            return _verdict(row, db)
    from hokma.backtest import walkforward

    result = walkforward.load_result(db, int(row["run_id"]), curve_dir=services.curves())
    report = audit.audit(result, evidence_bridge.base_spec(inputs, services=services), evidence, config)
    oos_total = context.get("oos_total")
    if pending is not None:
        ideal = _ideal_stage2(pending, report, oos_total, values)
    now = datetime.now(timezone.utc)
    if cause == "answers":
        excluded = [e for e in excluded if e.by != SYSTEM] + _system_exclusions(states, report, now)
    gone = {e.key for e in excluded}
    suggestions = build_suggestions(report, evidence, values, ideal, inputs.optimization)
    final = audit.verdict_for(i.status for i in report.items if i.key not in gone)
    sentences = build_sentences(report, gone, final, states, suggestions, evidence, values)
    identity = {"strategy_id": row["strategy_id"], "version": int(row["strategy_version"]), "run_id": int(row["run_id"]), "walkforward_id": row["walkforward_id"],
                "optimize_id": row["optimize_id"], "montecarlo_id": row["montecarlo_id"], "thresholds_id": current.id}
    return _insert(db, strategy_id=row["strategy_id"], version=int(row["strategy_version"]), line=row["line"], parent_id=int(row["id"]), cause=cause,
                   run_ids={"run": int(row["run_id"]), "walkforward": row["walkforward_id"], "optimize": row["optimize_id"], "montecarlo": row["montecarlo_id"]},
                   thresholds_id=current.id, answer_ids=answer_ids, excluded=excluded, evidence=evidence, report=report,
                   context=_context(states, notes, oos_total), sentences=sentences, suggestions=suggestions, ideal=ideal,
                   inputs_hash=_inputs_hash(identity, evidence, answer_ids, [e for e in excluded if e.by != SYSTEM]), actor=actor)
