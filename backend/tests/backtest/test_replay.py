"""The replay of the tribunal (SPEC-020: TA-81, TA-82, K28): a failed cell does not stop the others, a second call redoes only the failed
cell, a DT line is refused, nothing is written to the lessons, and the score by direction uses the horizon of the line with hand computed rates."""
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db, make_desk  # noqa: F401  (fixtures)

from hokma.backtest import replay
from hokma.backtest.replay import ReplayCell, ReplayDecision, ReplayError, ReplaySpec

DAYS = tuple(date(2025, 10, d) for d in (6, 7, 8, 9, 10))          # Monday to Friday
SYMBOLS = ("AAA", "BBB")


@pytest.fixture
def rdb(desks_db):
    """The desks database with one real desk: the run record points at it. It is the first one, so its id is 1."""
    assert make_desk(desks_db)["id"] == 1
    return desks_db


def spec(desk_id=1, **over):
    base = dict(desk_id=desk_id, line="SWC", symbols=SYMBOLS, dates=DAYS, engine_id="tribunal-fake-1")
    return ReplaySpec(**{**base, **over})


class FakeEngine:
    """Stands in for the tribunal: the buy or sell it returns depends on the cell, and chosen cells raise."""

    def __init__(self, failing=()):
        self.failing = set(failing)
        self.calls: list[str] = []

    def __call__(self, cell: ReplayCell):
        self.calls.append(cell.key)
        if cell.key in self.failing:
            raise RuntimeError(f"model timeout on {cell.key}")
        return {"direction": "buy" if cell.day.day % 2 == 0 else "sell", "confidence": 60}


def count(rdb, table):
    with rdb.read() as u:
        return int(u.session.scalar(f"SELECT COUNT(*) FROM {table}") or 0)


def test_a_failed_cell_is_stored_failed_and_the_other_nine_finish_TA_81(rdb):
    bad = "2025-10-08|BBB"
    engine = FakeEngine(failing={bad})
    first = replay.run_replay(spec(), engine, db=rdb)
    assert len(first.cells) == 10 and (first.done, first.failed, first.ran, first.skipped) == (9, 1, 10, 0)
    failed = [c for c in first.cells if c.status == "failed"]
    assert [(c.day, c.symbol) for c in failed] == [(date(2025, 10, 8), "BBB")]
    assert failed[0].error == "RuntimeError: model timeout on 2025-10-08|BBB" and failed[0].decision is None
    assert len(engine.calls) == 10
    assert first.cells[0].decision == {"direction": "buy", "confidence": 60}            # 2025-10-06 is an even day, so the fake buys
    # the grid is in date order and, inside a date, in symbol order
    assert [c.symbol for c in first.cells[:4]] == ["AAA", "BBB", "AAA", "BBB"]


def test_running_again_redoes_only_the_failed_cell_TA_81(rdb):
    bad = "2025-10-08|BBB"
    replay.run_replay(spec(), FakeEngine(failing={bad}), db=rdb)
    healed = FakeEngine()
    second = replay.run_replay(spec(), healed, db=rdb)
    assert healed.calls == [bad]                                                        # only the failed cell is run again
    assert (second.done, second.failed, second.ran, second.skipped) == (10, 0, 1, 9)
    third = FakeEngine()
    again = replay.run_replay(spec(), third, db=rdb)
    assert third.calls == [] and (again.done, again.ran, again.skipped) == (10, 0, 10)
    assert [c.decision for c in again.cells] == [c.decision for c in second.cells]


def test_a_failed_cell_that_fails_again_stays_failed_and_a_changed_tribunal_starts_over_TA_81(rdb):
    bad = "2025-10-07|AAA"
    replay.run_replay(spec(), FakeEngine(failing={bad}), db=rdb)
    still = replay.run_replay(spec(), FakeEngine(failing={bad}), db=rdb)
    assert (still.done, still.failed, still.ran) == (9, 1, 1)
    other = FakeEngine()
    fresh = replay.run_replay(spec(engine_id="tribunal-fake-2"), other, db=rdb)          # another build of the tribunal reuses nothing
    assert len(other.calls) == 10 and fresh.skipped == 0 and fresh.fingerprint != still.fingerprint


def test_a_stopped_run_goes_on_in_the_same_record_TA_81(rdb):
    class Stop(BaseException):
        pass

    def stopping(cell):
        if cell.key == "2025-10-08|AAA":
            raise Stop()
        return {"direction": "hold"}

    with pytest.raises(Stop):
        replay.run_replay(spec(), stopping, db=rdb)
    with rdb.read() as u:
        row = u.session.one("SELECT id, status FROM run_records WHERE kind = 'backtest_replay'")
    assert row["status"] == "interrupted"
    engine = FakeEngine()
    done = replay.run_replay(spec(), engine, db=rdb)
    assert done.run_id == row["id"] and len(engine.calls) == 6 and done.skipped == 4 and done.done == 10     # 4 cells were saved before the stop


def test_a_decision_that_is_not_a_decision_is_a_failed_cell_TA_81(rdb):
    def odd(cell):
        return {"direction": "moon"} if cell.symbol == "AAA" else {"direction": "buy", "confidence": 101}

    run = replay.run_replay(spec(dates=DAYS[:1]), odd, db=rdb)
    assert [c.status for c in run.cells] == ["failed", "failed"]
    assert run.cells[0].error.startswith("ReplayError: bad_decision: direction is one of") and "from 0 to 100" in run.cells[1].error


def test_a_replay_spec_with_the_dt_line_fails_replay_not_for_dt_bots_K28():
    with pytest.raises(ReplayError) as info:
        spec(line="DT")
    assert info.value.code == "replay_not_for_dt_bots"
    for line in ("LP", "RF"):
        with pytest.raises(ReplayError) as other:
            spec(line=line)
        assert other.value.code == "bad_replay"


def test_swo_replays_only_on_the_pead_dates_K28(rdb):
    with pytest.raises(ReplayError) as info:
        spec(line="SWO")
    assert info.value.code == "bad_replay"
    run = replay.run_replay(spec(line="SWO", event_source="pead", dates=(DAYS[0],)), lambda c: {"direction": "buy", "expiry": date(2025, 10, 17)}, db=rdb)
    assert run.done == 2 and run.cells[0].decision == {"direction": "buy", "expiry": "2025-10-17"}
    assert replay.decisions_of(run)[0].expiry == date(2025, 10, 17)


@pytest.mark.parametrize("over", [dict(symbols=()), dict(symbols=("A", "A")), dict(dates=()), dict(dates=(DAYS[0], DAYS[0])), dict(engine_id=""),
                                  dict(symbols=("A|B",)), dict(desk_id=True)])
def test_a_bad_grid_is_refused(over):
    with pytest.raises(ReplayError) as info:
        spec(**over)
    assert info.value.code == "bad_replay"


def test_a_replay_writes_no_lesson_decision_1093(rdb):
    tables = ("lessons", "thesis_events", "decisions", "broker_orders")
    before = {t: count(rdb, t) for t in tables}
    replay.run_replay(spec(), FakeEngine(failing={"2025-10-08|BBB"}), db=rdb)
    replay.run_replay(spec(), FakeEngine(), db=rdb)
    assert {t: count(rdb, t) for t in tables} == before
    source = Path(replay.__file__).read_text()
    assert "hokma.lessons" not in source and "hokma.broker" not in source and "hokma.exec" not in source


# ------------------------------------------------------------------------------------------------------------------------------ the score
CLOSES = [100, 102, 101, 105, 104, 103, 99, 100, 98, 97, 101, 102]
BARS = {"AAA": {date(2025, 10, 1) + (date(2025, 10, 2) - date(2025, 10, 1)) * i: Decimal(c) for i, c in enumerate(CLOSES)}}


def day(i):
    return date(2025, 10, 1 + i)


def swc_decisions():
    buys = [0, 1, 3, 4, 8, 9, 10]
    sells = [2, 5, 6, 7, 11]
    out = [ReplayDecision("AAA", day(i), "buy") for i in buys] + [ReplayDecision("AAA", day(i), "sell") for i in sells]
    return out + [ReplayDecision("BBB", day(i), "hold") for i in range(8)]


def test_the_score_by_direction_uses_the_horizon_of_the_line_TA_82():
    decisions = swc_decisions()
    assert len(decisions) == 20
    score = replay.score_directions(decisions, BARS, line="SWC", horizon_days=2)
    # horizon 2 bars: the close two bars later against the close of the day.
    # buys: i0 101-100 up hit, i1 105-102 up hit, i3 103-105 down miss, i4 99-104 down miss, i8 101-98 up hit, i9 102-97 up hit, i10 no bar
    # sells: i2 104-101 up miss, i5 100-103 down hit, i6 98-99 down hit, i7 97-100 down hit, i11 no bar
    assert score.by_direction["buy"] == {"decisions": 7, "scored": 6, "hits": 4, "rate": Decimal("0.6667")}
    assert score.by_direction["sell"] == {"decisions": 5, "scored": 4, "hits": 3, "rate": Decimal("0.7500")}
    assert score.by_direction["hold"] == {"decisions": 8, "rate": None}
    assert score.sample_size == 10 and score.indicative is True and score.horizon == "2 days"
    assert score.unscored == (("AAA", day(10), "no_bar_at_horizon"), ("AAA", day(11), "no_bar_at_horizon"))
    assert score.to_dict()["indicative"] is True and score.to_dict()["sample_size"] == 10


def test_another_horizon_gives_another_score_TA_82():
    one = replay.score_directions(swc_decisions(), BARS, line="SWC", horizon_days=1)
    # one bar ahead. buys: i0 102 up hit, i1 101 down miss, i3 104 down miss, i4 103 down miss, i8 97 down miss, i9 101 up hit, i10 102 up hit
    # sells: i2 105 up miss, i5 99 down hit, i6 100 up miss, i7 98 down hit, i11 no bar
    assert one.by_direction["buy"] == {"decisions": 7, "scored": 7, "hits": 3, "rate": Decimal("0.4286")}
    assert one.by_direction["sell"] == {"decisions": 5, "scored": 4, "hits": 2, "rate": Decimal("0.5000")}
    assert one.sample_size == 11


def test_swo_scores_at_the_expiry_of_the_structure_TA_82():
    decisions = [ReplayDecision("AAA", day(0), "buy", day(3)),        # 105 against 100: up, hit
                 ReplayDecision("AAA", day(1), "sell", day(5)),       # 103 against 102: up, miss
                 ReplayDecision("AAA", day(2), "buy", day(2)),        # the expiry is not after the decision
                 ReplayDecision("AAA", day(3), "sell", day(30)),      # no bar at the expiry: reported, not filled
                 ReplayDecision("AAA", day(4), "buy"),                # no expiry
                 ReplayDecision("AAA", day(5), "hold", day(9))]
    score = replay.score_directions(decisions, BARS, line="SWO")
    assert score.horizon == "expiry"
    assert score.by_direction["buy"] == {"decisions": 3, "scored": 1, "hits": 1, "rate": Decimal("1.0000")}
    assert score.by_direction["sell"] == {"decisions": 2, "scored": 1, "hits": 0, "rate": Decimal("0.0000")}
    assert score.by_direction["hold"] == {"decisions": 1, "rate": None}
    assert [u[2] for u in score.unscored] == ["expiry_not_after_decision", "no_bar_at_expiry", "no_expiry"]
    assert score.sample_size == 2 and score.indicative is True


def test_an_unchanged_price_is_a_miss_and_a_missing_symbol_is_unscored_TA_82():
    flat = {"AAA": {day(0): Decimal(10), day(1): Decimal(10)}}
    score = replay.score_directions([ReplayDecision("AAA", day(0), "buy"), ReplayDecision("AAA", day(0), "sell"), ReplayDecision("ZZZ", day(0), "buy")],
                                    flat, line="SWC", horizon_days=1)
    assert score.by_direction["buy"]["hits"] == 0 and score.by_direction["sell"]["hits"] == 0 and score.sample_size == 2
    assert score.unscored == (("ZZZ", day(0), "no_bar_on_decision_day"),)


def test_the_score_refuses_dt_and_a_bad_horizon_TA_82():
    with pytest.raises(ReplayError) as info:
        replay.score_directions([], {}, line="DT")
    assert info.value.code == "replay_not_for_dt_bots"
    for bad in (None, 0, -1, 261, True):
        with pytest.raises(ReplayError):
            replay.score_directions([], {}, line="SWC", horizon_days=bad)


def test_the_score_reads_the_decisions_of_a_run(rdb):
    run = replay.run_replay(spec(dates=DAYS[:2]), lambda c: {"direction": "hold" if c.symbol == "BBB" else "buy"}, db=rdb)
    found = replay.decisions_of(run)
    assert [(d.symbol, d.direction) for d in found] == [("AAA", "buy"), ("BBB", "hold"), ("AAA", "buy"), ("BBB", "hold")]
