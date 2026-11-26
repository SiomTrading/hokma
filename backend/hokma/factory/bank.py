"""The bank of strategies (SPEC-030, A1-7): every strategy the creator builds, with its versions, its source and its state.

A strategy of the bank has versions. Each version holds the extraction sheet of a source (its five components with their citations), the rules the factory
does not have yet (`needs_new_rule`) and, when everything maps to pieces of the factory, the saved spec that stands for it (`strategy_specs`). A version that
has a rule pending never has a spec: nothing is approximated. The state comes from the sheet (complete: draft, incomplete: idea) and `bank_set_status`
can close a strategy as not working, with the reason. The states of the test, of the ready strategy and of the live one belong to the next milestones and are
refused here. Every change of state writes an event, in the same transaction, and the events are append only.

The caller is always set by the code (the agent that called the tool), never by an argument.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from hokma.db import Database, get_database
from hokma.db.errors import IntegrityViolation
from hokma.desks.core.signal import LINES
from hokma.factory import spec as factory_spec
from hokma.factory.registry import Registry
from hokma.sources import extraction as ext
from hokma.sources.ingest import SourceError

STATUSES = ("idea", "draft", "testing", "ready", "not_working", "live", "off")
A1_STATUSES = ("idea", "draft", "not_working")
LATER = ("testing", "ready", "live", "off")
TRANSITIONS = frozenset({("idea", "draft"), ("idea", "not_working"), ("draft", "not_working"), ("draft", "idea")})
MAX_TITLE = 160
MAX_SLUG = 80
SLUG_TRIES = 5
REASON_RANGE = (20, 4_000)
BLOCKED_LATER = "this state belongs to milestone B or C"


class BankError(SourceError):
    """A classified failure of the bank: bad_input, no_data or blocked."""


@dataclass(frozen=True)
class BankVersion:
    bank_id: int
    slug: str
    version: int
    version_id: int
    status: str
    complete: bool
    missing: list[str] = field(default_factory=list)
    needs_new_rule: list[dict[str, Any]] = field(default_factory=list)
    proposed: list[str] = field(default_factory=list)
    citations_checked: int = 0
    strategy: dict[str, Any] | None = None


def _db(db: Database | None) -> Database:
    return db or get_database()


def _caller(caller: Any) -> str:
    if not isinstance(caller, str) or not caller.strip():
        raise BankError("bad_input", "the caller is the slug of the agent")
    return caller.strip()


def _whole(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BankError("bad_input", f"{name} is a whole number from 1")
    return value


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:MAX_SLUG].strip("-")
    return slug or "strategy"


def _unique_slug(session: Any, title: str) -> str:
    base = slugify(title)
    slug, n = base, 1
    while session.one("SELECT 1 AS x FROM strategy_bank WHERE slug = :s", {"s": slug}) is not None:
        n += 1
        tail = f"-{n}"
        slug = base[:MAX_SLUG - len(tail)] + tail
    return slug


# ---------------------------------------------------------------------------------------------------- the spec that stands for a sheet
def _check_spec(sheet: ext.Extraction, strategy_id: str, spec_version: int, line: str | None, registry: Registry, db: Database) -> None:
    """The saved spec must be the sheet: same line, the same alpha with the same parameters and the same sizer, exit rule, execution and filters."""
    if sheet.assessment.needs_new_rule:
        raise BankError("bad_input", "a sheet with a rule pending has no spec: nothing is approximated with another rule")
    if not sheet.mapped:
        raise BankError("bad_input", "the extraction does not map every component to a piece of the factory, so no spec can stand for it")
    try:
        row = factory_spec.load(strategy_id, spec_version, db=db)
    except factory_spec.BadSpec:
        raise BankError("no_data", f"there is no saved spec {strategy_id} version {spec_version}", ["Save it with factory_draft first."]) from None
    if line is not None and row.line != line:
        raise BankError("bad_input", f"the spec is of the line {row.line} and the strategy of {line}")
    spec = factory_spec.parse_spec(row.spec_yaml, registry)
    try:
        got = ext.plan_of({"signal_logic": {"map": {"alpha": spec.alpha, "params": dict(spec.params)}}, **_spec_maps(spec)}, registry)
        want = ext.plan_of(sheet.data, registry)
    except ext.ExtractionError as exc:
        raise BankError("bad_input", f"the spec cannot be compared with the extraction: {exc.message}") from None
    if got != want:
        differs = sorted(k for k in want if want[k] != got.get(k))
        raise BankError("bad_input", "the spec does not use the alpha or parts named in the extraction (" + ", ".join(differs) + ")",
                        ["Write the spec from the maps of the extraction, or fix the maps."])


def _spec_maps(spec: factory_spec.StrategySpec) -> dict[str, Any]:
    """The parts of a saved spec in the shape of a sheet, so both go through the same reading (the filters and the controls are one list)."""
    parts = dict(spec.parts or {})
    return {"exit": {"map": parts.get("exit", {"kind": "none"})}, "sizing": {"map": parts.get("sizer", {"kind": "fixed_contracts", "n": 1})},
            "execution": {"value": "x", "map": parts.get("execution", {"kind": "market"})}, "risk_controls": {"map": []},
            "entry": {"filters": {"value": list(parts.get("filters", []))}}}


# ---------------------------------------------------------------------------------------------------- create
def idea_sheet(idea: str) -> dict[str, Any]:
    """The sheet of an idea that comes alone, with no source to cite: every field is not specified, so nothing is guessed and the strategy is born as an idea.
    Ian fills it in later, with the next version of the card, from the spec he writes."""
    from hokma.sources import extraction as xt

    blank = {"value": xt.NOT_SPECIFIED}
    sheet: dict[str, Any] = {"schema": xt.SCHEMA, "summary": " ".join(idea.split())[:xt.MAX_SUMMARY], "instruments": dict(blank), "timeframe": dict(blank),
                             "execution": dict(blank), "needs_new_rule": []}
    for component, keys in xt.LEAF_KEYS.items():
        sheet[component] = {key: dict(blank) for key in keys}
    return sheet


def _bank_row(session: Any, bank_id: int) -> dict[str, Any]:
    row = session.one("SELECT * FROM strategy_bank WHERE id = :i", {"i": bank_id})
    if row is None:
        raise BankError("no_data", f"there is no strategy {bank_id} in the bank")
    return row


def _event(session: Any, bank_id: int, from_status: str | None, to_status: str, reason: str | None, actor: str) -> int:
    return session.insert("strategy_bank_events", {"bank_id": bank_id, "from_status": from_status, "to_status": to_status, "reason": reason, "actor": actor})


def create(*, title: str, source_id: int, extraction: Any, line: str | None = None, strategy_id: str | None = None, spec_version: int | None = None,
           bank_id: int | None = None, derived_from: int | None = None, caller: str, origin: tuple[int | None, str | None] = (None, None),
           registry: Registry | None = None, db: Database | None = None) -> BankVersion:
    """A strategy of the bank, or its next version: the sheet checked against its source, the spec that stands for it when everything maps, the state that
    the sheet gives and the event, all in one transaction. Without `bank_id` it creates the strategy and its version 1; with it, the next version, derived
    from the last one unless `derived_from` says another."""
    who = _caller(caller)
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= MAX_TITLE:
        raise BankError("bad_input", f"title is text of 1 to {MAX_TITLE} characters")
    if (strategy_id is None) != (spec_version is None):
        raise BankError("bad_input", "strategy_id and spec_version go together")
    if strategy_id is not None and (not isinstance(strategy_id, str) or not strategy_id.strip()):
        raise BankError("bad_input", "strategy_id is the id of a saved spec")
    if spec_version is not None:
        _whole(spec_version, "spec_version")
    if line is not None and line not in LINES:
        raise BankError("bad_input", f"line is one of {', '.join(LINES)}")
    for name, value in (("bank_id", bank_id), ("derived_from", derived_from), ("source_id", source_id)):
        if value is not None:
            _whole(value, name)
    database = _db(db)
    from hokma.factory.spec import default_registry

    registry = registry or default_registry()
    sheet = ext.validate_extraction(extraction, source_id, registry=registry, db=database, title=title)
    chosen_line = line if line is not None else sheet.line
    if line is not None and sheet.line is not None and line != sheet.line:
        raise BankError("bad_input", f"the extraction says the line {sheet.line} and the call {line}")
    code_ref = strategy_id is not None and strategy_id.startswith("u_")
    if code_ref:                                         # a strategy that is code stands for its sheet only after the code reviewer approved it
        from hokma.factory import user_rules

        try:
            user_rules.require_stage(strategy_id, user_rules.APPROVED, "the code reviewer has not approved it", db=database)
        except user_rules.UserRuleError as exc:
            raise BankError("blocked", exc.message) from None
    elif strategy_id is not None:
        _check_spec(sheet, strategy_id, spec_version, chosen_line, registry, database)
    status = sheet.status
    creating = bank_id is None

    def attempt(bank_id: int | None) -> tuple[int, str, int, int, list[dict[str, Any]]]:
        with database.write() as u:
            s = u.session
            previous: str | None = None
            parent: int | None = None
            if bank_id is not None:
                bank = _bank_row(s, bank_id)
                if bank["status"] == "not_working":
                    raise BankError("blocked", "a strategy that does not work gets no new versions: start a new strategy that derives from it", 
                                    ["Call bank_create without bank_id and with derived_from."])
                if bank["status"] not in A1_STATUSES:
                    raise BankError("blocked", BLOCKED_LATER)
                previous = bank["status"]
                last = s.one("SELECT id, version FROM strategy_bank_versions WHERE bank_id = :b ORDER BY version DESC LIMIT 1", {"b": bank_id})
                version = int(last["version"]) + 1 if last else 1
                parent = int(last["id"]) if last else None
            else:
                version = 1
            if derived_from is not None:
                found = s.one("SELECT id, bank_id FROM strategy_bank_versions WHERE id = :i", {"i": derived_from})
                if found is None:
                    raise BankError("no_data", f"there is no version {derived_from} in the bank")
                if bank_id is not None and int(found["bank_id"]) != bank_id:
                    raise BankError("bad_input", "derived_from is a version of this same strategy; to derive from another strategy start a new one")
                parent = int(found["id"])
            if not s.one("SELECT 1 AS x FROM strategy_sources WHERE id = :i", {"i": source_id}):
                raise BankError("no_data", f"there is no source {source_id}")
            if bank_id is None:
                slug = _unique_slug(s, title)
                bank_id = s.insert("strategy_bank", {"slug": slug, "title": title.strip(), "line": chosen_line, "status": status,
                                                  "origin_desk_id": origin[0], "origin_desk_name": origin[1]})
            else:
                slug = bank["slug"]
                if s.execute("UPDATE strategy_bank SET status = :st, line = COALESCE(:l, line), updated_at = CURRENT_TIMESTAMP WHERE id = :i AND status = :old",
                             {"st": status, "l": chosen_line, "i": bank_id, "old": previous}) != 1:
                    raise BankError("bad_input", "the strategy changed while its version was being saved: read it again")
            pending = sheet.assessment.needs_new_rule
            version_id = s.insert("strategy_bank_versions", {
                "bank_id": bank_id, "version": version, "source_id": source_id, "derived_from": parent,
                "extraction_json": json.dumps(sheet.data, ensure_ascii=True, sort_keys=True), "extraction_hash": sheet.hash,
                "needs_rule_json": json.dumps([] if code_ref else pending, ensure_ascii=True, sort_keys=True), "strategy_id": strategy_id, "spec_version": spec_version,
                "created_by": who, "origin_desk_id": origin[0], "origin_desk_name": origin[1]})
            _event(s, bank_id, previous, status, f"version {version} created from source {source_id}", who)
            return bank_id, slug, version, version_id, pending

    # The slug is picked before the insert, so two callers can pick the same one: the UNIQUE of the table decides and the loser starts again with the next suffix.
    for tries in range(SLUG_TRIES):
        try:
            bank_id, slug, version, version_id, pending = attempt(bank_id)
            break
        except IntegrityViolation as exc:
            if not (creating and "slug" in str(exc).lower()):
                raise BankError("bad_input", f"the strategy could not be saved: {exc}") from None
            if tries == SLUG_TRIES - 1:
                raise BankError("blocked", "the slug of the strategy was taken at the same time by other callers", ["Try again."]) from None
    pending = [] if code_ref else pending                  # the code answers the rule that was pending
    return BankVersion(bank_id, slug, version, version_id, status, sheet.complete, sheet.assessment.missing, pending, sheet.assessment.proposed,
                       sheet.citations_checked, None if strategy_id is None else {"strategy_id": strategy_id, "spec_version": spec_version})


# ---------------------------------------------------------------------------------------------------- list and get
def _origin(r: dict[str, Any]) -> dict[str, Any]:
    """Where a strategy or one of its versions was created: Hokma (the main chat) or a desk."""
    desk = r.get("origin_desk_id")
    return {"kind": "hokma"} if desk is None else {"kind": "desk", "desk_id": int(desk), "desk_name": r.get("origin_desk_name")}


def _row_out(r: dict[str, Any]) -> dict[str, Any]:
    return {"bank_id": int(r["id"]), "slug": r["slug"], "title": r["title"], "line": r["line"], "status": r["status"], "version": int(r["version"]),
            "source": {"kind": r["source_kind"], "reference": r["source_reference"]}, "needs_new_rule": r["needs_rule_json"] != "[]",
            "strategy_id": r["strategy_id"], "spec_version": r["spec_version"], "updated_at": str(r["updated_at"]), "origin": _origin(r), "code": None,
            "agent_reviewed": r.get("code_status") in ("generated", "owner_reviewed"), "owner_reviewed": r.get("code_status") == "owner_reviewed"}


def list_bank(*, status: str | None = None, line: str | None = None, needs_rule: bool | None = None, origin: str | None = None, limit: int = 100,
              db: Database | None = None) -> dict[str, Any]:
    if status is not None and status not in STATUSES:
        raise BankError("bad_input", f"status is one of {', '.join(STATUSES)}")
    if line is not None and line not in LINES:
        raise BankError("bad_input", f"line is one of {', '.join(LINES)}")
    if needs_rule is not None and not isinstance(needs_rule, bool):
        raise BankError("bad_input", "needs_rule is true or false")
    if origin not in (None, "hokma", "desk"):
        raise BankError("bad_input", "origin is hokma or desk")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise BankError("bad_input", "limit is a whole number from 1 to 200")
    where, params = ["1 = 1"], {}
    if status is not None:
        where.append("b.status = :st")
        params["st"] = status
    if line is not None:
        where.append("b.line = :ln")
        params["ln"] = line
    if needs_rule is not None:
        where.append("v.needs_rule_json <> '[]'" if needs_rule else "v.needs_rule_json = '[]'")
    if origin is not None:
        where.append("v.origin_desk_id IS NULL" if origin == "hokma" else "v.origin_desk_id IS NOT NULL")
    base = ("FROM strategy_bank b JOIN strategy_bank_versions v ON v.bank_id = b.id AND v.version = (SELECT MAX(version) FROM strategy_bank_versions WHERE bank_id = b.id) "
            "JOIN strategy_sources s ON s.id = v.source_id LEFT JOIN user_rule_versions uv ON uv.registry_id = v.strategy_id WHERE " + " AND ".join(where))
    with _db(db).read() as u:
        total = int(u.session.scalar("SELECT COUNT(*) " + base, params))
        rows = u.session.query("SELECT b.id, b.slug, b.title, b.line, b.status, b.updated_at, v.version, v.needs_rule_json, v.strategy_id, v.spec_version, "
                               "v.origin_desk_id, v.origin_desk_name, uv.status AS code_status, s.kind AS source_kind, s.reference AS source_reference " + base + " ORDER BY b.updated_at DESC, b.id DESC LIMIT :limit",
                               {**params, "limit": limit})
    return {"rows": [_row_out(r) for r in rows], "total": total}


def get(bank_id: int | None = None, *, slug: str | None = None, version: int | None = None, db: Database | None = None) -> dict[str, Any]:
    """A strategy of the bank with one version (the latest by default), its source, its versions and its events. The text of a source is given only when
    the source is the owner's own: for a third party the rule, the reference and the locators are all there is."""
    if (bank_id is None) == (slug is None):
        raise BankError("bad_input", "give bank_id or slug, one of them")
    if version is not None:
        _whole(version, "version")
    with _db(db).read() as u:
        s = u.session
        if bank_id is not None:
            bank = _bank_row(s, _whole(bank_id, "bank_id"))
        else:
            if not isinstance(slug, str) or not slug.strip():
                raise BankError("bad_input", "slug is text")
            bank = s.one("SELECT * FROM strategy_bank WHERE slug = :s", {"s": slug.strip()})
            if bank is None:
                raise BankError("no_data", f"there is no strategy {slug!r} in the bank")
        versions = s.query("SELECT id, version, source_id, derived_from, strategy_id, spec_version, evidence_stage, needs_rule_json, created_by, created_at, origin_desk_id, origin_desk_name "
                           "FROM strategy_bank_versions WHERE bank_id = :b ORDER BY version", {"b": bank["id"]})
        chosen = next((v for v in versions if version is None or int(v["version"]) == version), None) if version is not None else (versions[-1] if versions else None)
        if chosen is None:
            raise BankError("no_data", f"the strategy {bank['slug']} has no version {version}")
        full = s.one("SELECT extraction_json, extraction_hash FROM strategy_bank_versions WHERE id = :i", {"i": chosen["id"]})
        code = s.one("SELECT status FROM user_rule_versions WHERE registry_id = :r", {"r": chosen["strategy_id"]}) if chosen["strategy_id"] else None
        extraction = json.loads(full["extraction_json"])
        src = s.one("SELECT id, kind, reference, url, rights FROM strategy_sources WHERE id = :i", {"i": chosen["source_id"]})
        wanted = sorted(ext._cited_ids(extraction) | {c for n in json.loads(chosen["needs_rule_json"]) for c in n.get("cite", [])})
        passages = []
        for pid in wanted:
            p = s.one("SELECT id, locator, body FROM strategy_source_passages WHERE id = :i AND source_id = :s", {"i": pid, "s": src["id"]})
            if p is not None:
                item = {"passage_id": int(p["id"]), "locator": p["locator"]}
                if src["rights"] == "own":
                    item["body"] = p["body"]
                passages.append(item)
        events = s.query("SELECT id, from_status, to_status, reason, actor, created_at FROM strategy_bank_events WHERE bank_id = :b ORDER BY id", {"b": bank["id"]})
    return {"bank_id": int(bank["id"]), "slug": bank["slug"], "title": bank["title"], "line": bank["line"], "status": bank["status"],
            "close_reason": bank["close_reason"], "pivots_used": int(bank["pivots_used"]),
            "version": {"version": int(chosen["version"]), "version_id": int(chosen["id"]), "derived_from": chosen["derived_from"], "evidence_stage": chosen["evidence_stage"],
                        "extraction": extraction, "extraction_hash": full["extraction_hash"], "needs_new_rule": json.loads(chosen["needs_rule_json"]),
                        "strategy_id": chosen["strategy_id"], "spec_version": chosen["spec_version"], "created_by": chosen["created_by"],
                        "created_at": str(chosen["created_at"]), "origin": _origin(chosen), "code": None,
                        "agent_reviewed": code is not None and code["status"] in ("generated", "owner_reviewed"),
                        "owner_reviewed": code is not None and code["status"] == "owner_reviewed"},
            "source": {"source_id": int(src["id"]), "kind": src["kind"], "reference": src["reference"], "url": src["url"], "rights": src["rights"],
                       "cited": passages},
            "versions": [{"version": int(v["version"]), "version_id": int(v["id"]), "derived_from": v["derived_from"], "strategy_id": v["strategy_id"],
                          "spec_version": v["spec_version"], "needs_new_rule": v["needs_rule_json"] != "[]"} for v in versions],
            "events": [{"event_id": int(e["id"]), "from": e["from_status"], "to": e["to_status"], "reason": e["reason"], "actor": e["actor"], "at": str(e["created_at"])}
                       for e in events]}


# ---------------------------------------------------------------------------------------------------- state
def _move(session: Any, bank_id: int, from_status: str, to_status: str, reason: str | None, actor: str) -> int:
    """The compare and set of a change of state: the row changes only if it still has the state that was read, and the event is written with it."""
    close = reason if to_status == "not_working" else None
    changed = session.execute("UPDATE strategy_bank SET status = :to, close_reason = :r, updated_at = CURRENT_TIMESTAMP WHERE id = :i AND status = :old",
                              {"to": to_status, "r": close, "i": bank_id, "old": from_status})
    if changed != 1:
        raise BankError("bad_input", "the strategy changed while its state was being changed: read it again")
    return _event(session, bank_id, from_status, to_status, reason, actor)


def set_status(bank_id: int, status: str, *, reason: str | None = None, actor: str, db: Database | None = None) -> dict[str, Any]:
    who = _caller(actor)
    _whole(bank_id, "bank_id")
    if status in LATER:
        raise BankError("blocked", BLOCKED_LATER)
    if status not in STATUSES:
        raise BankError("bad_input", f"status is one of {', '.join(A1_STATUSES)}")
    if reason is not None and (not isinstance(reason, str) or len(reason) > REASON_RANGE[1]):
        raise BankError("bad_input", f"reason is text of at most {REASON_RANGE[1]} characters")
    if status == "not_working" and (reason is None or not REASON_RANGE[0] <= len(reason.strip()) <= REASON_RANGE[1]):
        raise BankError("bad_input", f"closing a strategy as not working needs its reason, {REASON_RANGE[0]} to {REASON_RANGE[1]} characters")
    with _db(db).write() as u:
        s = u.session
        bank = _bank_row(s, bank_id)
        current = bank["status"]
        if (current, status) not in TRANSITIONS:
            if current in LATER:
                raise BankError("blocked", BLOCKED_LATER)
            raise BankError("bad_input", f"a strategy in {current} does not go to {status}" + (": it is closed, start a new strategy that derives from it" if current == "not_working" else ""))
        if status == "draft":
            last = s.one("SELECT extraction_json FROM strategy_bank_versions WHERE bank_id = :b ORDER BY version DESC LIMIT 1", {"b": bank_id})
            if last is None or not ext.assess(json.loads(last["extraction_json"])).complete:
                raise BankError("bad_input", "the extraction is incomplete, so the strategy stays an idea: fill what is missing or propose it, with its reason")
        event = _move(s, bank_id, current, status, reason.strip() if reason else None, who)
    return {"bank_id": bank_id, "from": current, "to": status, "event_id": event}
