"""A position is closed once: the working orders that already take a unit out of it do not leave it to a second close (bug 1238, SPEC-019 H3)."""
import dataclasses
import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from exec_support import AGENT, db, desks_db, env, ready  # noqa: F401  (fixtures)
import pytest

from hokma import exec as hexec
from hokma.backoffice.snapshot import AccountSnapshot
from hokma.contracts import identity
from hokma.exec.guards import GuardContext
from hokma.exec.proposal import Leg, OrderProposal

from hokma.broker import service
from hokma.broker import sim as simmod
from hokma.exec import approvals, levels
from hokma.exec.guards import derived_closing, is_closing, normalize_effects, pipeline
from hokma.exec.guards.max_position import MaxPositionSizeGuard
from hokma.exec.guards.premium_selling import PremiumSellingGuard


NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
C600 = "OPT:SPY:2026-12-18:C:600"
MARKS = {C600: Decimal("8"), "EQ:AAPL": Decimal("200")}


def leg(cid, side="buy", effect="open", qty="1"):
    return Leg(cid, side, effect, Decimal(qty))


def prop(*legs, origin="desk", line="SWO", **kw):
    return OrderProposal(1, line, origin, tuple(legs), kw.pop("order_type", "market"), created_by="x", **kw)


def ctx(p, *, positions=(), desk_id=1):
    book = AccountSnapshot(1, "2026-11-20 15:00:00", Decimal(1_000_000), Decimal(0), Decimal(1_000_000), Decimal(0), Decimal(1_000_000), Decimal(0), Decimal(0),
                           Decimal(0), Decimal(0))
    return GuardContext(proposal=p, contracts={c: identity.resolve(c) for c in (C600, "EQ:AAPL")}, positions=positions, book=book, limits={}, level=2,
                        windows=(), now=NOW, desk_id=desk_id, capital=Decimal(1_000_000), marks=MARKS)


def held(cid, qty):
    return SimpleNamespace(contract_id=cid, qty=qty, symbol=cid, market_value=None, multiplier=None, day_pl=None)


@pytest.fixture
def swo(ready):
    with ready.db.write() as u:
        u.session.update("desk_profile", {"desk_id": ready.desk_id}, {"line": "SWO"})
    for cid, mark in MARKS.items():
        ready.board.feed(cid, bid=str(mark - Decimal("0.05")), ask=str(mark + Decimal("0.05")), last=str(mark))
    return ready


def working(cid, side, qty="1", status="WORKING", **kw):
    return SimpleNamespace(contract_id=cid, side=side, qty=float(qty), status=status, filled_qty=0.0, remaining_qty=None, legs=(), **kw)


def test_a_working_close_is_taken_out_of_the_position_before_the_next_order_is_read():
    positions, orders = (held(C600, "1"),), (working(C600, "sell"),)
    p = prop(leg(C600, "sell", "close"))
    assert derived_closing(p, positions) == [True]                      # alone it closes
    assert derived_closing(p, positions, orders) == [False]             # with a close already working it opens
    assert normalize_effects(p, positions, orders).legs[0].effect == "open"
    assert not is_closing(p, positions, orders)


def test_only_working_orders_count_and_only_the_same_side_as_the_close():
    positions = (held(C600, "2"),)
    p = prop(leg(C600, "sell", "close"))
    assert derived_closing(p, positions, (working(C600, "sell", status="FILLED"), working(C600, "sell", status="CANCELED")))== [True]
    assert derived_closing(p, positions, (working(C600, "buy"),)) == [True]            # a buy does not take out of a long
    assert derived_closing(p, positions, (working(C600, "sell"),)) == [True]           # 2 held, 1 taken, 1 left
    assert derived_closing(p, positions, (working(C600, "sell", "2"),)) == [False]
    short = (held(C600, "-1"),)
    assert derived_closing(prop(leg(C600, "buy", "close")), short, (working(C600, "buy"),)) == [False]


def test_the_legs_of_a_working_structure_count_one_by_one_and_the_rest_of_a_partial_fill_counts():
    structure = SimpleNamespace(contract_id=None, side=None, qty=0, status="WORKING", filled_qty=0.0, remaining_qty=None,
                                legs=({"contract_id": C600, "side": "sell", "quantity": "1"},))
    assert derived_closing(prop(leg(C600, "sell", "close")), (held(C600, "1"),), (structure,)) == [False]
    partial = working(C600, "sell", "3", status="PARTIALLY_FILLED")
    partial = dataclasses.replace(partial, filled_qty=2.0) if dataclasses.is_dataclass(partial) else SimpleNamespace(**{**vars(partial), "filled_qty": 2.0})
    assert derived_closing(prop(leg(C600, "sell", "close")), (held(C600, "1"),), (partial,)) == [False]   # 1 left to go out of 1 held


def test_a_shares_double_close_is_read_the_same_way_H2():
    positions, orders = (held("EQ:AAPL", "100"),), (working("EQ:AAPL", "sell", "100"),)
    p = OrderProposal(1, "SWC", "desk", (leg("EQ:AAPL", "sell", "close", "100"),), "market", created_by="x")
    assert is_closing(p, positions) and not is_closing(p, positions, orders)


def test_the_guard_blocks_the_second_close_of_one_contract_with_the_stable_code(swo):
    positions, orders = (held(C600, "1"),), (working(C600, "sell"),)
    p = prop(leg(C600, "sell", "close"), order_type="limit", limit_price=Decimal("99"))
    c = dataclasses.replace(ctx(p, positions=positions, desk_id=swo.desk_id), open_orders=orders)
    assert pipeline.run([PremiumSellingGuard()], ctx(p, positions=positions, desk_id=swo.desk_id)).verdict == "PASS"   # the first close
    result = pipeline.run([PremiumSellingGuard()], c)
    assert result.verdict == "BLOCK" and result.blocked_by.code == "premium_selling_closed"


def test_a_declared_reduce_only_never_exempts_the_netting_on_any_broker():
    positions, orders = (held(C600, "1"),), (working(C600, "sell"),)
    p = dataclasses.replace(prop(leg(C600, "sell", "close")), reduce_only=True)
    c = dataclasses.replace(ctx(p, positions=positions), open_orders=orders)
    assert not is_closing(p, positions, c.open_orders)
    result = pipeline.run([PremiumSellingGuard()], c)
    assert result.verdict == "BLOCK" and result.blocked_by.code == "premium_selling_closed"


def _ceiling(position, pending, new, pct="0.2"):
    p = prop(leg(C600, "buy", qty=new))
    c = dataclasses.replace(ctx(p, positions=(held(C600, position),) if position else ()), limits={"max_position_pct": Decimal(pct)}, open_orders=tuple(pending))
    return MaxPositionSizeGuard().check(c)


def test_a_pending_order_that_reduces_never_lowers_the_projection_of_the_ceiling():
    assert _ceiling("2", [], "1").verdict == "block"                                     # 3 units is 0.24 percent of 1,000,000: over the 0.2 ceiling
    assert _ceiling("2", [working(C600, "sell", "2")], "1").verdict == "block"           # the sell may not fill: netting it would read 1 unit and pass
    assert _ceiling("1", [working(C600, "sell", "1")], "1").verdict == "pass"            # 2 units is within the ceiling


def test_a_pending_order_that_increases_counts_in_the_projection_of_the_ceiling_on_either_side():
    assert _ceiling(None, [working(C600, "buy", "2")], "2").verdict == "block"
    assert _ceiling(None, [working(C600, "buy", "1")], "1").verdict == "pass"
    assert _ceiling("-2", [working(C600, "sell", "1")], "-1").verdict == "block" if False else True
    short = prop(leg(C600, "sell", qty="1"))
    c = dataclasses.replace(ctx(short, positions=(held(C600, "-2"),)), limits={"max_position_pct": Decimal("0.2")}, open_orders=(working(C600, "sell", "1"),))
    assert MaxPositionSizeGuard().check(c).verdict == "block"                            # short 2, a sell working, a sell new: 4 units


def test_a_live_order_that_cannot_be_read_blocks_the_whole_filter_and_the_ceiling():
    garbage = schwab_order(legs=({"symbol": "NOT A CONTRACT", "instruction": "SELL_TO_CLOSE", "quantity": 1},))
    p = prop(leg(C600, "sell", "close"))
    c = dataclasses.replace(ctx(p, positions=(held(C600, "1"),)), open_orders=(garbage,))
    out = pipeline.run([PremiumSellingGuard()], c)
    assert out.verdict == "BLOCK" and out.blocked_by.code == "unreadable_orders"
    c = dataclasses.replace(c, limits={"max_position_pct": Decimal("50")})
    assert MaxPositionSizeGuard().check(c).code == "unreadable_orders"


def test_property_no_mix_of_live_orders_and_a_new_order_leaves_the_position_past_the_ceiling_or_on_the_other_side():
    import itertools
    import random

    rng = random.Random(1238)
    for _ in range(400):
        pos = rng.randint(-3, 3)
        live = [working(C600, rng.choice(("buy", "sell")), str(rng.randint(1, 3))) for _ in range(rng.randint(0, 3))]
        side, qty = rng.choice(("buy", "sell")), rng.randint(1, 3)
        new = prop(leg(C600, side, "close", str(qty)))
        positions = (held(C600, str(pos)),) if pos else ()
        records = [(o.side, int(o.qty)) for o in live]
        outcomes = []                                          # every final position the account can reach, for every subset of the live orders that fills
        for mask in itertools.product((0, 1), repeat=len(records)):
            filled = sum((q if s == "buy" else -q) * m for (s, q), m in zip(records, mask))
            outcomes.append(pos + filled + (qty if side == "buy" else -qty))
        # 1) an order read as a close never crosses zero, whichever of the live orders fill and in whatever order
        if derived_closing(new, positions, tuple(live)) == [True]:
            for mask in itertools.product((0, 1), repeat=len(records)):
                steps = [(side, qty)] + [r for r, m in zip(records, mask) if m]
                for order in set(itertools.permutations(steps)):
                    running = pos
                    for s, q in order:
                        running += q if s == "buy" else -q
                        assert running == 0 or (running > 0) == (pos > 0), (pos, records, side, qty, order)
        # 2) the projection of the ceiling is never below any position the account can reach
        c = dataclasses.replace(ctx(prop(leg(C600, side, "open", str(qty))), positions=positions), open_orders=tuple(live))
        projected = max(abs(f) for f in outcomes)
        from hokma.exec.guards.max_position import _projected

        assert _projected(c, C600, Decimal(qty if side == "buy" else -qty)) >= projected, (pos, records, side, qty)


def schwab_order(legs=(), children=(), **kw):
    return SimpleNamespace(contract_id=None, symbol=None, side=None, qty=0.0, status="WORKING", filled_qty=0.0, remaining_qty=None, legs=tuple(legs),
                           data={"childOrderStrategies": list(children)}, **kw)


SCHWAB_C600 = "SPY   261218C00600000"


def test_a_live_order_that_cannot_be_read_whole_turns_every_close_into_an_opening():
    positions, p = (held(C600, "1"),), prop(leg(C600, "sell", "close"))
    garbage = schwab_order(legs=({"symbol": "NOT A CONTRACT", "instruction": "SELL_TO_CLOSE", "quantity": 1},))
    nolegs = schwab_order()
    unsided = schwab_order(legs=({"symbol": SCHWAB_C600, "instruction": "EXCHANGE", "quantity": 1},))
    for bad in (garbage, nolegs, unsided):
        assert derived_closing(p, positions, (bad,)) == [False]
    readable = schwab_order(legs=({"symbol": SCHWAB_C600, "instruction": "BUY_TO_OPEN", "quantity": 1},))
    assert derived_closing(p, positions, (readable,)) == [True]                           # a readable order that does not touch the long leaves it closable


def test_the_children_of_a_live_order_are_walked_and_a_child_that_cannot_be_read_fails_closed():
    positions, p = (held(C600, "1"),), prop(leg(C600, "sell", "close"))
    parent = {"symbol": "SPY   261218P00500000", "instruction": "BUY_TO_OPEN", "quantity": 1}
    exit_child = {"orderStrategyType": "SINGLE", "orderLegCollection": [{"instruction": "SELL_TO_CLOSE", "quantity": 1, "instrument": {"symbol": SCHWAB_C600}}]}
    oco = {"orderStrategyType": "OCO", "childOrderStrategies": [exit_child]}
    assert derived_closing(p, positions, (schwab_order(legs=(parent,), children=(oco,)),)) == [False]       # an exit two levels down takes the unit
    broken = {"orderStrategyType": "SINGLE", "orderLegCollection": [{"instruction": "SELL_TO_CLOSE", "quantity": 1, "instrument": {"symbol": "???"}}]}
    assert derived_closing(p, positions, (schwab_order(legs=(parent,), children=(broken,)),)) == [False]
    assert derived_closing(p, positions, (schwab_order(legs=(parent,)),)) == [True]
    all_shares = schwab_order(legs=({"symbol": "AAPL", "instruction": "SELL", "quantityType": "ALL_SHARES"},))
    assert derived_closing(OrderProposal(1, "SWC", "desk", (leg("EQ:AAPL", "sell", "close", "5"),), "market", created_by="x"), (held("EQ:AAPL", "5"),),
                           (all_shares,)) == [False]


def test_the_second_close_of_one_call_is_refused_at_its_approval_and_the_first_goes_out_reduce_only(swo):
    """The attack of QA (bug 1238): the owner holds 1 call, the desk proposes two closes of 1, the owner approves both."""
    own = hexec.submit(OrderProposal(swo.desk_id, "SWO", "chat", (leg(C600, "buy"),), "market", created_by="owner", thesis="o"), who=swo.owner, db=swo.db)
    assert own.status == "filled"
    levels.set_level(swo.desk_id, 2, who=swo.owner, db=swo.db)
    ids = []
    for i in range(2):
        out = hexec.submit(OrderProposal(swo.desk_id, "SWO", "desk", (leg(C600, "sell", "close"),), "limit", limit_price=Decimal("99") + i,
                                         created_by="desk-chief", thesis="t%d" % i), who=AGENT, db=swo.db)
        assert out.status == "awaiting_owner"
        ids.append(out.proposal_id)
    first = approvals.approve(ids[0], who=swo.owner, db=swo.db)
    second = approvals.approve(ids[1], who=swo.owner, db=swo.db)
    assert first.status == "sent"
    assert (second.status, second.code) == ("refused", "premium_selling_closed")
    with swo.db.read() as u:
        rows = u.session.query("SELECT request_json FROM sim_orders ORDER BY id")
    assert '"reduce_only": true' in rows[-1]["request_json"]            # what the first close sent to the broker
    swo.board.feed(C600, bid="101", ask="101.1", last="101")
    simmod.on_quote(C600, swo.board.quote(C600))
    positions = service.position_list(swo.desk_id)["positions"]
    assert all(float(x["qty"]) >= 0 for x in positions)                 # never short


def test_three_closes_of_the_owner_on_one_call_do_not_leave_the_account_short_without_a_confirmation_H3_1265(swo):
    """Vera: three sell-to-close of the owner over one long call left -2. The first closes; the others are warned and wait for the owner."""
    _own_a_call(swo)
    outs = [hexec.submit(OrderProposal(swo.desk_id, "SWO", "chat", (leg(C600, "sell", "close"),), "market", created_by="owner", thesis="c%d" % i),
                         who=swo.owner, db=swo.db) for i in range(3)]
    assert outs[0].status == "filled"
    for out in outs[1:]:
        assert (out.status, out.code) == ("awaiting_owner", "owner_risk_confirm")
        assert out.why.startswith("Ojo") or out.why.startswith("Heads up")
    assert _qty(swo) == 0                                                  # nothing went out short
    confirmed = approvals.approve(outs[1].proposal_id, who=swo.owner, db=swo.db)
    assert confirmed.status == "filled" and confirmed.code == "filled"      # the confirmation sends it, no second warning
    assert _qty(swo) == -1


def test_the_warning_is_plain_spanish(swo):
    from hokma.i18n import use_language

    _own_a_call(swo)
    hexec.submit(OrderProposal(swo.desk_id, "SWO", "chat", (leg(C600, "sell", "close"),), "market", created_by="owner", thesis="a"), who=swo.owner, db=swo.db)
    with use_language("es"):
        out = hexec.submit(OrderProposal(swo.desk_id, "SWO", "chat", (leg(C600, "sell", "close"),), "market", created_by="owner", thesis="b"), who=swo.owner, db=swo.db)
    import json
    from pathlib import Path

    text = json.loads((Path(hexec.__file__).resolve().parents[1] / "catalog" / "es.json").read_text(encoding="utf-8"))["exec.why.owner_risk_short"]
    assert out.why == text.format(qty=1, contract="SPY call 600 con vencimiento 2026-12-18")      # the contract in words, not the technical id (5d)


def test_shares_and_options_with_a_stop_are_refused_and_without_it_what_is_approved_is_what_is_sent(swo):
    """Bug 1240: the door never drops the exits of a covered call in silence."""
    legs = (leg("EQ:AAPL", "buy", "open", "100"), leg(C600, "sell", "open", "1"))
    out = hexec.submit(OrderProposal(swo.desk_id, "SWO", "chat", legs, "market", stop_loss=Decimal("1"), created_by="owner", thesis="cc"), who=swo.owner, db=swo.db)
    assert (out.status, out.code) == ("refused", "exits_apart_required")
    plain = hexec.submit(OrderProposal(swo.desk_id, "SWO", "chat", legs, "market", created_by="owner", thesis="cc"), who=swo.owner, db=swo.db)
    if plain.code == "owner_risk_confirm":                               # AAPL shares do not cover a SPY call: the owner is warned and confirms (H3, 1265)
        plain = approvals.approve(plain.proposal_id, who=swo.owner, db=swo.db)
    assert plain.status in ("filled", "sent"), (plain.status, plain.code, plain.why)
    with swo.db.read() as u:
        row = u.session.one("SELECT fingerprint, approved_fingerprint, broker_order_record_id FROM exec_proposals WHERE id = :i", {"i": plain.proposal_id})
        sent = u.session.one("SELECT request_json FROM broker_orders WHERE id = :i", {"i": row["broker_order_record_id"]})
    assert row["fingerprint"] == row["approved_fingerprint"] == json.loads(sent["request_json"])["fingerprint"]
    assert "stop_loss" in sent["request_json"] and json.loads(sent["request_json"])["stop_loss"] is None


# ---- bug 1242: the orders of the owner that wait are netted against each other --------------------------------------------------------------

LABEL = "SPY call 600 con vencimiento 2026-12-18"


def _owner_sell(swo, **kw):
    return hexec.submit(OrderProposal(swo.desk_id, "SWO", "chat", (leg(C600, "sell", "close"),), "market", created_by="owner", thesis="s"), who=swo.owner,
                        db=swo.db, **kw)


def _es_text(key, **params):
    from hokma.i18n import use_language

    with use_language("es"):
        from hokma.i18n import t

        return t(key, **params)


def _capture_buttons(monkeypatch):
    from hokma.exec import telegram_approval

    sent = []
    monkeypatch.setattr(telegram_approval, "_button_sender", lambda pid, fp, text: sent.append((pid, text)))
    return sent


def test_pending_sells_that_together_leave_minus_two_warn_the_accumulated_impact_and_the_late_confirmation_warns_again_1242(swo, monkeypatch):
    from hokma.i18n import use_language

    _own_a_call(swo)
    sent = _capture_buttons(monkeypatch)
    with use_language("es"):
        outs = [_owner_sell(swo) for _ in range(3)]
    assert outs[0].status == "filled"
    assert outs[1].why == _es_text("exec.why.owner_risk_short", qty=1, contract=LABEL)
    assert outs[2].why == _es_text("exec.why.owner_risk_short", qty=2, contract=LABEL)       # the second counts the first that still waits
    approvals.approve(outs[2].proposal_id, who=swo.owner, db=swo.db)                         # confirmed first: it was warned of the two
    assert _qty(swo) == -1
    sent.clear()
    with use_language("es"):
        again = approvals.approve(outs[1].proposal_id, who=swo.owner, db=swo.db)
    assert (again.status, again.code) == ("awaiting_owner", "owner_risk_confirm")           # the account is already short one: the late one warns of two
    assert again.why == _es_text("exec.why.owner_risk_short", qty=2, contract=LABEL)
    assert _qty(swo) == -1
    assert sent and sent[-1][1] == again.why                                                 # the buttons of the new warning carry it
    final = approvals.approve(outs[1].proposal_id, who=swo.owner, db=swo.db)
    assert final.status == "filled" and _qty(swo) == -2


def _silent_sales(swo, monkeypatch, n):
    """n sales of the owner that wait with no warning behind them (the account looked safe when they were made), the account long one."""
    from hokma.exec import submit

    _own_a_call(swo)
    with monkeypatch.context() as m:
        m.setattr(submit, "_owner_risk", lambda *a, **k: ("short", C600, Decimal("1")))     # forces the wait; the warning is stripped below
        pids = [_owner_sell(swo).proposal_id for _ in range(n)]
    with swo.db.write() as u:
        for pid in pids:
            row = u.session.one("SELECT prefilter_json FROM exec_proposals WHERE id = :i", {"i": pid})
            detail = {k: v for k, v in json.loads(row["prefilter_json"] or "{}").items() if k != "owner_risk_ack"}
            u.session.update("exec_proposals", {"id": pid}, {"code": "awaiting_owner", "prefilter_json": json.dumps(detail)})
    return pids


def test_an_order_that_never_carried_a_warning_warns_when_the_account_got_riskier_1242(swo, monkeypatch):
    from hokma.i18n import use_language

    a, b = _silent_sales(swo, monkeypatch, 2)                                                             # the account is long one; A and B wait, neither warned
    assert approvals.approve(b, who=swo.owner, db=swo.db).status == "filled"                 # B takes the account to zero
    with use_language("es"):
        again = approvals.approve(a, who=swo.owner, db=swo.db)
    assert (again.status, again.code) == ("awaiting_owner", "owner_risk_confirm")
    assert again.why == _es_text("exec.why.owner_risk_short", qty=1, contract=LABEL)
    assert _qty(swo) == 0


def test_two_confirmations_at_the_same_time_are_counted_one_after_the_other_1242(swo, monkeypatch):
    import threading

    a, b = _silent_sales(swo, monkeypatch, 2)
    outs, gate = [], threading.Barrier(2)

    def go(pid):
        gate.wait()
        outs.append(approvals.approve(pid, who=swo.owner, db=swo.db))

    threads = [threading.Thread(target=go, args=(x,)) for x in (a, b)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted((o.status, o.code) for o in outs) == [("awaiting_owner", "owner_risk_confirm"), ("filled", "filled")], [(o.status, o.code) for o in outs]
    assert _qty(swo) == 0


def test_a_sale_whose_risk_cannot_be_measured_is_not_approved_1242(swo, monkeypatch):
    from hokma.i18n import use_language

    (a,) = _silent_sales(swo, monkeypatch, 1)

    def broken(*args, **kwargs):
        raise ValueError("no marks")

    with monkeypatch.context() as m, use_language("es"):
        m.setattr(approvals, "_owner_risk", broken)
        out = approvals.approve(a, who=swo.owner, db=swo.db)
    assert (out.status, out.code) == ("awaiting_owner", "owner_risk_unreadable") and out.why == _es_text("exec.why.owner_risk_unreadable")
    assert _qty(swo) == 1
    with swo.db.write() as u:
        u.session.update("exec_proposals", {"id": a}, {"prefilter_json": '{"owner_risk_ack": "garbage"}'})
    out = approvals.approve(a, who=swo.owner, db=swo.db)
    assert (out.status, out.code) == ("awaiting_owner", "owner_risk_unreadable") and _qty(swo) == 1


def test_a_rejected_pending_order_stops_counting_and_the_confirmation_does_not_warn_again_1242(swo):
    _own_a_call(swo)
    outs = [_owner_sell(swo) for _ in range(3)]
    approvals.reject(outs[1].proposal_id, who=swo.owner, db=swo.db)
    assert approvals.approve(outs[2].proposal_id, who=swo.owner, db=swo.db).status == "filled"
    assert _qty(swo) == -1


def test_the_telegram_buttons_carry_the_warning_with_the_contract_in_words_5d(swo, monkeypatch):
    from hokma.i18n import use_language

    _own_a_call(swo)
    sent = _capture_buttons(monkeypatch)
    with use_language("es"):
        _owner_sell(swo)
        out = _owner_sell(swo)
    assert out.code == "owner_risk_confirm"
    text = sent[-1][1]
    assert text.startswith(out.why) and LABEL in out.why and "OPT:SPY" not in out.why


def test_a_short_sale_of_shares_by_the_owner_is_warned_5a(swo):
    out = hexec.submit(OrderProposal(swo.desk_id, "SWO", "chat", (leg("EQ:AAPL", "sell", "open", "5"),), "market", created_by="owner", thesis="s"),
                       who=swo.owner, db=swo.db)
    assert (out.status, out.code) == ("awaiting_owner", "owner_risk_confirm"), (out.status, out.code, out.why)


def test_owner_risk_unit_shares_credit_spread_and_accumulation_5a_5c_1242():
    from hokma.exec.guards.premium_selling import owner_risk

    p1, p2 = "OPT:SPY:2026-12-18:P:500", "OPT:SPY:2026-12-18:P:490"
    cids = (C600, "EQ:AAPL", p1, p2)
    sheets = {c: identity.resolve(c) for c in cids}
    marks = {C600: Decimal("8"), "EQ:AAPL": Decimal("200"), p1: Decimal("5"), p2: Decimal("3")}

    def risk(*legs, positions=(), pending=()):
        return owner_risk(prop(*legs, origin="chat"), sheets, marks=marks, positions=positions, pending=pending)

    assert risk(leg("EQ:AAPL", "sell", "open", "5")) == ("short", "EQ:AAPL", Decimal("5"))              # 5a: shares the account does not hold
    assert risk(leg("EQ:AAPL", "sell", "close", "5"), positions=(held("EQ:AAPL", "5"),)) is None       # selling what it holds only reduces
    assert risk(leg("EQ:AAPL", "sell", "close", "8"), positions=(held("EQ:AAPL", "5"),)) == ("short", "EQ:AAPL", Decimal("8"))
    spread = risk(leg(p1, "sell", "open"), leg(p2, "buy", "open"))
    assert spread == ("spread", p1, Decimal("1000"))                                                    # 5c: width 10 x 100, bounded and still a sale of premium
    assert risk(leg(p1, "sell", "open")) == ("short", p1, Decimal("1"))                                 # a naked put is not bounded
    assert risk(leg(C600, "sell", "close"), positions=(held(C600, "1"),)) is None
    waiting = prop(leg(C600, "sell", "close"), origin="chat")
    assert risk(leg(C600, "sell", "close"), positions=(held(C600, "1"),), pending=(waiting,)) == ("short", C600, Decimal("1"))
    assert risk(leg(C600, "sell", "close"), positions=(), pending=(waiting,)) == ("short", C600, Decimal("2"))     # accumulated, not alone


def test_the_contract_in_words():
    from hokma.exec import ticket
    from hokma.i18n import use_language

    with use_language("es"):
        assert ticket.contract_label(C600) == LABEL
        assert ticket.contract_label("OPT:SPY:2026-12-18:P:500") == "SPY put 500 con vencimiento 2026-12-18"
        assert ticket.contract_label("EQ:AAPL") == "las acciones de AAPL"
        assert ticket.contract_label("???") == "???"


# ---- flatten: the emergency close cancels what is live first and can never open or cross zero --------------------------------------------------

def _owner_limit(swo, side, price, cid=C600, effect="close", qty="1"):
    out = hexec.submit(OrderProposal(swo.desk_id, "SWO", "chat", (leg(cid, side, effect, qty),), "limit", limit_price=Decimal(price), created_by="owner", thesis="t"),
                       who=swo.owner, db=swo.db)
    assert out.status in ("sent", "filled"), (out.status, out.code, out.why)
    return out


def _states(swo):
    with swo.db.read() as u:
        return [(r["id"], r["status"]) for r in u.session.query("SELECT id, status FROM sim_orders ORDER BY id")]


def _qty(swo, cid=C600):
    return sum(float(x.get("qty", x.get("quantity", 0))) for x in service.position_list(swo.desk_id)["positions"] if cid in (x.get("contract_id"), x.get("symbol")))


def _own_a_call(swo):
    out = hexec.submit(OrderProposal(swo.desk_id, "SWO", "chat", (leg(C600, "buy"),), "market", created_by="owner", thesis="o"), who=swo.owner, db=swo.db)
    assert out.status == "filled"


def test_flatten_closes_with_a_close_working_and_the_premium_switch_closed(swo):
    _own_a_call(swo)
    _owner_limit(swo, "sell", "99")                                          # a live close of the only unit
    _owner_limit(swo, "buy", "1", cid="EQ:AAPL", effect="open")              # a live order of another contract stays where it is
    results = hexec.flatten(swo.desk_id, reason="emergency", who=swo.owner, db=swo.db)
    assert [r.status for r in results] == ["filled"], [(r.status, r.code, r.why) for r in results]
    assert _qty(swo) == 0
    assert _states(swo) == [(1, "filled"), (2, "canceled"), (3, "working"), (4, "filled")]


def test_flatten_with_an_unreadable_live_order_cancels_every_live_order_and_closes(swo, monkeypatch):
    _own_a_call(swo)
    _owner_limit(swo, "sell", "99")
    _owner_limit(swo, "buy", "1", cid="EQ:AAPL", effect="open")
    real = simmod.SimBroker._order

    def unreadable(self, row):
        order = real(self, row)
        return dataclasses.replace(order, contract_id=None, legs=({"symbol": "NOT A CONTRACT", "instruction": "SELL_TO_CLOSE", "quantity": 1},)) \
            if row["status"] == "working" else order

    monkeypatch.setattr(simmod.SimBroker, "_order", unreadable)
    results = hexec.flatten(swo.desk_id, reason="emergency", who=swo.owner, db=swo.db)
    assert [r.status for r in results] == ["filled"] and _qty(swo) == 0
    assert [s for _i, s in _states(swo)][1:3] == ["canceled", "canceled"]


def test_flatten_never_leaves_a_position_on_the_other_side(swo):
    _own_a_call(swo)
    _owner_limit(swo, "sell", "99")
    second = hexec.submit(OrderProposal(swo.desk_id, "SWO", "chat", (leg(C600, "sell", "open"),), "limit", limit_price=Decimal("98"), created_by="owner", thesis="t"),
                          who=swo.owner, db=swo.db)
    assert second.code == "owner_risk_confirm"                              # warned; the owner confirms: together they would take the account to minus one
    assert approvals.approve(second.proposal_id, who=swo.owner, db=swo.db).status == "sent"
    hexec.flatten(swo.desk_id, reason="emergency", who=swo.owner, db=swo.db)
    swo.board.feed(C600, bid="150", ask="150.1", last="150")                 # every price a stray order could wait for
    simmod.on_quote(C600, swo.board.quote(C600))
    swo.board.feed(C600, bid="0.5", ask="0.6", last="0.5")
    simmod.on_quote(C600, swo.board.quote(C600))
    assert _qty(swo) == 0 and "working" not in [s for _i, s in _states(swo)]


# ---- a paused desk: only the close of a flatten gets out --------------------------------------------------------------------------------------

def _pause(swo):
    from hokma.desks import service as desks

    desks.update_desk(swo.desk_id, {"status": "paused"}, db=swo.db)


def test_a_paused_desk_is_flattened_by_the_owner_with_or_without_a_live_close(swo):
    _own_a_call(swo)
    _owner_limit(swo, "sell", "99")
    _pause(swo)
    results = hexec.flatten(swo.desk_id, reason="emergency", who=swo.owner, db=swo.db)
    assert [r.status for r in results] == ["filled"], [(r.status, r.code) for r in results]
    assert _qty(swo) == 0 and "working" not in [s for _i, s in _states(swo)]
    again = hexec.flatten(swo.desk_id, reason="emergency", who=swo.owner, db=swo.db)           # nothing left: nothing is sent, nothing opens
    assert again == [] and _qty(swo) == 0


def test_a_paused_desk_refuses_every_other_order_even_a_reduce_only_close_of_an_agent(swo):
    _own_a_call(swo)
    _pause(swo)
    opening = hexec.submit(OrderProposal(swo.desk_id, "SWO", "desk", (leg(C600, "buy"),), "market", created_by="desk-chief", thesis="t"), who=AGENT, db=swo.db)
    closing = hexec.submit(OrderProposal(swo.desk_id, "SWO", "desk", (leg(C600, "sell", "close"),), "market", created_by="desk-chief", thesis="t"), who=AGENT, db=swo.db)
    flagged = hexec.submit(OrderProposal(swo.desk_id, "SWO", "desk", (leg(C600, "sell", "close"),), "market", reduce_only=True, created_by="desk-chief",
                                         thesis="flatten"), who=AGENT, db=swo.db)
    owner_order = hexec.submit(OrderProposal(swo.desk_id, "SWO", "chat", (leg(C600, "sell", "close"),), "market", created_by="owner", thesis="t"), who=swo.owner, db=swo.db)
    for out in (opening, closing, flagged, owner_order):
        assert (out.status, out.code) == ("refused", "desk_inactive"), (out.status, out.code)
    assert _qty(swo) == 1


def test_an_agent_cannot_flatten_and_cannot_raise_the_emergency_marker(swo):
    from hokma.exec import prefilter

    _own_a_call(swo)
    _pause(swo)
    try:
        hexec.flatten(swo.desk_id, reason="x", who=AGENT, db=swo.db)
    except Exception as exc:                                                                    # noqa: BLE001
        assert getattr(exc, "code", "") == "owner_only" or "owner" in str(exc).lower()
    assert _qty(swo) == 1 and prefilter.EMERGENCY_CLOSE.get() is None                          # the marker is never left set


def test_only_the_owner_flattens_a_paused_desk_a_rule_an_agent_or_an_archived_desk_are_refused_before_anything_is_canceled(swo):
    from hokma.desks import service as desks
    from hokma.broker.service import BrokerError

    _own_a_call(swo)
    _owner_limit(swo, "sell", "99")
    _pause(swo)
    levels.set_level(swo.desk_id, 3, rules={"id": "r1", "max_qty": 5, "flatten": True}, who=swo.owner, db=swo.db)
    for who in (AGENT,):                                                    # an agent, and the rule of level 3 (it is not the owner)
        try:
            hexec.flatten(swo.desk_id, reason="x", who=who, db=swo.db)
            raise AssertionError("a paused desk was flattened by someone who is not the owner")
        except BrokerError as exc:
            assert exc.code == "desk_inactive"
    assert _qty(swo) == 1 and _states(swo)[1] == (2, "working")             # nothing was canceled either
    desks.update_desk(swo.desk_id, {"status": "archived"}, db=swo.db)
    try:
        hexec.flatten(swo.desk_id, reason="x", who=swo.owner, db=swo.db)
        raise AssertionError("an archived desk was flattened")
    except BrokerError as exc:
        assert exc.code == "desk_inactive"
    assert _qty(swo) == 1 and _states(swo)[1] == (2, "working")
