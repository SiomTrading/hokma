"""The bank of strategies (SPEC-030, A1-7): create, version, list, read, states and events, on SQLite and on Postgres."""
import itertools
import threading

import pytest
from bank_support import IDEA, db, env, pending_sheet, pg_admin_url, pg_template, pg_url_migrated, sheet, spec_yaml, src  # noqa: F401  (fixtures)

from hokma.factory import bank, spec as factory_spec
from hokma.sources import ingest

AGENT = "strategy-researcher"


def idea(db, text=IDEA, **kw):
    source = ingest.ingest_text(text, caller=AGENT, db=db, **kw)
    return source, ingest.read_passages(source.id, db=db).passages[0].passage_id


def make(db, title="Morning EMA cross", **kw):
    source, pid = idea(db)
    data = kw.pop("data", None) or sheet(pid)
    return source, pid, bank.create(title=title, source_id=source.id, extraction=data, caller=AGENT, db=db, **kw)


def rows(db, table):
    with db.read() as u:
        return u.session.query(f"SELECT * FROM {table} ORDER BY id")


def refused(code, fragment=None):
    caught = pytest.raises(bank.BankError)
    return caught, code, fragment


def fails(code, fragment, call):
    with pytest.raises(bank.SourceError) as caught:
        call()
    assert caught.value.code == code and fragment in caught.value.message, caught.value.message


# ---------------------------------------------------------------------------------------------------- create
def test_a_strategy_is_created_with_its_first_version_its_state_and_its_event(db):
    source, pid, made = make(db)
    assert (made.bank_id, made.slug, made.version, made.status, made.complete, made.strategy) == (1, "morning-ema-cross", 1, "draft", True, None)
    assert made.proposed == ["signal_logic.inputs", "sizing.method", "execution"] and made.citations_checked == 1 and made.missing == [] and made.needs_new_rule == []
    [version] = rows(db, "strategy_bank_versions")
    assert (version["source_id"], version["derived_from"], version["created_by"], version["evidence_stage"], version["needs_rule_json"]) == (source.id, None, AGENT, "hypothesis", "[]")
    [event] = rows(db, "strategy_bank_events")
    assert (event["from_status"], event["to_status"], event["actor"]) == (None, "draft", AGENT) and "version 1" in event["reason"]
    [row] = rows(db, "strategy_bank")
    assert (row["title"], row["line"], row["status"], row["close_reason"], row["pivots_used"]) == ("Morning EMA cross", "DT", "draft", None, 0)


def test_an_incomplete_sheet_makes_an_idea(db):
    source, pid = idea(db)
    data = sheet(pid)
    data["exit"]["stop"] = {"value": "not_specified"}
    made = bank.create(title="Half an idea", source_id=source.id, extraction=data, caller=AGENT, db=db)
    assert made.status == "idea" and not made.complete and "exit.stop" in made.missing


def test_the_spec_that_stands_for_the_sheet_is_linked_when_it_uses_the_same_alpha_and_parts(db):
    source, pid = idea(db)
    saved = factory_spec.save_draft(spec_yaml(), db=db)
    made = bank.create(title="Morning EMA cross", source_id=source.id, extraction=sheet(pid), strategy_id=saved.strategy_id, spec_version=saved.version, caller=AGENT, db=db)
    assert made.strategy == {"strategy_id": "idea_ema_morning", "spec_version": 1} and made.status == "draft"
    [version] = rows(db, "strategy_bank_versions")
    assert (version["strategy_id"], version["spec_version"]) == ("idea_ema_morning", 1)


@pytest.mark.parametrize("over", [{"alpha": "macd", "params": "{}"}, {"params": "{fast: 5, slow: 21}"}, {"line": "SWC"},
                                 {"parts": "parts:\n  sizer: {kind: fixed_risk, risk_pct: 0.5}\n  exit: {kind: atr, length: 14, stop_atr: 1.5, r_multiple: 2.0}\n  execution: {kind: market}\n"
                                           "  filters:\n    - {kind: session, start: '09:45', end: '11:30'}\n"},      # the entry limit is missing
                                 {"parts": "parts:\n  sizer: {kind: fixed_risk, risk_pct: 1.0}\n  exit: {kind: atr, length: 14, stop_atr: 1.5, r_multiple: 2.0}\n  execution: {kind: market}\n"
                                           "  filters:\n    - {kind: session, start: '09:45', end: '11:30'}\n    - {kind: max_trades_per_day, n: 2}\n"},
                                 {"parts": "parts:\n  sizer: {kind: fixed_risk, risk_pct: 0.5}\n  exit: {kind: none}\n  execution: {kind: market}\n"
                                           "  filters:\n    - {kind: session, start: '09:45', end: '11:30'}\n    - {kind: max_trades_per_day, n: 2}\n"},
                                 {"parts": "parts:\n  sizer: {kind: fixed_risk, risk_pct: 0.5}\n  exit: {kind: atr, length: 14, stop_atr: 1.5, r_multiple: 2.0}\n  execution: {kind: twap, n_slices: 3}\n"
                                           "  filters:\n    - {kind: session, start: '09:45', end: '11:30'}\n    - {kind: max_trades_per_day, n: 2}\n"}])
def test_a_spec_that_does_not_say_what_the_sheet_says_is_refused_and_nothing_is_written(db, over):
    source, pid = idea(db)
    saved = factory_spec.save_draft(spec_yaml(**over), db=db)
    fails("bad_input", "the spec does not use the alpha or parts named in the extraction" if "line" not in over else "line",
          lambda: bank.create(title="x", source_id=source.id, extraction=sheet(pid), strategy_id=saved.strategy_id, spec_version=saved.version, caller=AGENT, db=db))
    assert rows(db, "strategy_bank") == [] and rows(db, "strategy_bank_versions") == [] and rows(db, "strategy_bank_events") == []


def test_a_spec_that_is_not_there_or_a_pair_that_is_not_a_pair_is_classified(db):
    source, pid = idea(db)
    fails("no_data", "no saved spec", lambda: bank.create(title="x", source_id=source.id, extraction=sheet(pid), strategy_id="ghost", spec_version=1, caller=AGENT, db=db))
    fails("bad_input", "go together", lambda: bank.create(title="x", source_id=source.id, extraction=sheet(pid), strategy_id="ghost", caller=AGENT, db=db))
    fails("bad_input", "go together", lambda: bank.create(title="x", source_id=source.id, extraction=sheet(pid), spec_version=1, caller=AGENT, db=db))


def test_a_rule_pending_leaves_the_version_without_a_spec_and_a_spec_cannot_be_forced_on_it(db):
    source, pid = idea(db)
    made = bank.create(title="Delta divergence", source_id=source.id, extraction=pending_sheet(pid), caller=AGENT, db=db)
    assert made.status == "draft" and made.strategy is None and [n["component"] for n in made.needs_new_rule] == ["signal_logic"]
    [version] = rows(db, "strategy_bank_versions")
    assert version["strategy_id"] is None and version["needs_rule_json"] != "[]"
    saved = factory_spec.save_draft(spec_yaml(), db=db)
    fails("bad_input", "rule pending", lambda: bank.create(title="y", source_id=source.id, extraction=pending_sheet(pid), strategy_id=saved.strategy_id,
                                                        spec_version=saved.version, caller=AGENT, db=db))
    incomplete_map = sheet(pid)
    del incomplete_map["exit"]["map"]
    fails("bad_input", "does not map every component", lambda: bank.create(title="z", source_id=source.id, extraction=incomplete_map, strategy_id=saved.strategy_id,
                                                                         spec_version=saved.version, caller=AGENT, db=db))


def test_a_cite_that_is_not_of_the_source_stops_everything(db):
    source, pid = idea(db)
    other, other_pid = idea(db, "Another idea: buy when the RSI is below 30 on the daily chart.")
    data = sheet(pid)
    data["timeframe"] = src(other_pid, "5m")
    fails("bad_input", "not a passage of source", lambda: bank.create(title="x", source_id=source.id, extraction=data, caller=AGENT, db=db))
    assert rows(db, "strategy_bank") == []
    fails("no_data", "no source", lambda: bank.create(title="x", source_id=999, extraction=data, caller=AGENT, db=db))


@pytest.mark.parametrize("kw, fragment", [({"title": ""}, "title"), ({"title": "t" * 161}, "title"), ({"line": "XX"}, "line"), ({"caller": " "}, "caller"),
                                         ({"bank_id": 0}, "bank_id"), ({"derived_from": True}, "derived_from")])
def test_the_arguments_of_create_are_checked(db, kw, fragment):
    source, pid = idea(db)
    args = {"title": "ok", "source_id": source.id, "extraction": sheet(pid), "caller": AGENT, "db": db, **kw}
    fails("bad_input", fragment, lambda: bank.create(**args))


def test_the_line_of_the_call_and_the_line_of_the_sheet_cannot_disagree(db):
    source, pid = idea(db)
    fails("bad_input", "line", lambda: bank.create(title="x", source_id=source.id, extraction=sheet(pid), line="SWC", caller=AGENT, db=db))
    data = sheet(pid)
    data["line"] = None
    made = bank.create(title="x", source_id=source.id, extraction=data, line="LP", caller=AGENT, db=db)
    assert bank.get(made.bank_id, db=db)["line"] == "LP"


def test_the_slug_comes_from_the_title_and_is_unique(db):
    source, pid = idea(db)
    slugs = [bank.create(title=t, source_id=source.id, extraction=sheet(pid), caller=AGENT, db=db).slug for t in ("Morning EMA cross", "Morning EMA cross!", "???", "\u00f1" * 3)]
    assert slugs == ["morning-ema-cross", "morning-ema-cross-2", "strategy", "strategy-2"]


# ---------------------------------------------------------------------------------------------------- versions
def test_the_next_version_derives_from_the_last_and_the_state_follows_the_sheet(db):
    source, pid, first = make(db)
    worse = sheet(pid)
    worse["exit"]["stop"] = {"value": "not_specified"}
    second = bank.create(title="Morning EMA cross", source_id=source.id, extraction=worse, bank_id=first.bank_id, caller="vera", db=db)
    assert (second.bank_id, second.version, second.status) == (first.bank_id, 2, "idea")
    third = bank.create(title="Morning EMA cross", source_id=source.id, extraction=sheet(pid), bank_id=first.bank_id, caller=AGENT, db=db)
    assert (third.version, third.status) == (3, "draft")
    versions = rows(db, "strategy_bank_versions")
    assert [(v["version"], v["derived_from"], v["created_by"]) for v in versions] == [(1, None, AGENT), (2, first.version_id, "vera"), (3, second.version_id, AGENT)]
    events = rows(db, "strategy_bank_events")
    assert [(e["from_status"], e["to_status"]) for e in events] == [(None, "draft"), ("draft", "idea"), ("idea", "draft")]
    assert bank.get(first.bank_id, db=db)["status"] == "draft"


def test_derived_from_can_point_to_an_earlier_version_of_the_same_strategy_or_start_a_new_one(db):
    source, pid, first = make(db)
    second = bank.create(title="t", source_id=source.id, extraction=sheet(pid), bank_id=first.bank_id, caller=AGENT, db=db)
    third = bank.create(title="t", source_id=source.id, extraction=sheet(pid), bank_id=first.bank_id, derived_from=first.version_id, caller=AGENT, db=db)
    assert rows(db, "strategy_bank_versions")[2]["derived_from"] == first.version_id and third.version == 3 and second.version == 2
    _other, other_pid, other = make(db, "Another")
    fails("bad_input", "this same strategy", lambda: bank.create(title="t", source_id=source.id, extraction=sheet(pid), bank_id=first.bank_id,
                                                              derived_from=other.version_id, caller=AGENT, db=db))
    fresh = bank.create(title="Derived", source_id=source.id, extraction=sheet(pid), derived_from=other.version_id, caller=AGENT, db=db)
    assert fresh.bank_id != other.bank_id and fresh.version == 1
    fails("no_data", "no version", lambda: bank.create(title="t", source_id=source.id, extraction=sheet(pid), derived_from=9999, caller=AGENT, db=db))
    fails("no_data", "no strategy", lambda: bank.create(title="t", source_id=source.id, extraction=sheet(pid), bank_id=9999, caller=AGENT, db=db))


def test_a_strategy_that_does_not_work_gets_no_new_versions(db):
    source, pid, made = make(db)
    bank.set_status(made.bank_id, "not_working", reason="the rule depends on a data feed that is not available", actor="hokma", db=db)
    with pytest.raises(bank.BankError) as caught:
        bank.create(title="t", source_id=source.id, extraction=sheet(pid), bank_id=made.bank_id, caller=AGENT, db=db)
    assert caught.value.code == "blocked" and "derives from it" in caught.value.message + " ".join(caught.value.suggestions)
    fresh = bank.create(title="t2", source_id=source.id, extraction=sheet(pid), derived_from=made.version_id, caller=AGENT, db=db)
    assert fresh.bank_id != made.bank_id


# ---------------------------------------------------------------------------------------------------- states
ALLOWED = {("idea", "draft"), ("idea", "not_working"), ("draft", "not_working"), ("draft", "idea")}


def put_in(db, state):
    """A strategy in the given state: through the front door where there is one."""
    source, pid = idea(db)
    data = sheet(pid)
    if state == "idea":
        data["exit"]["stop"] = {"value": "not_specified"}
    made = bank.create(title=f"in {state}", source_id=source.id, extraction=data, caller=AGENT, db=db)
    if state in ("idea", "draft"):
        return made.bank_id
    if state == "not_working":
        bank.set_status(made.bank_id, "not_working", reason="closed for the test of the states", actor="hokma", db=db)
    else:
        with db.write() as u:
            u.session.execute("UPDATE strategy_bank SET status = :s WHERE id = :i", {"s": state, "i": made.bank_id})
    return made.bank_id


@pytest.mark.parametrize("start, goal", [(a, b) for a in ("idea", "draft") for b in bank.STATUSES if a != b])
def test_every_transition_of_a_state_that_a1_uses_is_allowed_or_refused_with_its_code(db, start, goal):
    bank_id = put_in(db, start)
    before = len(rows(db, "strategy_bank_events"))
    call = lambda: bank.set_status(bank_id, goal, reason="a reason that is long enough to count", actor="hokma", db=db)    # noqa: E731
    if goal in bank.LATER:
        fails("blocked", "milestone B or C", call)
    elif (start, goal) == ("idea", "draft"):
        fails("bad_input", "incomplete", call)                          # this idea is missing its stop, so it cannot be a draft
    elif (start, goal) in ALLOWED:
        done = call()
        assert (done["from"], done["to"]) == (start, goal) and len(rows(db, "strategy_bank_events")) == before + 1
        return
    else:
        fails("bad_input", "does not go to", call)
    assert len(rows(db, "strategy_bank_events")) == before and bank.get(bank_id, db=db)["status"] == start


def test_an_idea_with_a_complete_sheet_goes_to_draft_and_a_draft_goes_back_to_idea(db):
    source, pid, made = make(db)
    worse = sheet(pid)
    worse["timeframe"] = {"value": "not_specified"}
    del worse["timeframe"]
    bank.create(title="t", source_id=source.id, extraction=worse, bank_id=made.bank_id, caller=AGENT, db=db)
    assert bank.get(made.bank_id, db=db)["status"] == "idea"
    fails("bad_input", "incomplete", lambda: bank.set_status(made.bank_id, "draft", actor="hokma", db=db))
    bank.create(title="t", source_id=source.id, extraction=sheet(pid), bank_id=made.bank_id, caller=AGENT, db=db)
    assert bank.get(made.bank_id, db=db)["status"] == "draft"
    done = bank.set_status(made.bank_id, "idea", reason="the owner wants to rewrite the entry", actor="joel", db=db)
    assert (done["from"], done["to"]) == ("draft", "idea")
    assert bank.set_status(made.bank_id, "draft", actor="joel", db=db)["to"] == "draft"


@pytest.mark.parametrize("state", ["testing", "ready", "live", "off"])
def test_a_strategy_in_a_later_state_cannot_be_moved_or_versioned_here(db, state):
    bank_id = put_in(db, state)
    fails("blocked", "milestone B or C", lambda: bank.set_status(bank_id, "idea", actor="hokma", db=db))
    source, pid = idea(db, "Yet another idea about carry that nobody wrote before.")
    fails("blocked", "milestone B or C", lambda: bank.create(title="t", source_id=source.id, extraction=sheet(pid), bank_id=bank_id, caller=AGENT, db=db))


@pytest.mark.parametrize("reason", [None, "", "too short", " " * 30, "x" * 4001])
def test_closing_as_not_working_needs_the_reason(db, reason):
    _s, _p, made = make(db)
    fails("bad_input", "reason", lambda: bank.set_status(made.bank_id, "not_working", reason=reason, actor="hokma", db=db))
    assert bank.get(made.bank_id, db=db)["status"] == "draft"


def test_the_reason_of_a_close_is_kept_with_the_strategy_and_in_its_event_and_it_is_not_reopened(db):
    _s, _p, made = make(db)
    done = bank.set_status(made.bank_id, "not_working", reason="  the rule depends on a data feed that we do not have  ", actor="vera", db=db)
    got = bank.get(made.bank_id, db=db)
    assert got["status"] == "not_working" and got["close_reason"] == "the rule depends on a data feed that we do not have"
    assert got["events"][-1] == {"event_id": done["event_id"], "from": "draft", "to": "not_working", "reason": got["close_reason"], "actor": "vera", "at": got["events"][-1]["at"]}
    for goal in ("idea", "draft", "not_working"):
        fails("bad_input", "closed", lambda: bank.set_status(made.bank_id, goal, reason="a reason that is long enough", actor="vera", db=db))


def test_a_state_and_an_id_that_do_not_exist_are_classified(db):
    fails("bad_input", "status is one of", lambda: bank.set_status(1, "frozen", actor="hokma", db=db))
    fails("no_data", "no strategy", lambda: bank.set_status(77, "idea", actor="hokma", db=db))
    fails("bad_input", "bank_id", lambda: bank.set_status(0, "idea", actor="hokma", db=db))
    fails("bad_input", "caller", lambda: bank.set_status(1, "idea", actor="", db=db))


def test_the_compare_and_set_refuses_a_state_that_changed_after_it_was_read(db):
    _s, _p, made = make(db)
    with db.write() as u:
        with pytest.raises(bank.BankError) as caught:
            bank._move(u.session, made.bank_id, "idea", "not_working", "a reason that is long enough", "hokma")          # it was read as idea; it is draft
    assert caught.value.code == "bad_input" and "changed" in caught.value.message
    assert len(rows(db, "strategy_bank_events")) == 1


def test_two_callers_closing_the_same_strategy_at_once_one_wins(db):
    _s, _p, made = make(db)
    gate = threading.Barrier(2)
    outcome: list = []

    def close(who):
        gate.wait()
        try:
            outcome.append(bank.set_status(made.bank_id, "not_working", reason=f"closed by {who} for this reason", actor=who, db=db))
        except bank.BankError as exc:
            outcome.append(exc)

    threads = [threading.Thread(target=close, args=(w,)) for w in ("hokma", "vera")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(type(o).__name__ for o in outcome) == ["BankError", "dict"]
    assert len([e for e in rows(db, "strategy_bank_events") if e["to_status"] == "not_working"]) == 1


def test_the_events_cannot_be_changed_or_deleted(db):
    make(db)
    from hokma.db.errors import DbError

    for sql in ("UPDATE strategy_bank_events SET reason = 'x'", "DELETE FROM strategy_bank_events"):
        with pytest.raises(DbError):
            with db.write() as u:
                u.session.execute(sql)


# ---------------------------------------------------------------------------------------------------- list and get
def fill(db):
    source, pid = idea(db)
    a = bank.create(title="Alpha", source_id=source.id, extraction=sheet(pid), caller=AGENT, db=db)
    pending = pending_sheet(pid)
    pending["line"] = "SWC"
    b = bank.create(title="Bravo", source_id=source.id, extraction=pending, caller=AGENT, db=db)
    thin = sheet(pid)
    thin["line"] = "DT"
    del thin["instruments"]
    c = bank.create(title="Charlie", source_id=source.id, extraction=thin, caller=AGENT, db=db)
    bank.set_status(a.bank_id, "not_working", reason="the rule depends on a feed that does not exist", actor="hokma", db=db)
    return a, b, c


def test_the_list_filters_by_state_line_and_pending_rule_and_counts_the_total(db):
    a, b, c = fill(db)
    everything = bank.list_bank(db=db)
    assert everything["total"] == 3 and [r["slug"] for r in everything["rows"]] == ["alpha", "charlie", "bravo"] or {r["slug"] for r in everything["rows"]} == {"alpha", "bravo", "charlie"}
    assert [r["slug"] for r in bank.list_bank(status="idea", db=db)["rows"]] == ["charlie"]
    assert [r["slug"] for r in bank.list_bank(status="not_working", db=db)["rows"]] == ["alpha"]
    assert [r["slug"] for r in bank.list_bank(line="SWC", db=db)["rows"]] == ["bravo"]
    assert [r["slug"] for r in bank.list_bank(needs_rule=True, db=db)["rows"]] == ["bravo"]
    assert {r["slug"] for r in bank.list_bank(needs_rule=False, db=db)["rows"]} == {"alpha", "charlie"}
    assert bank.list_bank(status="draft", line="DT", db=db) == {"rows": [], "total": 0}
    one = bank.list_bank(limit=1, db=db)
    assert len(one["rows"]) == 1 and one["total"] == 3
    row = next(r for r in everything["rows"] if r["slug"] == "bravo")
    assert row["version"] == 1 and row["needs_new_rule"] is True and row["source"] == {"kind": "text", "reference": row["source"]["reference"]} and row["strategy_id"] is None
    assert b.bank_id == row["bank_id"] and c.version == 1


def test_the_list_shows_the_latest_version_of_each_strategy(db):
    source, pid, made = make(db)
    pending = pending_sheet(pid)
    bank.create(title="t", source_id=source.id, extraction=pending, bank_id=made.bank_id, caller=AGENT, db=db)
    [row] = bank.list_bank(db=db)["rows"]
    assert row["version"] == 2 and row["needs_new_rule"] is True


@pytest.mark.parametrize("kw", [{"status": "frozen"}, {"line": "XX"}, {"needs_rule": "yes"}, {"limit": 0}, {"limit": 201}, {"limit": True}])
def test_a_bad_filter_of_the_list_is_bad_input(db, kw):
    with pytest.raises(bank.BankError) as caught:
        bank.list_bank(db=db, **kw)
    assert caught.value.code == "bad_input"


def test_get_gives_the_strategy_its_versions_its_source_and_its_events_and_the_text_only_of_an_own_source(db):
    source, pid, made = make(db)
    bank.create(title="t", source_id=source.id, extraction=sheet(pid), bank_id=made.bank_id, caller=AGENT, db=db)
    got = bank.get(made.bank_id, db=db)
    assert (got["slug"], got["status"], got["version"]["version"], got["version"]["derived_from"]) == ("morning-ema-cross", "draft", 2, 1)
    assert [v["version"] for v in got["versions"]] == [1, 2] and [e["to"] for e in got["events"]] == ["draft", "draft"]
    assert got["source"]["rights"] == "own" and got["source"]["cited"] == [{"passage_id": pid, "locator": "paragraph 1", "body": IDEA}]
    assert bank.get(slug="morning-ema-cross", version=1, db=db)["version"]["version"] == 1
    assert got["version"]["extraction"]["signal_logic"]["map"]["alpha"] == "ema_cross"


def test_get_of_a_third_party_source_has_the_rule_the_reference_and_the_locators_and_no_text(db):
    text = "Chapter four explains that the trader waits for confirmation. " * 3
    source = ingest.ingest_text(text, rights="third_party", reference="A book, chapter 4", caller=AGENT, db=db)
    pid = ingest.read_passages(source.id, db=db).passages[0].passage_id
    made = bank.create(title="From a book", source_id=source.id, extraction=sheet(pid), caller=AGENT, db=db)
    got = bank.get(made.bank_id, db=db)
    assert got["source"]["reference"] == "A book, chapter 4" and got["source"]["cited"] == [{"passage_id": pid, "locator": "paragraph 1"}]
    assert "waits for confirmation" not in str(got)
    unknown = ingest.ingest_text(text + "More.", rights="unknown", caller=AGENT, db=db)
    upid = ingest.read_passages(unknown.id, db=db).passages[0].passage_id
    got = bank.get(bank.create(title="Unknown", source_id=unknown.id, extraction=sheet(upid), caller=AGENT, db=db).bank_id, db=db)
    assert all("body" not in p for p in got["source"]["cited"])


def test_get_names_a_strategy_by_id_or_slug_and_classifies_what_is_not_there(db):
    make(db)
    for call, code in ((lambda: bank.get(db=db), "bad_input"), (lambda: bank.get(1, slug="x", db=db), "bad_input"), (lambda: bank.get(55, db=db), "no_data"),
                       (lambda: bank.get(slug="ghost", db=db), "no_data"), (lambda: bank.get(1, version=9, db=db), "no_data"), (lambda: bank.get(1, version=0, db=db), "bad_input")):
        with pytest.raises(bank.BankError) as caught:
            call()
        assert caught.value.code == code


def test_a_pending_rule_is_in_get_with_its_cite_and_the_locator_of_the_cited_passage(db):
    source, pid = idea(db)
    made = bank.create(title="Delta", source_id=source.id, extraction=pending_sheet(pid), caller=AGENT, db=db)
    got = bank.get(made.bank_id, db=db)
    assert got["version"]["needs_new_rule"][0]["component"] == "signal_logic" and got["version"]["strategy_id"] is None
    assert {p["passage_id"] for p in got["source"]["cited"]} == {pid}


def test_the_title_cannot_carry_twelve_words_of_a_third_party_source(db):
    text = "Chapter four explains that the trader waits for confirmation before entering the market on the retest of the level. " * 2
    source = ingest.ingest_text(text, rights="third_party", caller=AGENT, db=db)
    pid = ingest.read_passages(source.id, db=db).passages[0].passage_id
    quoted = " ".join(text.split()[:12])
    fails("bad_input", "title", lambda: bank.create(title=quoted, source_id=source.id, extraction=sheet(pid), caller=AGENT, db=db))
    assert rows(db, "strategy_bank") == []


def test_a_source_ingested_again_as_third_party_gives_no_text_from_get_and_the_ingest_says_so(db):
    source, pid, made = make(db)
    assert bank.get(made.bank_id, db=db)["source"]["cited"][0]["body"] == IDEA
    again = ingest.ingest_text(IDEA, rights="third_party", caller=AGENT, db=db)
    assert again.duplicate and again.warnings
    got = bank.get(made.bank_id, db=db)
    assert got["source"]["rights"] == "third_party" and all("body" not in p for p in got["source"]["cited"]) and IDEA not in str(got)


def test_a_slug_taken_between_the_check_and_the_insert_is_retried_with_the_next_suffix(db, monkeypatch):
    source, pid = idea(db)
    bank.create(title="Morning EMA cross", source_id=source.id, extraction=sheet(pid), caller=AGENT, db=db)
    real, calls = bank._unique_slug, []

    def racy(session, title):                       # the first answer is the slug that the other caller already took
        calls.append(title)
        return "morning-ema-cross" if len(calls) == 1 else real(session, title)

    monkeypatch.setattr(bank, "_unique_slug", racy)
    made = bank.create(title="Morning EMA cross", source_id=source.id, extraction=sheet(pid), caller=AGENT, db=db)
    assert made.slug == "morning-ema-cross-2" and len(calls) == 2 and len(rows(db, "strategy_bank")) == 2


def test_a_slug_that_stays_taken_is_classified_and_nothing_is_written(db, monkeypatch):
    source, pid = idea(db)
    bank.create(title="Morning EMA cross", source_id=source.id, extraction=sheet(pid), caller=AGENT, db=db)
    monkeypatch.setattr(bank, "_unique_slug", lambda session, title: "morning-ema-cross")
    fails("blocked", "slug", lambda: bank.create(title="Morning EMA cross", source_id=source.id, extraction=sheet(pid), caller=AGENT, db=db))
    assert len(rows(db, "strategy_bank")) == 1 and len(rows(db, "strategy_bank_versions")) == 1
