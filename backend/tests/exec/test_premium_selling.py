"""The premium selling switch at the filter before an order (SPEC-019: ECT-042, spec 4.5, decision 1260)."""
import ast
import dataclasses
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from exec_support import AGENT, db, desks_db, env, ready  # noqa: F401  (fixtures)

from hokma import exec as hexec
from hokma.backoffice.snapshot import AccountSnapshot
from hokma.contracts import identity
from hokma.exec import native
from hokma.exec.guards import GuardContext, pipeline, registry
from hokma.exec.guards import premium_selling as ps
from hokma.exec.guards.premium_selling import PremiumSellingGuard, is_premium_selling, record_calibration, switch_state
from hokma.exec.proposal import Leg, OrderProposal
from hokma.i18n import use_language
from hokma.quant.validation.evidence import EvidenceBundle

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
C600, C610, C590 = "OPT:SPY:2026-12-18:C:600", "OPT:SPY:2026-12-18:C:610", "OPT:SPY:2026-12-18:C:590"
P600, P590, P610 = "OPT:SPY:2026-12-18:P:600", "OPT:SPY:2026-12-18:P:590", "OPT:SPY:2026-12-18:P:610"
C600_LATE = "OPT:SPY:2027-01-15:C:600"
C600_QQQ = "OPT:QQQ:2026-12-18:C:600"
BACKEND = Path(__file__).resolve().parents[2]
HOKMA = BACKEND / "hokma"
MARKS = {C600: Decimal("8"), C610: Decimal("4"), C590: Decimal("12"), P600: Decimal("7"), P590: Decimal("4"), P610: Decimal("10"),
         C600_LATE: Decimal("11"), C600_QQQ: Decimal("5"), "EQ:SPY": Decimal("600")}


def leg(cid, side="buy", effect="open", qty="1"):
    return Leg(cid, side, effect, Decimal(qty))


def prop(*legs, origin="desk", line="SWO", **kw):
    return OrderProposal(1, line, origin, tuple(legs), kw.pop("order_type", "market"), created_by="x", **kw)


def sheets():
    return {c: identity.resolve(c) for c in (*MARKS, "EQ:SPY")}


def book():
    return AccountSnapshot(1, "2026-11-20 15:00:00", Decimal(1_000_000), Decimal(0), Decimal(1_000_000), Decimal(0), Decimal(1_000_000), Decimal(0), Decimal(0),
                           Decimal(0), Decimal(0))


def ctx(p, *, positions=(), marks=None, desk_id=1):
    return GuardContext(proposal=p, contracts=sheets(), positions=positions, book=book(), limits={}, level=2, windows=(), now=NOW, desk_id=desk_id,
                        capital=Decimal(1_000_000), marks=MARKS if marks is None else marks)


def held(cid, qty):
    return SimpleNamespace(contract_id=cid, qty=qty, symbol=cid, market_value=None, multiplier=None, day_pl=None)


def selling(p, **kw):
    return is_premium_selling(p, sheets(), marks=kw.pop("marks", MARKS), positions=kw.pop("positions", ()))


def evidence(**kw):
    base = dict(n_observations=252, n_strategies=1, n_trials=1, best_strategy="short-put", best_sharpe=1.1, cpcv={}, walk_forward={}, pbo=0.1, dsr=0.9, sr0=0.2,
                leakage_free=True)
    base.update(kw)
    return EvidenceBundle(**base)


def calibrate(desk_id, database, underlying="SPY", **kw):
    args = dict(metric="iv_minus_rv", threshold=Decimal("2.5"), evidence=evidence(), backtest_run_ref="bt-2026-11-22-0001", db=database)
    args.update(kw)
    return record_calibration(desk_id, underlying, **args)


@pytest.fixture
def swo(ready):
    with ready.db.write() as u:
        u.session.update("desk_profile", {"desk_id": ready.desk_id}, {"line": "SWO"})
    for cid, mark in MARKS.items():
        ready.board.feed(cid, bid=str(mark - Decimal("0.05")), ask=str(mark + Decimal("0.05")), last=str(mark))
    return ready


# ---- what is premium selling -----------------------------------------------------------------------------------------

def test_a_naked_short_call_or_put_is_premium_selling_ECT_042():
    assert selling(prop(leg(C600, "sell"))) and selling(prop(leg(P600, "sell")))
    assert selling(prop(leg(C600, "sell", qty="3")))


def test_buying_calls_and_puts_is_not_premium_selling_ECT_042():
    assert not selling(prop(leg(C600, "buy"))) and not selling(prop(leg(P600, "buy", qty="5")))
    assert not selling(prop(leg("EQ:SPY", "buy")))


def test_a_debit_call_spread_and_a_debit_put_spread_are_not_premium_selling_ECT_042():
    assert not selling(prop(leg(C600, "buy"), leg(C610, "sell")))          # long the lower strike: pays 8, receives 4
    assert not selling(prop(leg(P610, "buy"), leg(P600, "sell")))          # long the higher strike: pays 10, receives 7


def test_credit_spreads_are_premium_selling_whatever_the_marks_say_ECT_042():
    assert selling(prop(leg(C600, "sell"), leg(C610, "buy")))              # the long strike is on the wrong side: a credit spread
    assert selling(prop(leg(P600, "sell"), leg(P590, "buy")))
    wrong_marks = {**MARKS, C610: Decimal("20")}                           # stale marks that would make the credit spread look like a debit
    assert selling(prop(leg(C600, "sell"), leg(C610, "buy")), marks=wrong_marks)


def test_iron_condor_short_straddle_and_short_strangle_are_premium_selling_ECT_042():
    condor = prop(leg(P590, "buy"), leg(P600, "sell"), leg(C610, "sell"), leg(C590, "buy"))
    assert selling(condor)
    assert selling(prop(leg(C600, "sell"), leg(P600, "sell")))             # short straddle
    assert selling(prop(leg(C610, "sell"), leg(P590, "sell")))             # short strangle


def test_covered_call_and_cash_secured_put_are_premium_selling_ECT_042():
    assert selling(prop(leg("EQ:SPY", "buy", qty="100"), leg(C610, "sell")))     # a share leg in the order covers no option
    assert selling(prop(leg(P600, "sell")))


def test_a_long_leg_of_another_type_underlying_or_size_covers_nothing_ECT_042():
    assert selling(prop(leg(C600, "sell"), leg(P590, "buy")))
    assert selling(prop(leg(C610, "sell"), leg(C600_QQQ, "buy")))
    assert selling(prop(leg(C610, "sell", qty="2"), leg(C600, "buy", qty="1")))   # half covered
    assert not selling(prop(leg(C610, "sell", qty="2"), leg(C600, "buy", qty="2")))


def test_a_calendar_spread_is_a_debit_and_the_reverse_calendar_is_selling_ECT_042():
    assert not selling(prop(leg(C600, "sell"), leg(C600_LATE, "buy")))     # the long expires later: covered, and it costs more than it brings
    assert selling(prop(leg(C600_LATE, "sell"), leg(C600, "buy")))         # the long expires first: it covers nothing


def test_a_covered_structure_whose_credit_cannot_be_priced_counts_as_selling_ECT_042():
    assert selling(prop(leg(C600, "buy"), leg(C610, "sell")), marks={})
    assert selling(prop(leg(C600, "buy"), leg(C610, "sell")), marks={C600: Decimal(8)})


def test_a_short_leg_with_no_contract_sheet_counts_as_selling_ECT_042():
    p = prop(leg(C600, "sell"))
    assert is_premium_selling(p, {}, marks=MARKS, positions=())
    assert not is_premium_selling(prop(leg(C600, "buy")), {}, marks=MARKS, positions=())


def test_closing_positions_is_never_premium_selling_ECT_042():
    positions = (held(C600, "2"), held(P600, "-1"))
    assert not selling(prop(leg(C600, "sell", "close", "2")), positions=positions)       # sell to close a long
    assert not selling(prop(leg(P600, "buy", "close", "1")), positions=positions)        # buy to close a short
    assert not selling(prop(leg(C600, "sell", "close", "1"), leg(P600, "buy", "close", "1")), positions=positions)
    guard = PremiumSellingGuard()
    result = pipeline.run([guard], ctx(prop(leg(C600, "sell", "close", "2")), positions=positions))
    assert result.verdict == "PASS" and result.closing and result.results[0].code == "closing_exempt"


def test_a_leg_declared_as_a_close_that_reduces_nothing_is_an_opening_ECT_042():
    p = prop(leg(C600, "sell", "close", "1"))                                            # the account holds nothing
    assert selling(p)
    assert pipeline.run([PremiumSellingGuard()], ctx(p)).verdict == "BLOCK"
    bigger = prop(leg(C600, "sell", "close", "5"))                                       # holds 2: it cannot close 5
    assert selling(bigger, positions=(held(C600, "2"),))


def test_the_declared_line_and_the_declared_reduce_only_do_not_change_the_answer_ECT_042():
    for line in ("DT", "SWC", "LP", "RF", "SWO"):
        assert pipeline.run([PremiumSellingGuard()], ctx(prop(leg(C600, "sell"), line=line))).verdict == "BLOCK", line
    assert selling(prop(leg(C600, "sell"), reduce_only=True))


def test_one_leg_alone_is_judged_alone_a_structure_split_in_proposals_is_refused_leg_by_leg_ECT_042():
    long_leg, short_leg = prop(leg(C600, "buy")), prop(leg(C610, "sell"))
    assert pipeline.run([PremiumSellingGuard()], ctx(long_leg)).verdict == "PASS"
    assert pipeline.run([PremiumSellingGuard()], ctx(short_leg)).verdict == "BLOCK"
    # even when the account already holds the long leg: it was not in the same order, and the sale opens a new short
    assert pipeline.run([PremiumSellingGuard()], ctx(short_leg, positions=(held(C600, "1"),))).verdict == "BLOCK"


def test_a_declared_net_premium_or_limit_price_does_not_make_a_credit_look_like_a_debit_ECT_042():
    p = prop(leg(C600, "buy"), leg(C610, "sell"), order_type="limit", limit_price=Decimal("0.01"))
    assert not selling(p)                                                                # debit by the marks
    q = prop(leg(C600, "sell"), order_type="limit", limit_price=Decimal("0.01"), thesis="net premium paid, a debit")
    assert selling(q)


# ---- the guard and the switch ----------------------------------------------------------------------------------------

def test_a_premium_selling_proposal_of_a_desk_is_refused_with_the_stable_code_ECT_042(swo):
    swo_proposal = OrderProposal(swo.desk_id, "SWO", "desk", (leg(C610, "sell"),), "market", created_by="desk-chief", thesis="sell premium")
    out = hexec.submit(swo_proposal, who=AGENT, db=swo.db)
    assert (out.status, out.code) == ("refused", "premium_selling_closed")
    assert "SPY" in out.why and "closed" in out.why
    with swo.db.read() as u:
        row = u.session.one("SELECT status, code FROM exec_proposals WHERE id = :i", {"i": out.proposal_id})
    assert (row["status"], row["code"]) == ("refused", "premium_selling_closed")


def test_the_refusal_is_in_the_language_of_the_owner_ECT_042(swo):
    p = OrderProposal(swo.desk_id, "SWO", "rule", (leg(C610, "sell"),), "market", created_by="rule:r1")
    with use_language("es"):
        result = pipeline.run([PremiumSellingGuard()], ctx(p, desk_id=swo.desk_id))
    assert result.blocked_by.code == "premium_selling_closed" and "cerrada" in result.blocked_by.why and "SPY" in result.blocked_by.why
    with use_language("en"):
        assert "closed" in pipeline.run([PremiumSellingGuard()], ctx(p, desk_id=swo.desk_id)).blocked_by.why


def test_a_debit_spread_and_a_purchase_pass_the_whole_filter_ECT_042(swo):
    for legs in ((leg(C600, "buy"),), (leg(C600, "buy"), leg(C610, "sell"))):
        out = hexec.submit(OrderProposal(swo.desk_id, "SWO", "desk", legs, "market", created_by="desk-chief", thesis="debit"), who=AGENT, db=swo.db)
        assert out.status == "analysis_only" and out.code == "level_1", (legs, out)


def test_the_order_of_the_owner_in_the_chat_is_not_subject_to_the_guard_ECT_042(swo):
    p = OrderProposal(swo.desk_id, "SWO", "chat", (leg(C610, "sell"),), "market", created_by="owner", thesis="the owner asks")
    result = pipeline.run([PremiumSellingGuard()], ctx(p, desk_id=swo.desk_id))
    assert result.verdict == "PASS" and result.results[0].code == "chat_exempt"
    out = hexec.submit(p, who=swo.owner, db=swo.db)
    assert out.code != "premium_selling_closed"


def test_an_agent_cannot_pose_as_the_chat_to_dodge_the_guard_ECT_042(swo):
    p = OrderProposal(swo.desk_id, "SWO", "chat", (leg(C610, "sell"),), "market", created_by="desk-chief", thesis="I claim to be the owner")
    out = hexec.submit(p, who=AGENT, db=swo.db)
    assert (out.status, out.code) == ("refused", "premium_selling_closed")


def test_the_guard_sits_between_the_limits_and_the_rules_of_the_broker_and_no_configuration_turns_it_off_ECT_042():
    names = registry.known_types()
    assert names.index("limits") + 1 == names.index("premium_selling") == names.index("stop") - 1 == names.index("broker_rules") - 2          # the stop of an entry (3390e3) sits between it and the rules
    assert "premium_selling" in registry.ALWAYS
    assert [g.name for g in registry.build({"guards": ["whitelist"]})].count("premium_selling") == 1
    assert [g.name for g in registry.build(None)] == list(names)


def test_a_nested_order_cannot_hide_a_sale_in_a_child_ECT_042(swo):
    order = {"orderStrategyType": "TRIGGER", "session": "NORMAL", "duration": "DAY", "orderType": "MARKET",
             "orderLegCollection": [{"instruction": "BUY_TO_OPEN", "quantity": 1, "instrument": {"symbol": "SPY   261218C00600000", "assetType": "OPTION"}}],
             "childOrderStrategies": [{"orderStrategyType": "SINGLE", "session": "NORMAL", "duration": "DAY", "orderType": "MARKET",
                                       "orderLegCollection": [{"instruction": "SELL_TO_OPEN", "quantity": 1,
                                                               "instrument": {"symbol": "SPY   261218C00610000", "assetType": "OPTION"}}]}]}
    refused = native.check(swo.desk_id, order, preview=None, who=AGENT, db=swo.db)
    # after the H2 correction the native filter reads only entry roots with exit children: a sale hidden in an entry child is `cannot_check`
    # (never sent); a sale it can read is `premium_selling_closed`. Either way it is refused.
    assert refused is not None and refused["code"] in ("cannot_check", "premium_selling_closed")
    exit_child = json.loads(json.dumps(order))
    exit_child["childOrderStrategies"][0]["orderLegCollection"][0].update(instruction="SELL_TO_CLOSE")
    exit_child["childOrderStrategies"][0]["orderLegCollection"][0]["instrument"]["symbol"] = "SPY   261218C00600000"
    assert native.check(swo.desk_id, exit_child, preview=None, who=AGENT, db=swo.db) is None      # a real exit is not a sale


def test_a_single_native_naked_short_is_refused_by_the_same_guard_ECT_042(swo):
    order = {"orderStrategyType": "SINGLE", "session": "NORMAL", "duration": "DAY", "orderType": "MARKET",
             "orderLegCollection": [{"instruction": "SELL_TO_OPEN", "quantity": 1, "instrument": {"symbol": "SPY   261218P00600000", "assetType": "OPTION"}}]}
    refused = native.check(swo.desk_id, order, preview=None, who=AGENT, db=swo.db)
    assert refused is not None and refused["code"] == "premium_selling_closed"


def test_the_guard_fails_closed_when_the_table_cannot_be_read_ECT_042(swo, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("the database is gone")

    monkeypatch.setattr(ps, "get_database", boom)
    assert switch_state(swo.desk_id, "SPY").state == "closed" and switch_state(swo.desk_id, "SPY").reason == "unreadable"
    result = pipeline.run([PremiumSellingGuard()], ctx(prop(leg(C610, "sell")), desk_id=swo.desk_id))
    assert result.blocked_by.code == "premium_selling_closed"
    assert pipeline.run([PremiumSellingGuard()], ctx(prop(leg(C600, "buy")), desk_id=swo.desk_id)).verdict == "PASS"


def test_the_guard_fails_closed_when_the_contracts_cannot_be_read_ECT_042(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no sheets")

    monkeypatch.setattr(ps, "is_premium_selling", boom)
    result = pipeline.run([PremiumSellingGuard()], ctx(prop(leg(C610, "sell"))))
    assert result.verdict == "BLOCK" and result.blocked_by.code == "premium_selling_closed"
    broken = dataclasses.replace(ctx(prop(leg(C610, "sell"))), contracts={})
    monkeypatch.undo()
    assert pipeline.run([PremiumSellingGuard()], broken).verdict == "BLOCK"


# ---- the table and who writes it -------------------------------------------------------------------------------------

def test_the_table_of_thresholds_is_empty_and_the_switch_is_closed_for_every_desk_and_underlying_ECT_042(desks_db):
    with desks_db.read() as u:
        assert u.session.one("SELECT count(*) AS n FROM premium_thresholds")["n"] == 0
    state = switch_state(1, "SPY", db=desks_db)
    assert (state.state, state.reason, state.threshold) == ("closed", "no_calibrated_threshold", None)
    for bad in ("", "a b", "x" * 60, None):
        assert switch_state(1, bad, db=desks_db).state == "closed"
    assert switch_state(0, "SPY", db=desks_db).state == "closed" and switch_state(True, "SPY", db=desks_db).state == "closed"


def test_a_calibrated_threshold_opens_the_switch_for_that_desk_and_underlying_only_ECT_042(swo):
    row = calibrate(swo.desk_id, swo.db)
    state = switch_state(swo.desk_id, "spy", db=swo.db)
    assert state.is_open and state.row_id == row and state.threshold == Decimal("2.5") and state.backtest_run_ref == "bt-2026-11-22-0001"
    assert switch_state(swo.desk_id, "QQQ", db=swo.db).state == "closed"
    assert switch_state(swo.desk_id + 1, "SPY", db=swo.db).state == "closed"
    assert pipeline.run([PremiumSellingGuard()], ctx(prop(leg(C610, "sell")), desk_id=swo.desk_id)).results[0].code == "switch_open"
    qqq = pipeline.run([PremiumSellingGuard()], ctx(prop(leg(C600_QQQ, "sell")), desk_id=swo.desk_id))
    assert qqq.blocked_by.code == "premium_selling_closed" and "QQQ" in qqq.blocked_by.why
    both = prop(leg(C610, "sell"), leg(C600_QQQ, "sell"))
    assert pipeline.run([PremiumSellingGuard()], ctx(both, desk_id=swo.desk_id)).verdict == "BLOCK"       # one underlying is still closed


def test_a_new_calibration_retires_the_old_one_and_a_retired_row_does_not_open_the_switch_ECT_042(swo):
    first = calibrate(swo.desk_id, swo.db)
    second = calibrate(swo.desk_id, swo.db, threshold=Decimal("3"), backtest_run_ref="bt-2")
    assert second != first and switch_state(swo.desk_id, "SPY", db=swo.db).threshold == Decimal("3")
    with swo.db.write() as u:
        u.session.update("premium_thresholds", {"id": second}, {"status": "retired"})
    assert switch_state(swo.desk_id, "SPY", db=swo.db).state == "closed"


def test_a_calibration_needs_clean_evidence_a_run_reference_and_a_real_desk_ECT_042(swo):
    for bad in (dict(evidence=evidence(leakage_free=None)), dict(evidence=evidence(leakage_free=False)), dict(evidence=evidence(n_observations=0)),
                dict(evidence={"leakage_free": True}), dict(backtest_run_ref=""), dict(backtest_run_ref="two words"), dict(backtest_run_ref="x" * 200),
                dict(threshold=2.5), dict(threshold=Decimal("NaN")), dict(metric="Bad Metric")):
        with pytest.raises(ValueError):
            calibrate(swo.desk_id, swo.db, **bad)
    with pytest.raises(ValueError):
        calibrate(99999, swo.db)
    with pytest.raises(ValueError):
        calibrate(swo.desk_id, swo.db, underlying="no spaces")
    with swo.db.read() as u:
        assert u.session.one("SELECT count(*) AS n FROM premium_thresholds")["n"] == 0


CALIBRATION_WRITERS = ("record_calibration", "retire_calibration")


def test_premium_thresholds_is_written_only_by_record_calibration_and_only_the_backtest_may_call_it_ECT_042():
    """The scan: no module outside backend/hokma/backtest/ calls record_calibration or retire_calibration, and nothing but its own module writes the table."""
    callers, writers = [], []
    for path in sorted(HOKMA.rglob("*.py")):
        rel = path.relative_to(HOKMA).as_posix()
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        uses_name = any((isinstance(n, ast.Name) and n.id in CALIBRATION_WRITERS) or (isinstance(n, ast.Attribute) and n.attr in CALIBRATION_WRITERS)
                        or (isinstance(n, ast.alias) and n.name in CALIBRATION_WRITERS) for n in ast.walk(tree))
        if uses_name and rel != "exec/guards/premium_selling.py" and not rel.startswith("backtest/"):
            callers.append(rel)
        if "premium_thresholds" in text and rel not in ("exec/guards/premium_selling.py", "db/transfer.py") and not rel.startswith("db/migrations/"):
            writers.append(rel)
    assert callers == [], f"record_calibration and retire_calibration are for the backtest only: {callers}"
    assert writers == [], f"premium_thresholds is written by premium_selling.py only: {writers}"


def test_no_api_route_and_no_mcp_tool_can_write_the_thresholds_ECT_042():
    for path in sorted(HOKMA.rglob("*.py")):
        rel = path.relative_to(HOKMA).as_posix()
        if rel.endswith("api.py") or rel.startswith("kit/") or rel.startswith("rpc/") or "tools" in rel:
            text = path.read_text(encoding="utf-8")
            assert "record_calibration" not in text and "premium_thresholds" not in text and "premium_selling import" not in text, rel


def test_there_is_no_manual_opening_of_the_switch_ECT_042():
    from hokma.desks.swo import premium_switch as switch

    source = Path(switch.__file__).read_text(encoding="utf-8").lower()
    for word in ("insert", "update", ".write(", "execute(", "record_calibration", "premium_thresholds"):
        assert word not in source, word
    public = [n for n in dir(switch) if not n.startswith("_") and callable(getattr(switch, n)) and getattr(getattr(switch, n), "__module__", "") == switch.__name__]
    assert not [n for n in public if any(k in n for k in ("open_", "enable", "set_", "force", "write", "calibrat"))], public
    guard_source = Path(ps.__file__).read_text(encoding="utf-8")
    assert guard_source.count("INSERT") + guard_source.count('"premium_thresholds", {') == 0 or "record_calibration" in guard_source


# ---- retire_calibration and calibrated_rows (SPEC-020, 4.9 step 7) -------------------------------------------------------------------

def test_retiring_a_calibrated_row_closes_the_switch_and_keeps_the_reason_and_the_run_in_its_evidence_ECT_042(swo):
    row_id = calibrate(swo.desk_id, swo.db)
    assert switch_state(swo.desk_id, "SPY", db=swo.db).is_open
    ps.retire_calibration(row_id, reason="recalibration_failed:dsr_below_min:0.4<0.95", backtest_run_ref="calibration-9", db=swo.db)
    state = switch_state(swo.desk_id, "SPY", db=swo.db)
    assert not state.is_open and state.reason == "no_calibrated_threshold"
    with swo.db.read() as u:
        row = u.session.one("SELECT status, retired_at, evidence_json FROM premium_thresholds WHERE id = :i", {"i": row_id})
    body = json.loads(row["evidence_json"])
    assert row["status"] == "retired" and row["retired_at"] is not None
    assert body["retired"]["reason"] == "recalibration_failed:dsr_below_min:0.4<0.95" and body["retired"]["backtest_run_ref"] == "calibration-9"
    assert body["n_observations"] == 252 and body["dsr"] == 0.9                              # the evidence that opened it is still there
    out = hexec.submit(OrderProposal(swo.desk_id, "SWO", "desk", (leg(C610, "sell"),), "market", created_by="desk-chief", thesis="sell premium"), who=AGENT, db=swo.db)
    assert (out.status, out.code) == ("refused", "premium_selling_closed")


def test_a_row_that_is_retired_or_that_does_not_exist_cannot_be_retired_ECT_042(swo):
    row_id = calibrate(swo.desk_id, swo.db)
    ps.retire_calibration(row_id, reason="first", backtest_run_ref="calibration-1", db=swo.db)
    for bad_id in (row_id, 9999):
        with pytest.raises(ValueError):
            ps.retire_calibration(bad_id, reason="again", backtest_run_ref="calibration-2", db=swo.db)


def test_retire_calibration_refuses_bad_arguments_and_changes_nothing_ECT_042(swo):
    row_id = calibrate(swo.desk_id, swo.db)
    good = dict(reason="why", backtest_run_ref="calibration-1", db=swo.db)
    for bad in ({"threshold_id": 0}, {"threshold_id": True}, {"threshold_id": "1"}, {"reason": ""}, {"reason": "x" * 201}, {"reason": "line\nbreak"}, {"reason": 5},
                {"backtest_run_ref": ""}, {"backtest_run_ref": "has space"}, {"backtest_run_ref": "r" * 129}, {"backtest_run_ref": None}):
        args = {"threshold_id": row_id, **good, **bad}
        with pytest.raises(ValueError):
            ps.retire_calibration(args.pop("threshold_id"), **args)
    assert switch_state(swo.desk_id, "SPY", db=swo.db).is_open


def test_a_recalibration_that_passes_replaces_the_row_and_only_the_latest_one_counts_ECT_042(swo):
    first = calibrate(swo.desk_id, swo.db)
    second = calibrate(swo.desk_id, swo.db, threshold=Decimal("3.5"), backtest_run_ref="bt-2026-11-22-0002")
    rows = ps.calibrated_rows(swo.desk_id, db=swo.db)
    assert [(r.row_id, r.underlying, r.metric, r.threshold, r.backtest_run_ref) for r in rows] == [(second, "SPY", "iv_minus_rv", Decimal("3.5"), "bt-2026-11-22-0002")]
    assert first != second and ps.calibrated_rows(swo.desk_id + 1, db=swo.db) == []
    with pytest.raises(ValueError):
        ps.calibrated_rows(0, db=swo.db)
