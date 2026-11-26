"""The extraction sheet (SPEC-030, A1-6): five components, citations, proposals, maps to the factory, pending rules and copied text."""
import copy

import pytest
from bank_support import IDEA, NS, db, env, pending_sheet, pg_admin_url, pg_template, pg_url_migrated, proposed, sheet, src  # noqa: F401  (fixtures)

from hokma.sources import extraction, ingest

AGENT = "strategy-researcher"


def idea(db, text=IDEA, **kw):
    source = ingest.ingest_text(text, caller=AGENT, db=db, **kw)
    return source, ingest.read_passages(source.id, db=db).passages[0].passage_id


def check(db, data, source_id):
    return extraction.validate_extraction(data, source_id, db=db)


def refused(db, data, source_id, fragment=None, code="bad_input"):
    with pytest.raises(extraction.ExtractionError) as caught:
        check(db, data, source_id)
    assert caught.value.code == code
    if fragment:
        assert fragment in caught.value.message
    return caught.value


def test_a_complete_sheet_is_a_draft_with_its_citations_checked_and_its_proposals_listed(db):
    source, pid = idea(db)
    got = check(db, sheet(pid), source.id)
    assert got.complete and got.status == "draft" and got.mapped and got.assessment.missing == [] and got.assessment.needs_new_rule == []
    assert got.citations_checked == 1 and got.line == "DT"
    assert got.assessment.proposed == ["signal_logic.inputs", "sizing.method", "execution"]       # nothing proposed is shown as the source


def test_the_hash_is_of_the_sheet_and_does_not_depend_on_the_order_of_the_keys(db):
    source, pid = idea(db)
    one = check(db, sheet(pid), source.id)
    shuffled = dict(reversed(list(sheet(pid).items())))
    assert check(db, shuffled, source.id).hash == one.hash and len(one.hash) == 64
    changed = sheet(pid)
    changed["summary"] = "Another summary of the same strategy."
    assert check(db, changed, source.id).hash != one.hash


def test_a_sheet_with_a_field_missing_is_an_idea_and_names_what_is_missing(db):
    source, pid = idea(db)
    data = sheet(pid)
    del data["timeframe"]
    data["exit"]["stop"] = dict(NS)
    del data["exit"]["map"]
    got = check(db, data, source.id)
    assert not got.complete and got.status == "idea"
    assert got.assessment.missing == ["timeframe", "exit.stop", "exit.map"] and not got.mapped


def test_a_field_with_not_specified_is_present_and_the_four_that_must_be_stated_are_not_allowed_blank(db):
    source, pid = idea(db)
    data = sheet(pid)
    assert check(db, data, source.id).complete                      # time_exit, reversal and the others are blank and the sheet is complete
    for path in ("signal_logic.rule", "entry.trigger", "sizing.method"):
        node = copy.deepcopy(data)
        head, leaf = path.split(".")
        node[head][leaf] = dict(NS)
        assert path in check(db, node, source.id).assessment.missing, path


def test_a_field_from_the_source_needs_a_real_cite(db):
    source, pid = idea(db)
    for cite in ([], None, [0], ["1"], [True], list(range(1, 52))):
        data = sheet(pid)
        data["timeframe"] = {"value": "5m", "origin": "source", "cite": cite} if cite is not None else {"value": "5m", "origin": "source"}
        refused(db, data, source.id, "cite")


def test_a_cite_to_a_passage_that_does_not_exist_or_of_another_source_is_refused_and_nothing_is_saved(db):
    source, pid = idea(db)
    other, other_pid = idea(db, "A different idea about RSI: buy when it is below thirty.")
    data = sheet(pid)
    data["timeframe"] = src(other_pid, "5m")
    refused(db, data, source.id, f"not a passage of source {source.id}")
    data["timeframe"] = src(99999, "5m")
    refused(db, data, source.id, "99999")
    data = sheet(pid)
    data["needs_new_rule"] = [{"component": "exit", "rule": "close when the order book thins out", "cite": [other_pid]}]
    data["exit"].pop("map")
    refused(db, data, source.id, "not a passage")


def test_a_proposal_needs_its_why_and_is_never_the_source(db):
    source, pid = idea(db)
    for why in (None, "", "short", "x" * 401):
        data = sheet(pid)
        data["sizing"]["method"] = {"value": "fixed risk", "origin": "proposed", **({} if why is None else {"why": why})}
        refused(db, data, source.id, "why")
    data = sheet(pid)
    data["sizing"]["method"] = {"value": "fixed risk", "origin": "proposed", "why": "the idea says nothing about size", "cite": [pid]}
    refused(db, data, source.id, "sizing.method")                   # a proposal that carries a cite is not allowed: it would look like the source
    data["sizing"]["method"] = {"value": "fixed risk", "origin": "owner", "why": "the owner said so in the chat"}
    assert check(db, data, source.id).complete


@pytest.mark.parametrize("leaf, fragment", [({"value": "5m"}, "origin"), ({"value": "not_specified", "origin": "source", "cite": [1]}, "not_specified"),
                                           ({"value": "5m", "origin": "guess", "cite": [1]}, "origin is one of"), ({"origin": "source", "cite": [1]}, "value"),
                                           ("5m", "object"), ({"value": "not_specified", "extra": 1}, "has the keys")])
def test_a_field_has_one_of_the_three_forms(db, leaf, fragment):
    source, pid = idea(db)
    data = sheet(pid)
    data["timeframe"] = leaf
    refused(db, data, source.id, fragment)


@pytest.mark.parametrize("path, value, fragment", [
    (("signal_logic", "map"), {"alpha": "no_such_alpha", "params": {}}, "not an alpha"),
    (("signal_logic", "map"), {"alpha": "ema_cross", "params": {"fast": 9, "slow": 21, "extra": 1}}, "signal_logic.map"),
    (("signal_logic", "map"), {"alpha": "ema_cross", "params": {"fast": -3}}, "signal_logic.map"),
    (("exit", "map"), {"kind": "laser"}, "exit.map"), (("exit", "map"), {"kind": "atr", "length": 14, "stop_atr": 0, "r_multiple": 2}, "exit.map"),
    (("sizing", "map"), {"kind": "kelly"}, "sizing.map"), (("execution", "map"), {"kind": "iceberg"}, "execution.map"),
    (("risk_controls", "map"), [{"kind": "news_blackout"}], "filters"), (("risk_controls", "map"), [{"kind": "max_trades_per_day", "n": 0}], "filters"),
    (("risk_controls", "map"), "max two", "list of controls")])
def test_a_map_names_only_pieces_of_the_factory_with_parameters_its_schema_accepts(db, path, value, fragment):
    source, pid = idea(db)
    data = sheet(pid)
    if path[0] == "execution":
        data["execution"]["map"] = value
    else:
        data[path[0]][path[1]] = value
    refused(db, data, source.id, fragment)


def test_the_filters_of_the_entry_are_checked_like_the_controls(db):
    source, pid = idea(db)
    data = sheet(pid)
    data["entry"]["filters"] = src(pid, [{"kind": "session", "start": "25:00", "end": "11:30"}])
    refused(db, data, source.id, "HH:MM")
    data["entry"]["filters"] = src(pid, "only in the morning")
    refused(db, data, source.id, "entry.filters")
    data["entry"]["filters"] = src(pid, [{"kind": "session", "start": "09:45", "end": "11:30"}, {"kind": "max_trades_per_day", "n": 1}])
    refused(db, data, source.id, "twice")                           # the same kind in the entry and in the controls


def test_a_rule_the_factory_does_not_have_is_marked_and_has_no_map(db):
    source, pid = idea(db)
    got = check(db, pending_sheet(pid), source.id)
    assert got.complete and got.status == "draft" and not got.mapped
    assert [n["component"] for n in got.assessment.needs_new_rule] == ["signal_logic"]


def test_a_component_with_a_rule_pending_cannot_keep_a_map_that_approximates_it(db):
    source, pid = idea(db)
    data = pending_sheet(pid)
    data["signal_logic"]["map"] = {"alpha": "ema_cross", "params": {"fast": 9, "slow": 21}}
    refused(db, data, source.id, "nothing is approximated")
    for component in ("exit", "sizing", "execution", "risk_controls", "entry.trigger"):
        data = sheet(pid)
        data["needs_new_rule"] = [{"component": component, "rule": "a rule that no registered piece does", "cite": [pid]}]
        refused(db, data, source.id, "nothing is approximated")


@pytest.mark.parametrize("item, fragment", [({"component": "everything", "rule": "a rule in my words", "cite": [1]}, "component is one of"),
                                           ({"component": "exit", "rule": "short", "cite": [1]}, "rule"), ({"component": "exit", "rule": "a rule in my words"}, "cite"),
                                           ({"component": "exit", "rule": "a rule in my words", "cite": []}, "cite")])
def test_a_pending_rule_has_its_component_its_words_and_its_cite(db, item, fragment):
    source, pid = idea(db)
    data = sheet(pid)
    data["exit"].pop("map")
    data["needs_new_rule"] = [item if "cite" not in item or item["cite"] != [1] else {**item, "cite": [pid]}]
    refused(db, data, source.id, fragment)


def test_a_pending_rule_covers_the_map_it_replaces_so_the_sheet_stays_complete(db):
    source, pid = idea(db)
    data = sheet(pid)
    data["exit"].pop("map")
    data["needs_new_rule"] = [{"component": "exit", "rule": "close when the order book thins out", "cite": [pid]}]
    got = check(db, data, source.id)
    assert got.complete and got.assessment.missing == [] and not got.mapped
    data["exit"].pop("stop")
    assert check(db, data, source.id).assessment.missing == ["exit.stop"]


# ---------------------------------------------------------------------------------------------------- copied text
THIRD = ("Our system waits for the nine period average to close above the twenty one period average and then it enters long at the next open provided that "
         "the morning volatility is not extreme.")


def third_party(db, text=THIRD):
    source = ingest.ingest_text(text, rights="third_party", reference="A book, chapter 4", caller=AGENT, db=db)
    return source, ingest.read_passages(source.id, db=db).passages[0].passage_id


def rule_with(db, words):
    source, pid = third_party(db)
    data = sheet(pid)
    data["signal_logic"]["rule"] = src(pid, " ".join(THIRD.split()[:words]))
    return source, data


def test_twelve_words_in_a_row_of_a_third_party_passage_are_refused_and_eleven_are_not(db):
    source, data = rule_with(db, 12)
    refused(db, data, source.id, "written in your words")
    source, data = rule_with(db, 11)
    assert check(db, data, source.id).complete


def test_the_copy_is_found_in_any_text_of_the_sheet_and_ignores_case_and_punctuation(db):
    source, pid = third_party(db)
    quoted = THIRD.upper().replace(" the ", ", the ").split(" at the next open")[0]
    for place in ("summary", "why", "needs"):
        data = sheet(pid)
        if place == "summary":
            data["summary"] = quoted
        elif place == "why":
            data["sizing"]["method"] = proposed("fixed risk", quoted)
        else:
            data["exit"].pop("map")
            data["needs_new_rule"] = [{"component": "exit", "rule": quoted, "cite": [pid]}]
        refused(db, data, source.id, "written in your words")


def test_the_owner_may_repeat_their_own_words_and_a_source_of_unknown_rights_is_treated_as_a_third_party(db):
    own = ingest.ingest_text(THIRD, caller=AGENT, db=db)
    pid = ingest.read_passages(own.id, db=db).passages[0].passage_id
    data = sheet(pid)
    data["signal_logic"]["rule"] = src(pid, " ".join(THIRD.split()[:20]))
    assert check(db, data, own.id).complete
    unknown = ingest.ingest_text(THIRD + " Extra.", rights="unknown", caller=AGENT, db=db)
    upid = ingest.read_passages(unknown.id, db=db).passages[0].passage_id
    data = sheet(upid)
    data["signal_logic"]["rule"] = src(upid, " ".join(THIRD.split()[:20]))
    refused(db, data, unknown.id, "written in your words")


# ---------------------------------------------------------------------------------------------------- shape
@pytest.mark.parametrize("change, fragment", [(lambda d: d.update(schema=2), "schema"), (lambda d: d.update(extra=1), "keys"), (lambda d: d.update(summary=""), "summary"),
                                             (lambda d: d.update(line="XX"), "line"), (lambda d: d["exit"].update(extra=1), "exit"),
                                             (lambda d: d.update(summary="x" * 2001), "summary"), (lambda d: d.update(needs_new_rule="none"), "needs_new_rule"),
                                             (lambda d: d["signal_logic"].update(kind=src(1, {"deep": {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}})), "deep")])
def test_the_shape_of_the_sheet_is_strict(db, change, fragment):
    source, pid = idea(db)
    data = sheet(pid)
    change(data)
    for node in (data.get("signal_logic", {}).get("kind"),):
        if isinstance(node, dict):
            node["cite"] = [pid]
    refused(db, data, source.id, fragment)


def test_a_sheet_that_is_not_an_object_or_is_too_big_or_has_no_source_is_refused(db):
    source, pid = idea(db)
    refused(db, "a sheet", source.id, "object")
    big = sheet(pid)
    big["instruments"] = src(pid, ["x" * 900] * 50)
    big["timeframe"] = src(pid, ["y" * 900] * 50)
    refused(db, big, source.id, "64 KB")
    refused(db, sheet(pid), 9999, code="no_data")
    refused(db, sheet(pid), 0, "whole number")


def test_a_sheet_with_only_a_summary_is_valid_and_is_an_idea(db):
    source, _pid = idea(db)
    got = check(db, {"schema": 1, "summary": "Something about trend following."}, source.id)
    assert got.status == "idea" and "timeframe" in got.assessment.missing and got.citations_checked == 0 and got.line is None


def test_the_plan_of_a_sheet_is_what_the_factory_reads_with_the_defaults_filled_in(db):
    source, pid = idea(db)
    got = check(db, sheet(pid), source.id)
    plan = extraction.plan_of(got.data, got.registry)
    assert plan["alpha"] == "ema_cross" and plan["alpha_params"]["fast"] == 9 and plan["parts"]["sizer"]["risk_pct"] == 0.5 and len(plan["filters"]) == 2
    assert plan == extraction.plan_of(check(db, sheet(pid), source.id).data, got.registry)


def test_the_title_of_the_strategy_is_checked_for_copied_text_like_any_other_words(db):
    source, pid = third_party(db)
    quoted = " ".join(THIRD.split()[:12])
    with pytest.raises(extraction.ExtractionError) as caught:
        extraction.validate_extraction(sheet(pid), source.id, db=db, title=quoted)
    assert caught.value.code == "bad_input" and "title" in caught.value.message and "written in your words" in caught.value.message
    assert extraction.validate_extraction(sheet(pid), source.id, db=db, title=" ".join(THIRD.split()[:11])).complete


@pytest.mark.parametrize("component", ["entry.direction", "entry.timing"])
def test_a_direction_or_a_timing_without_a_piece_is_marked_pending_and_never_approximated(db, component):
    source, pid = idea(db)
    data = sheet(pid)
    data["needs_new_rule"] = [{"component": component, "rule": "only the long side, after the second close above it", "cite": [pid]}]
    got = check(db, data, source.id)
    assert [n["component"] for n in got.assessment.needs_new_rule] == [component] and got.complete
