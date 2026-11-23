"""Alerts, TCA, monitoring, the emergency switch and the Telegram ticket (SPEC-018 B6: DTS-09, ECT-018, ECT-026, OA-52, OA-53)."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from exec_support import AGENT, NQ, db, desks_db, env, proposal, ready  # noqa: F401

from hokma import exec as hexec
from hokma.broker import service
from hokma.exec import alerts, killswitch, levels, monitor, tca, telegram_approval

D = Decimal


def test_alerts_ring_once_per_crossing_survive_a_restart_and_rearm_when_the_condition_ends(ready):
    a = alerts.add_alert(ready.desk_id, {"type": "price_cross", "contract_id": NQ, "direction": "above", "level": "21100"}, db=ready.db)
    now = datetime(2026, 11, 20, 15, tzinfo=timezone.utc)
    assert alerts.evaluate(ready.desk_id, now=now, marks={NQ: D(21000)}, db=ready.db) == []
    first = alerts.evaluate(ready.desk_id, now=now, marks={NQ: D(21150)}, db=ready.db)
    assert [f.alert_id for f in first] == [a]
    assert alerts.evaluate(ready.desk_id, now=now, marks={NQ: D(21200)}, db=ready.db) == []          # rings once
    assert alerts.list_alerts(ready.desk_id, db=ready.db)[0]["armed"] is False                         # state is in the table: a restart reads it
    assert alerts.evaluate(ready.desk_id, now=now, marks={NQ: D(21050)}, db=ready.db) == []           # condition over: arms again
    assert len(alerts.evaluate(ready.desk_id, now=now, marks={NQ: D(21300)}, db=ready.db)) == 1


def test_percent_move_uses_the_reference_of_the_session_and_drawdown_and_unfilled_orders_ring(ready):
    alerts.add_alert(ready.desk_id, {"type": "pct_change", "contract_id": NQ, "pct": "1", "kind": "future"}, db=ready.db)
    alerts.add_alert(ready.desk_id, {"type": "desk_drawdown", "pct": "5"}, db=ready.db)
    alerts.add_alert(ready.desk_id, {"type": "unfilled_order", "minutes": 10}, db=ready.db)
    t0 = datetime(2026, 11, 17, 15, tzinfo=timezone.utc)                           # a Tuesday
    assert alerts.evaluate(ready.desk_id, now=t0, marks={NQ: D(21000)}, equity=D(100), peak=D(100), db=ready.db) == []   # takes the reference
    fired = alerts.evaluate(ready.desk_id, now=t0 + timedelta(hours=1), marks={NQ: D(21300)}, equity=D(90), peak=D(100),
                            working=[{"age_minutes": 12}], db=ready.db)
    assert {f.rule["type"] for f in fired} == {"pct_change", "desk_drawdown", "unfilled_order"}
    with pytest.raises(alerts.AlertError):
        alerts.add_alert(ready.desk_id, {"type": "nope"}, db=ready.db)


def test_shortfall_splits_into_five_parts_that_add_up_exactly_and_the_spread_is_not_counted_twice():
    s = tca.implementation_shortfall(side="buy", ordered_qty=D(10), fills=[(D(4), D("21002")), (D(4), D("21004"))], decision_price=D("21000"),
                                     arrival_price=D("21001"), close_price=D("21010"), spread=D("0.5"), multiplier=D(20), commission=D(10))
    # filled 8 at 21003 on average; unfilled 2
    assert s.delay == D(1) * 8 * 20 and s.spread == D("0.25") * 8 * 20
    assert s.impact == D(2) * 8 * 20 - s.spread and s.opportunity == D(10) * 2 * 20 and s.commission == 10
    paper = D(10) * 20 * D(10)                                   # all 10 bought at the decision price, worth 21010 at the close
    actual = D(8) * 20 * (D("21010") - D("21003")) - 10
    assert s.total == paper - actual
    b = tca.against_benchmarks(side="buy", avg_fill=D("21003"), arrival=D("21001"), vwap=D("21002"), close=D("21010"))
    assert round(b["arrival"], 4) == round(D(2) / D("21001") * 10000, 4) and b["close"] < 0
    assert tca.commission_per_contract(D(3), D("2.5")) == D("7.5")
    assert tca.sqrt_impact_bps(daily_volatility_bps=D(100), qty=D(25), daily_volume=D(100)) == D(50)
    table = tca.broker_table([{"broker": "a", "slippage_bps": 2, "commission": 1}, {"broker": "b", "slippage_bps": 5, "commission": 2}, {"broker": "a", "slippage_bps": 4, "commission": 1}])
    assert [t["broker"] for t in table] == ["b", "a"] and table[1]["avg_slippage_bps"] == 3
    real = tca.slippage_vs_backtest([{"symbol": "NQ", "side": "buy", "price": "21002", "next_open": "21000", "tick": "0.25"}])
    assert real["NQ"]["avg_extra_ticks"] == D(7)                 # 2 points is 8 ticks, the backtest charged 1
    assert tca.compare_with_preview(side="buy", sim_price=D("100.10"), preview_price=D("100"))["difference"] == D("0.10")


def test_cusum_has_the_right_sign_the_sharpe_warns_and_the_levels_come_from_facts():
    worse, better = [-0.02] * 10, [0.04] * 10
    assert monitor.cusum_deterioration(worse, expected=0.01, slack=0.005, threshold=0.1)[0] is True
    assert monitor.cusum_deterioration(better, expected=0.01, slack=0.005, threshold=0.1)[0] is False        # doing better never rings
    assert monitor.sharpe_warning([-0.01, 0.0, -0.02, 0.005] * 15, expected_sharpe=1.0, window=30) is True
    assert monitor.sharpe_warning([0.02, 0.01, 0.03, 0.015] * 15, expected_sharpe=1.0, window=30) is False
    assert monitor.ks_statistic([1, 2, 3], [1, 2, 3]) == 0 and monitor.ks_statistic([1, 2, 3], [7, 8, 9]) == 1
    th = {"warn_drawdown_pct": 3, "reduce_drawdown_pct": 4, "halt_drawdown_pct": 5, "halt_day_loss_usd": 1000}
    assert monitor.kill_level(drawdown_pct=None, day_loss_usd=None, thresholds=th).name == "ok"
    assert monitor.kill_level(drawdown_pct=4.5, day_loss_usd=0, thresholds=th).name == "reduce"
    assert monitor.kill_level(drawdown_pct=1, day_loss_usd=1500, thresholds=th).name == "halt"


def test_the_emergency_switch_pauses_a_desk_over_its_usd_cap_and_the_desk_then_refuses_orders(ready):
    service.set_limits(ready.desk_id, {"funding": {"daily_loss_usd": 1000, "trailing_drawdown_usd": 5000}}, db=ready.db)
    assert killswitch.run_once(ready.db) == {}                                              # nothing lost yet
    with ready.db.write() as u:
        u.session.insert_row("book_entries", {"account_id": ready.account_id, "kind": "fee", "amount": "-6000", "currency": "USD", "source": "owner",
                                              "source_ref": "loss1", "occurred_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")})
    assert killswitch.run_once(ready.db) == {ready.desk_id: "halt"}
    with ready.db.read() as u:
        assert u.desks.find(ready.desk_id).status == "paused"
    out = hexec.submit(proposal(ready, origin="chat", created_by="owner"), who=ready.owner, db=ready.db)
    assert out.status == "refused"
    assert killswitch.run_once(ready.db) == {}                                              # a paused desk is left alone


def test_the_ticket_buttons_approve_the_exact_fingerprint_and_an_unauthorized_or_stale_tap_does_nothing(ready):
    levels.set_level(ready.desk_id, 2, who=ready.owner, db=ready.db)
    out = hexec.submit(proposal(ready), who=AGENT, db=ready.db)
    data = telegram_approval.callback_data("approve", out.proposal_id, out.fingerprint)
    assert telegram_approval.parse_callback(data) == ("ok", out.proposal_id, out.fingerprint[:12])
    assert telegram_approval.parse_callback("ox:ok:1:zzzz") is None
    assert telegram_approval.answer(f"ox:ok:{out.proposal_id}:{'0' * 12}", db=ready.db) == "stale"      # not the order the owner saw
    assert telegram_approval.answer(data, db=ready.db) == "filled"
    assert telegram_approval.answer(data, db=ready.db) == "closed"                                      # a second tap changes nothing

    from hokma.exec.telegram_approval import make_ui_class

    class Base:
        def __init__(self, *a, **k):
            self._t = lambda key, **kw: key

        async def _callback_authorized(self, query, ctx, text):
            return query.allowed

        def _callback_ctx(self, query):
            return {}

    ui = make_ui_class(Base)()
    query = SimpleNamespace(data=data, allowed=False, answered=[])

    async def answer(text=None):
        query.answered.append(text)
    query.answer = answer
    asyncio.run(ui.handle_callback_query(SimpleNamespace(callback_query=query)))
    assert query.answered == []                                                                         # the gate refused before any approval ran


def test_the_text_of_the_ticket_message_is_escaped(ready):
    message, keyboard = telegram_approval.build_ticket_message(1, "a" * 64, "<b>x</b> & y", ("A", "R"))
    assert "&lt;b&gt;" in message and "<b>" not in message and keyboard.inline_keyboard[0][0].callback_data.startswith("ox:ok:1:")


def test_a_failed_notice_does_not_hold_back_the_next_ones(env):
    from hokma.telegram import glue
    from hokma.db import get_database
    db = get_database()
    from hokma.db import migrate
    migrate()
    with db.write() as u:
        for n in range(3):
            glue.order_notice(u, "filled", f"notice {n}")
    with db.read() as u:
        assert [r["text"] for r in u.session.query("SELECT text FROM telegram_outbox ORDER BY id")] == ["notice 0", "notice 1", "notice 2"]


def test_a_usd_trailing_drawdown_alone_halts_the_desk_with_no_percent_threshold(ready):
    assert killswitch.thresholds_of({"funding": {"trailing_drawdown_usd": 2000}}) == {"halt_drawdown_usd": 2000.0}
    assert monitor.kill_level(drawdown_pct=None, day_loss_usd=None, drawdown_usd=2500, thresholds={"halt_drawdown_usd": 2000}).name == "halt"
    assert monitor.kill_level(drawdown_pct=None, day_loss_usd=None, drawdown_usd=100, thresholds={"halt_drawdown_usd": 2000}).name == "ok"
    service.set_limits(ready.desk_id, {"funding": {"trailing_drawdown_usd": 5000}}, db=ready.db)          # nothing else configured
    with ready.db.write() as u:
        u.session.insert_row("book_entries", {"account_id": ready.account_id, "kind": "fee", "amount": "-6000", "currency": "USD", "source": "owner",
                                              "source_ref": "dd1", "occurred_at": "2026-01-01 00:00:00"})
    assert killswitch.run_once(ready.db) == {ready.desk_id: "halt"}


class _Dies(BaseException):
    pass


def test_one_failing_desk_does_not_stop_the_pass_and_a_dead_task_is_restarted_and_a_stale_monitor_blocks_openings(ready, monkeypatch):
    calls = []

    def flaky(db=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        if len(calls) == 2:
            raise _Dies("the task itself dies")                      # escapes the except Exception of the loop
        return {}

    monkeypatch.setattr(killswitch, "run_once", flaky)

    async def scenario():
        task = killswitch.KillSwitchTask(ready.db, interval=0.01)
        task.start()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(calls) >= 4:
                break
        await task.stop()
        return task.restarts

    assert asyncio.run(scenario()) >= 1 and len(calls) >= 3             # the first error was logged and the loop went on; the death was restarted
    # a monitor that read the desk once and then stopped: after two intervals the filter does not let a new opening through
    service.set_limits(ready.desk_id, {"funding": {"trailing_drawdown_usd": 5000}}, db=ready.db)
    with ready.db.write() as u:
        u.session.insert_row("exec_guard_state", {"desk_id": ready.desk_id, "guard": "killswitch", "key": "heartbeat",
                                                  "state_json": '{"at": "2020-01-01 00:00:00"}', "updated_at": "2020-01-01 00:00:00"})
    out = hexec.submit(proposal(ready, origin="chat", created_by="owner"), who=ready.owner, db=ready.db)
    assert (out.status, out.code) == ("refused", "monitor_stale")
    killswitch.beat(ready.desk_id, db=ready.db)                              # the monitor reads again
    assert hexec.submit(proposal(ready, origin="chat", created_by="owner"), who=ready.owner, db=ready.db).status == "filled"


def test_a_pass_that_measured_nothing_is_not_a_heartbeat_and_new_openings_stay_blocked(ready):
    from hokma.backoffice import book as book_module
    from hokma.broker.types import Fill

    service.set_limits(ready.desk_id, {"funding": {"trailing_drawdown_usd": 5000}}, db=ready.db)
    book_module.record_fill(Fill(ready.account_id, NQ, "buy", 1.0, 21000.0, 0.0, "2026-11-20 15:00:00", multiplier=D(20), contract_id=NQ),
                            desk_id=ready.desk_id, source="sim", source_ref="open1")        # an open position with no price: the equity is unknown
    from hokma.exec import prefilter

    prefilter.set_mark_source(lambda ids: {})                                                  # no price for the open position
    assert killswitch.check_desk(ready.desk_id, db=ready.db).measured is False
    killswitch.run_once(ready.db)
    assert killswitch.load_heartbeat(ready.desk_id, db=ready.db) is None
    out = hexec.submit(proposal(ready, origin="chat", created_by="owner"), who=ready.owner, db=ready.db)
    assert (out.status, out.code) == ("refused", "monitor_never_measured")


@pytest.mark.parametrize("limits, priced, expected", [
    ({"funding": {"trailing_drawdown_usd": 5000}}, True, True),
    ({"funding": {"trailing_drawdown_usd": 5000}}, False, False),
    ({"funding": {"daily_loss_usd": 1000}}, True, True),
    ({"funding": {"daily_loss_usd": 1000}}, False, False),          # an open position with no price: its loss is not in the day result
    ({"funding": {"trailing_drawdown_usd": 5000, "daily_loss_usd": 1000}}, True, True),
    ({"funding": {"trailing_drawdown_usd": 5000, "daily_loss_usd": 1000}}, False, False),
])
def test_measured_needs_every_armed_threshold_compared_with_a_real_value(ready, limits, priced, expected):
    from hokma.backoffice import book as book_module
    from hokma.broker.types import Fill
    from hokma.exec import prefilter

    service.set_limits(ready.desk_id, limits, db=ready.db)
    book_module.record_fill(Fill(ready.account_id, NQ, "buy", 1.0, 21000.0, 0.0, "2026-11-20 15:00:00", multiplier=D(20), contract_id=NQ),
                            desk_id=ready.desk_id, source="sim", source_ref="p1")
    if not priced:
        prefilter.set_mark_source(lambda ids: {})
    assert killswitch.check_desk(ready.desk_id, db=ready.db).measured is expected
