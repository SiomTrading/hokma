"""The flow of a strategy written in pandas inside the bank and the registry (1.3.0, task 3433).

It reuses the tables of the strategies written as code (rules, briefs, versions, reviews, specs): a version of the new flow is one more row there, marked in its manifest as flow pandas.
- When the checks of Hokma pass (dry run), the version is saved and generated in one step, with a review signed by hokma-checks: from that moment the backtest and the optimizer can use it.
  Ben is not in the way of the optimizer.
- Ben (the reviewer of the logic against the spec, who does not repeat the checks) gives his verdict before paper and before real money: the owner can only approve a version of this flow whose
  last review is an approval by a reviewer that is not hokma-checks. The code refuses it and the base refuses it too (migration 0041). A strategy that only passed the checks never reaches
  paper or real money; and the owner approves it, as always, for real money.
- The class that the registry holds replays the signals (adapter.py) and gets them from the loader (loader.py), in the same process.
"""
from __future__ import annotations

import json
from typing import Any

from hokma.db import Database, get_database
from hokma.factory.pandas_strategy import adapter, contract, loader
from hokma.factory.usercode import static_check

FLOW = "pandas"
CHECKS_BY = "hokma-checks"                      # the signature of the automatic review: it is never Ben


def manifest_of(defs: tuple[contract.ParamDef, ...]) -> str:
    """The manifest of a version. Written without spaces: the trigger of the base looks for the text "flow":"pandas"."""
    schema = [{"name": d.name, "kind": d.kind, "default": d.coerce(d.default), "low": d.min, "high": d.max, "choices": []} for d in defs]
    steps = [{"name": d.name, "step": d.step} for d in defs]
    return json.dumps({"flow": FLOW, "schema": schema, "steps": steps}, sort_keys=True, separators=(",", ":"))


def is_pandas(manifest_json: str | None) -> bool:
    try:
        return json.loads(manifest_json or "{}").get("flow") == FLOW
    except ValueError:
        return False


def defs_of(manifest_json: str) -> tuple[contract.ParamDef, ...]:
    data = json.loads(manifest_json)
    steps = {s["name"]: s["step"] for s in data.get("steps", [])}
    return tuple(contract.ParamDef(i["name"], i["default"], i["low"], i["high"], steps.get(i["name"], 1), i["kind"]) for i in data["schema"])


def text_of(source: str) -> str:
    return static_check.normalize_source(loader.normalize(source))


def store_version(*, rule_id: int, brief_id: int, request_version: int, registry_id: str, number: int, text: str, defs: tuple[contract.ParamDef, ...], report: dict[str, Any],
                  caller: str, origin: tuple[int | None, str | None], db: Database | None = None) -> dict[str, Any]:
    """Save the version as a draft, then let the automatic review approve it and generate it, exactly like the verdict of a reviewer does for the code of the other flow."""
    from hokma.factory import user_rules

    database = db or get_database()
    sha = static_check.code_sha256(text)
    with database.write() as u:
        s = u.session
        version_id = s.insert("user_rule_versions", {
            "rule_id": rule_id, "version": number, "registry_id": registry_id, "request_version": request_version, "code_sha256": sha, "code_bytes": len(text.encode("utf-8")), "code_text": text,
            "cases_json": json.dumps({"flow": FLOW}), "manifest_json": manifest_of(defs), "check_json": "[]", "test_json": json.dumps({**report, "ok": True, "strategy_sha256": sha}, sort_keys=True),
            "status": "draft", "brief_id": brief_id, "created_by": caller, "origin_desk_id": origin[0], "origin_desk_name": origin[1]})
        user_rules._event(s, version_id, None, "draft", "saved by the creator after the dry run", caller)
    with database.read() as u:
        row = user_rules._version_row(u.session, rule_version_id=version_id)
    approved = user_rules._approve(database, row, {"tests": []}, [], CHECKS_BY)
    return {**approved, "rule_version_id": version_id, "code_sha256": sha}


def ben_verdict(database: Database, row: Any, who: str, verdict: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Ben's verdict on a version of this flow that the checks already generated: the logic against the spec. An approval opens the road to the owner; a rejection closes the version."""
    from hokma.factory import user_rules

    if who == CHECKS_BY:
        raise user_rules.UserRuleError("blocked", "the signature of the automatic checks is not a reviewer")
    if row["status"] != "generated":
        raise user_rules.UserRuleError("bad_input", f"only a version that the checks generated gets the verdict of the reviewer: this one is {row['status']}")
    with database.write() as u:
        s = u.session
        last = s.one("SELECT test_json FROM user_rule_versions WHERE id = :i", {"i": int(row["id"])})
        s.insert("user_rule_reviews", {"rule_version_id": int(row["id"]), "brief_id": int(row["brief_id"]), "code_sha256": row["code_sha256"], "test_json": last["test_json"],
                                       "verdict": verdict, "findings_json": json.dumps(items), "reviewer": who})
        if verdict == "rejected":
            s.execute("UPDATE user_rule_versions SET status = 'rejected', review_note = :n WHERE id = :i AND status = 'generated'", {"n": "; ".join(f["message"] for f in items)[:1900], "i": int(row["id"])})
        user_rules._event(s, int(row["id"]), "generated", "rejected" if verdict == "rejected" else "generated", ("rejected: " if verdict == "rejected" else "approved by the reviewer: ") +
                          ("; ".join(f["message"] for f in items)[:400] or "logic matches the spec"), who)
    return {"verdict": verdict, "status": "rejected" if verdict == "rejected" else "generated", "rule_version_id": int(row["id"]), "registry_id": row["registry_id"], "ben_approved": verdict == "approved"}


def ben_approved(session: Any, rule_version_id: int, code_sha256: str) -> bool:
    """The last review of the version is an approval of this code by someone who is not the automatic checks."""
    last = session.one("SELECT verdict, reviewer, code_sha256 FROM user_rule_reviews WHERE rule_version_id = :i ORDER BY id DESC LIMIT 1", {"i": rule_version_id})
    return last is not None and last["verdict"] == "approved" and last["reviewer"] != CHECKS_BY and last["code_sha256"] == code_sha256


def load_class(row: Any) -> type:
    """The class of the registry for a version of this flow. The text must be the one that was hashed and still have the format."""
    from hokma.factory.user_rules import UserRuleError

    text = row["code_text"]
    if static_check.code_sha256(text) != row["code_sha256"]:
        raise UserRuleError("blocked", f"{row['registry_id']} does not load: the code is not the one that was hashed")
    problems, defs = loader.read(text)
    if problems:
        raise UserRuleError("blocked", f"{row['registry_id']} does not have the format: {problems[0].text()}")
    return adapter.build_class(row["registry_id"], defs, text, loader.provider)


def space_for(strategy_id: str, names: tuple[str, ...] | None = None, db: Database | None = None) -> Any:
    """PARAMS of a strategy of this flow as the search space of the optimizer, or None when the strategy is not of this flow."""
    if not isinstance(strategy_id, str) or not strategy_id.startswith("u_"):
        return None
    with (db or get_database()).read() as u:
        row = u.session.one("SELECT manifest_json FROM user_rule_versions WHERE registry_id = :r", {"r": strategy_id})
    if row is None or not is_pandas(row["manifest_json"]):
        return None
    return contract.space_of(defs_of(row["manifest_json"]), names)
