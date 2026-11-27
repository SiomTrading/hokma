"""The creation of a strategy in pandas, with all its paperwork done by the code (1.3.0, task 3433).

The creator gives text: the spec and the code. Everything else that a strategy needs to exist in Hokma is done here, in order, with no call of the creator in between:
the checks (dry run), the source and the card of the bank (the file of the idea), the number STR-NNN and its folder with spec.md, checklist.md, plan.md, tasks.md, adr/, results/ and versions/v1/strategy.py,
the requirement (brief) and the saved version that the checks generate (the link with its spec), the tasks of the road (the review of Ben and the backtest), the entry of the journal and the measure of the creation.
Nothing is written if the checks do not pass: the answer is the list of problems with the change that fixes each one.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from hokma import __version__
from hokma.db import Actor, Database, get_database
from hokma.factory import bank, creation_metrics, pandas_flow, user_rules
from hokma.factory.pandas_strategy import checks
from hokma.factory.pandas_strategy.checks import DryRun
from hokma.factory.user_rules import UserRuleError
from hokma.sources import ingest

LINES = ("RF", "DT", "SWC", "SWO", "LP")
MAX_SPEC = 60_000
MIN_SPEC = 40
CACHE_SECONDS = 30 * 60
_CACHE: dict[tuple[str, int], tuple[float, DryRun]] = {}
_LOCK = threading.Lock()


def bar_seconds_of(timeframe: str | None) -> int:
    from hokma.backtest.spec import SpecError, bar_seconds

    try:
        return bar_seconds(timeframe or "5m")
    except SpecError as exc:
        raise UserRuleError("bad_input", f"timeframe: {exc}") from None


def dry_run(code: str, timeframe: str | None = None) -> tuple[str, DryRun]:
    """The dry run of a text, remembered for half an hour: the same text on the same timeframe gives the same answer, so strategy_create does not repeat what the creator just saw."""
    if not isinstance(code, str) or not code.strip():
        raise UserRuleError("bad_input", "code is the text of the strategy")
    text = pandas_flow.text_of(code)
    seconds = bar_seconds_of(timeframe)
    key = (text, seconds)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None and time.monotonic() - hit[0] < CACHE_SECONDS:
            return text, hit[1]
    report = checks.dry_run(text, bar_seconds=seconds)
    with _LOCK:
        _CACHE[key] = (time.monotonic(), report)
        for stale in [k for k, (at, _r) in _CACHE.items() if time.monotonic() - at >= CACHE_SECONDS]:
            _CACHE.pop(stale, None)
    return text, report


def _text(value: Any, name: str, low: int, high: int) -> str:
    if not isinstance(value, str) or not low <= len(value.strip()) <= high:
        raise UserRuleError("bad_input", f"{name} is text of {low} to {high} characters")
    if "\x00" in value:
        raise UserRuleError("bad_input", f"{name} has a null character")
    return value.strip()


def _agent_or_none(session: Any, slug: str) -> str | None:
    return slug if session.one("SELECT 1 AS x FROM agents WHERE slug = :s", {"s": slug}) is not None else None


def _write(path: Any, text: str) -> None:
    """Every file of a strategy goes through here: no link or junction in the way, nothing outside the working folder, never through a name that was planted."""
    user_rules.write_text_inside(path, text, user_rules.drafts_dir())


def create(*, title: str, summary: str, spec: str, code: str, caller: str, origin: tuple[int | None, str | None], conversation_id: int | None = None, line: str | None = None,
           market: str | None = None, timeframe: str | None = None, bank_id: int | None = None, db: Database | None = None) -> dict[str, Any]:
    """Create the strategy, or its next version when `bank_id` is given. Returns what was made, or the problems of the checks with nothing made."""
    who = user_rules.require_role(caller, user_rules.CODER, "creating a strategy")
    title = _text(title, "title", 1, 160)
    summary = _text(summary, "summary", 10, 1000)
    spec_text = _text(spec, "spec", MIN_SPEC, MAX_SPEC)
    if line is not None and line not in LINES:
        raise UserRuleError("bad_input", f"line is one of {', '.join(LINES)}")
    database = db or get_database()
    text, report = dry_run(code, timeframe)
    if not report.ok:
        return {"created": False, **report.to_data(), "next": "Fix what the problems say and call strategy_create again; nothing was saved."}
    from hokma.factory.usercode import static_check

    sha = static_check.code_sha256(text)
    with database.read() as u:
        same = u.session.one("SELECT registry_id, status FROM user_rule_versions WHERE code_sha256 = :h", {"h": sha})
    if same is not None:
        return {"created": False, "duplicate": True, "registry_id": same["registry_id"], "status": same["status"], "next": "This exact code is already saved: use that strategy."}
    if user_rules.PREMIUM_WORDS.search(" ".join([title, summary, spec_text])):
        raise UserRuleError("blocked", "selling option premium is off: this strategy cannot be built")
    if bank_id is None:
        source = ingest.ingest_text(spec_text, kind="text", reference=title[:300], rights="own", caller=who)
        made = bank.create(title=title, source_id=source.id, extraction=bank.idea_sheet(summary), line=line or "DT", caller=who, origin=origin, db=database)
        bank_ref, slug = made.bank_id, made.slug
    else:
        with database.read() as u:
            card = u.session.one("SELECT id, slug FROM strategy_bank WHERE id = :i", {"i": int(bank_id)})
        if card is None:
            raise UserRuleError("no_data", f"there is no strategy {bank_id} in the bank")
        bank_ref, slug = int(card["id"]), card["slug"]
    str_code = user_rules.code_of(bank_ref)
    root = user_rules.drafts_dir()
    folder = root / f"{str_code}-{slug}"
    user_rules.make_folder(folder, root, new=bank_id is None)       # a new card never takes a folder that was already there; a link, a junction or a path outside is refused
    for sub in ("adr", "results"):
        user_rules.make_folder(folder / sub, root)
    if bank_id is None or not (folder / "spec.md").exists():
        _write(folder / "spec.md", f"# {str_code} {title}\n\n- Code: {str_code}\n- Name: {title}\n- Bank id: {bank_ref}\n- Created by: {who} (strategy in pandas, flow of Hokma 1.3.0)\n- Folder: {user_rules.rel_folder(folder)}\n\n{spec_text}\n")
    else:
        user_rules.check_inside(folder / "spec.md", root)
        old = (folder / "spec.md").read_text(encoding="utf-8")
        _write(folder / "spec.md", old.rstrip("\n") + f"\n\n## Change of the next version\n\n{spec_text}\n")
    brief: dict[str, Any] = {"summary": summary}
    if market:
        brief["market"] = market[:40]
    if timeframe:
        brief["timeframe"] = timeframe[:10]
    rule_id = None
    if bank_id is not None:
        with database.read() as u:
            row = u.session.one("SELECT id FROM user_rules WHERE kind = 'strategy' AND bank_id = :b ORDER BY id DESC LIMIT 1", {"b": bank_ref})
        rule_id = int(row["id"]) if row else None
    written = user_rules.write_brief(brief=brief, caller=who, bank_id=bank_ref, title=title if rule_id is None else None, rule_id=rule_id, origin=origin, db=database)
    with database.read() as u:
        brief_row = u.session.one("SELECT id, request_version FROM user_rule_briefs WHERE id = :i", {"i": written["brief_id"]})
        number = int(u.session.scalar("SELECT COALESCE(MAX(version), 0) FROM user_rule_versions WHERE rule_id = :r", {"r": written["rule_id"]})) + 1
    registry_id = f"u_str{bank_ref:03d}_v{number}"
    version_file = user_rules.version_folder(bank_ref, slug, number) / "strategy.py"
    if os.path.lexists(version_file):
        raise UserRuleError("blocked", f"versions/v{number}/strategy.py already exists: a saved version is never written over")
    _write(version_file, text)
    stored = pandas_flow.store_version(rule_id=written["rule_id"], brief_id=int(brief_row["id"]), request_version=int(brief_row["request_version"]), registry_id=registry_id, number=number,
                                       text=text, defs=report.defs, report={"stats": report.stats, "seconds": round(report.seconds, 2)}, caller=who, origin=origin, db=database)
    user_rules.versions_row(bank_ref, slug, number, sha, author=who, reviewer=pandas_flow.CHECKS_BY, verdict="checks passed: ready for backtest and optimizer; Ben before paper")
    tasks = _tasks(database, who, str_code, title, folder, number, stored["rule_version_id"], registry_id, report)
    _paperwork(folder, str_code, title, number, registry_id, report, tasks)
    measure = creation_metrics.finish(conversation_id=conversation_id, bank_id=bank_ref, str_code=str_code, agent=who, hokma_version=__version__, db=database)
    return {"created": True, "code": str_code, "bank_id": bank_ref, "slug": slug, "version": number, "registry_id": registry_id, "rule_version_id": stored["rule_version_id"],
            "status": "generated", "folder": user_rules.rel_folder(folder), "spec_path": user_rules.rel_folder(folder) + "/spec.md",
            "code_path": user_rules.rel_folder(user_rules.version_folder(bank_ref, slug, number)) + "/strategy.py", "params": [p["name"] for p in report.to_data()["params"]],
            "checks": report.stats, "tasks": tasks,
            "next": f"{registry_id} is ready for backtest_run and backtest_optimize (without a space, the optimizer walks PARAMS). The review of Ben (task {tasks['review']}) comes before paper or real money; the owner approves real money.",
            "measure": measure}


def _tasks(database: Database, who: str, str_code: str, title: str, folder: Any, number: int, rule_version_id: int, registry_id: str, report: DryRun) -> dict[str, int]:
    """The tasks of the road, made by the code: two already done, the review of Ben waiting and the backtest after it."""
    spec_path = user_rules.rel_folder(folder) + "/spec.md"
    actor = Actor.agent(who)
    with database.write() as u:
        mine = _agent_or_none(u.session, who)
        ben = _agent_or_none(u.session, user_rules.REVIEWER)
        built = u.tasks.create(f"{str_code} v{number}: spec and code written", actor=actor, body=f"Spec: {spec_path}. Version {registry_id}.", status="done", assigned_agent=mine)
        passed = u.tasks.create(f"{str_code} v{number}: checks passed", actor=actor, status="done", assigned_agent=mine, parents=[built.id],
                                body=f"Runs, decides, deterministic, does not look at the future ({report.stats.get('cuts')} cuts), {report.stats.get('bars')} bars. Version {registry_id}.")
        review = u.tasks.create(f"{str_code} v{number}: review the logic against the spec (Ben)", actor=actor, status="ready", assigned_agent=ben, parents=[passed.id],
                                body=f"Read {spec_path} and the code of {registry_id} (strategy_code_get, rule_version_id {rule_version_id}). Judge whether the logic does what the spec says. Do NOT repeat the checks: they passed. "
                                     f"Answer with strategy_code_verdict. Without your approval the strategy does not go to paper or real money.")
        backtest = u.tasks.create(f"{str_code} v{number}: backtest and optimize", actor=actor, status="todo", assigned_agent=mine, parents=[passed.id],
                                  body=f"backtest_run and backtest_optimize on {registry_id} (the space is its PARAMS), then the verdict of the optimizer. Spec: {spec_path}.")
        u.journal.add("action", f"{str_code} v{number} created: {title}", actor=actor, outcome="success", agent_slug=mine, task_id=built.id,
                      body=f"Strategy in pandas {registry_id}. Checks passed with {report.stats.get('cuts')} cuts. Waiting for the review of Ben (task {review.id}) before paper.")
    return {"written": built.id, "checks": passed.id, "review": review.id, "backtest": backtest.id}


def _paperwork(folder: Any, str_code: str, title: str, number: int, registry_id: str, report: DryRun, tasks: dict[str, int]) -> None:
    s = report.stats
    _write(folder / "checklist.md", f"# Checklist {str_code} v{number}\n\n- [x] has the format (PARAMS and generate_signals) and loads\n- [x] runs with the default parameters and with every parameter at its min and at its max\n"
                                    f"- [x] decides ({s.get('entries_long')} long and {s.get('entries_short')} short entries on the sample)\n- [x] deterministic\n- [x] does not look at the future ({s.get('cuts')} cuts, the changes of session and of regular hours included)\n"
                                    f"- [ ] review of the logic by Ben (task {tasks['review']})\n- [ ] backtest and optimizer\n")
    _write(folder / "plan.md", f"# Plan {str_code}\n\nOne step, done by the code: the strategy is text (PARAMS and generate_signals), checked by Hokma and saved as {registry_id}. The signal is made at the close and the order goes at the open of the next bar; the timeframe is chosen in each run.\n")
    _write(folder / "tasks.md", f"# Tasks {str_code}\n\n- [x] spec and code written (task {tasks['written']})\n- [x] checks passed (task {tasks['checks']})\n- [ ] review of Ben (task {tasks['review']})\n- [ ] backtest and optimize (task {tasks['backtest']})\n")
