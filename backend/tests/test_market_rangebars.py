import json
from types import SimpleNamespace

import pytest

from hokma.market import instruments
from hokma.market.rangebars import RangeBar, RangeBarBuilder, approximate_from_minutes, build_from_ticks, splice
from hokma.market.tape import CAPACITY, TickTape


def T(bars):
    return [(b.i, b.t, b.o, b.h, b.l, b.c, b.v) for b in bars]


# ES, tick 0.25, range 10 ticks = 2.5. Expected table calculated by hand.
FIXED = [
    (1000, 100.00, 1), (1100, 101.00, 2), (1200, 102.50, 1),   # up; 102.5 is exactly the edge: stays
    (1300, 102.75, 3),                                          # 2.75 > 2.5: bar 0 closes at 102.5, bar 1 opens 102.75
    (2000, 104.00, 2), (2100, 105.25, 1), (2200, 103.00, 4),    # bar 1 grows to exactly 2.5
    (2300, 102.50, 2),                                          # down: bar 1 closes at 102.75, bar 2 opens 102.50
    (3000, 102.00, 1),
    (3100, 94.00, 5),                                           # jump of 3 ranges
    (4000, 94.50, None),                                        # no size: price counts, volume does not
    (4100, 97.00, 2),                                           # reversal up
]
EXPECTED = [
    (0, 1, 100.00, 102.50, 100.00, 102.50, 4),
    (1, 1, 102.75, 105.25, 102.75, 102.75, 10),
    (2, 2, 102.50, 102.50, 100.00, 100.00, 3),
    (3, 3, 99.75, 99.75, 97.25, 97.25, 0),
    (4, 3, 97.00, 97.00, 94.50, 94.50, 0),
    (5, 3, 94.25, 96.50, 94.00, 96.50, 5),
    (6, 4, 96.75, 97.00, 96.75, 97.00, 2),
]


def test_fixed_sequence_matches_hand_table():
    builder = build_from_ticks(FIXED, 2.5, 0.25)
    assert T(builder.bars()) == EXPECTED
    assert [b.approx for b in builder.bars()] == [False] * 7
    assert sum(b.v for b in builder.bars()) == sum(s or 0 for _, _, s in FIXED)
    assert T([builder.current()]) == [EXPECTED[-1]]
    assert T(builder.closed()) == EXPECTED[:-1]


def test_jump_reports_closed_bars_then_current():
    builder = build_from_ticks(FIXED[:9], 2.5, 0.25)
    changes = builder.on_tick(3100, 94.00, 5)
    assert [c.final for c in changes] == [True, True, True, False]
    assert [c.bar.i for c in changes] == [2, 3, 4, 5]


def test_first_tick_opens_a_bar():
    builder = RangeBarBuilder(2.5, 0.25)
    assert builder.bars() == [] and builder.current() is None
    changes = builder.on_tick(5000, 100.0, 3)
    assert len(changes) == 1 and changes[0].final is False
    assert changes[0].bar == RangeBar(0, 5, 100.0, 100.0, 100.0, 100.0, 3, False)


def test_tick_exactly_on_the_edge_stays_in_the_bar():
    bars = build_from_ticks([(0, 100.0, 1), (1, 102.5, 1), (2, 100.0, 1)], 2.5, 0.25).bars()
    assert len(bars) == 1 and (bars[0].h, bars[0].l) == (102.5, 100.0)


def test_reversal_after_a_rise():
    bars = build_from_ticks([(0, 100.0, 1), (1, 102.75, 1), (2, 100.25, 1), (3, 100.0, 1)], 2.5, 0.25).bars()
    assert T(bars) == [(0, 0, 100.0, 102.5, 100.0, 102.5, 1), (1, 0, 102.75, 102.75, 100.25, 100.25, 2),
                       (2, 0, 100.0, 100.0, 100.0, 100.0, 1)]


def test_ticks_without_size_move_price_not_volume():
    bars = build_from_ticks([(0, 100.0, None), (1, 101.0, 0), (2, 103.0, None), (3, 103.0, 4)], 2.5, 0.25).bars()
    assert T(bars) == [(0, 0, 100.0, 102.5, 100.0, 102.5, 0), (1, 0, 102.75, 103.0, 102.75, 103.0, 4)]


def test_invalid_prices_are_ignored():
    builder = RangeBarBuilder(2.5, 0.25)
    for bad in (0, -1, float("nan"), float("inf"), None):
        assert builder.on_tick(0, bad, 1) == []
    assert builder.bars() == []


def test_unknown_tick_opens_at_the_edge():
    bars = build_from_ticks([(0, 100.0, 1), (1, 110.0, 1), (2, 110.5, 2)], 10, None).bars()
    assert T(bars) == [(0, 0, 100.0, 110.0, 100.0, 110.0, 2), (1, 0, 110.0, 110.5, 110.0, 110.5, 2)]


def test_unknown_tick_jump_creates_intermediate_bars_at_the_edge():
    bars = build_from_ticks([(0, 100.0, 1), (1, 135.0, 1)], 10, None).bars()
    assert T(bars) == [(0, 0, 100.0, 110.0, 100.0, 110.0, 1), (1, 0, 110.0, 120.0, 110.0, 120.0, 0),
                       (2, 0, 120.0, 130.0, 120.0, 130.0, 0), (3, 0, 130.0, 135.0, 130.0, 135.0, 1)]


def test_forex_prices_are_exact():
    bars = build_from_ticks([(0, 1.1000, 1), (1, 1.1010, 1), (2, 1.1011, 1)], 0.0010, 0.0001).bars()
    assert len(bars) == 2
    assert bars[0].h == 1.101 and bars[0].c == 1.101 and bars[1].o == 1.1011


def test_float_noise_does_not_break_the_edge():
    # 2000.4 - 2000.1 is 0.30000000000018 in floating point: it must still count as exactly 3 ticks of 0.1.
    bars = build_from_ticks([(0, 2000.1, 1), (1, 2000.4, 1)], 0.3, 0.1).bars()
    assert len(bars) == 1 and bars[0].h == 2000.4
    bars = build_from_ticks([(0, 2000.1, 1), (1, 2000.4, 1), (2, 2000.5, 1)], 0.3, 0.1).bars()
    assert T(bars) == [(0, 0, 2000.1, 2000.4, 2000.1, 2000.4, 2), (1, 0, 2000.5, 2000.5, 2000.5, 2000.5, 1)]


def test_es_prices_exact_on_a_long_walk():
    builder = RangeBarBuilder(2.5, 0.25)
    price = 5000.0
    for n in range(2000):
        price += 0.25 if (n // 20) % 2 == 0 else -0.25
        builder.on_tick(n, price, 1)
    closed = builder.closed()
    assert closed
    for b in closed:
        assert b.h - b.l == 2.5
        assert b.h * 4 == int(b.h * 4) and b.l * 4 == int(b.l * 4)


def test_bad_constructor_args():
    for rp, ts in ((0, 0.25), (-1, 0.25), (float("nan"), None), (1, 0), (1, 0.3)):
        with pytest.raises(ValueError):
            RangeBarBuilder(rp, ts)


# Approximate bars from 1-minute candles. ES tick 0.25, R 2.5.
C1 = SimpleNamespace(t=1000, o=100.0, h=103.0, l=99.5, c=102.0, v=50)    # closed up: O, L, H, C
C2 = SimpleNamespace(t=1060, o=102.0, h=104.0, l=100.0, c=101.0, v=30)   # closed down: O, H, L, C


def test_approximate_paths():
    bars = approximate_from_minutes([C1, C2], 2.5, 0.25)
    assert T(bars) == [(0, 1000, 100.0, 102.0, 99.5, 102.0, 50),
                       (1, 1030, 102.25, 104.0, 101.5, 101.5, 30),
                       (2, 1090, 101.25, 101.25, 100.0, 101.0, 0)]
    assert all(b.approx for b in bars)
    assert sum(b.v for b in bars) == 80


def test_approximate_single_candle_and_until():
    assert T(approximate_from_minutes([C1], 2.5, 0.25)) == [(0, 1000, 100.0, 102.0, 99.5, 102.0, 50),
                                                           (1, 1030, 102.25, 103.0, 102.0, 102.0, 0)]
    assert len(approximate_from_minutes([C1, C2], 2.5, 0.25, until_ts=1060)) == 2
    assert approximate_from_minutes([C1, C2], 2.5, 0.25, until_ts=1000) == []


def test_approximate_up_equal_close_goes_low_before_high():
    flat = SimpleNamespace(t=0, o=100.0, h=101.0, l=99.0, c=100.0, v=1)
    # Range 1.5: O,L,H,C closes the first bar going up, at low + R = 100.5.
    bars = approximate_from_minutes([flat], 1.5, 0.25)
    assert T(bars) == [(0, 0, 100.0, 100.5, 99.0, 100.5, 1), (1, 30, 100.75, 101.0, 100.0, 100.0, 0)]
    # O,H,L,C (a down candle with the same prices) would close it going down instead, at high - R = 99.5.
    down = SimpleNamespace(t=0, o=100.0, h=101.0, l=99.0, c=99.9, v=1)
    assert approximate_from_minutes([down], 1.5, 0.25)[0].h == 101.0
    assert approximate_from_minutes([down], 1.5, 0.25)[0].c == 99.5


def test_splice_reindexes_and_reports_boundary():
    approx = approximate_from_minutes([C1, C2], 2.5, 0.25)   # bars opening at 1000, 1030, 1090
    exact = build_from_ticks([(1_070_000, 101.0, 1), (1_071_000, 104.0, 1)], 2.5, 0.25).bars()
    merged, approx_until = splice(approx, exact, 1070)
    assert approx_until == 1070
    assert [b.i for b in merged] == list(range(len(merged)))
    assert [b.approx for b in merged] == [True, True] + [False] * len(exact)
    assert [b.t for b in merged[:2]] == [1000, 1030] and merged[2].t == 1070
    # When the tape starts before every approximate bar, none is kept.
    merged, approx_until = splice(approx, exact, 100)
    assert approx_until is None and not any(b.approx for b in merged) and len(merged) == len(exact)
    # Without exact bars everything is approximate and there is no boundary.
    merged, approx_until = splice(approx, [])
    assert len(merged) == 3 and approx_until is None
    # exact_from defaults to the open time of the first exact bar.
    assert splice(approx, exact)[1] == 1070


# Instruments
def test_tick_sizes():
    assert instruments.tick_size("NQ=F", "future") == 0.25
    assert instruments.tick_size("MES=F", "future") == 0.25
    assert instruments.tick_size("YM=F", "future") == 1
    assert instruments.tick_size("M2K=F", "future") == 0.1
    assert instruments.tick_size("NG=F", "future") == 0.001
    assert instruments.tick_size("SI=F", "future") == 0.005
    assert instruments.tick_size("ZZ=F", "future") is None
    assert instruments.tick_size("AAPL", "equity") == 0.01
    assert instruments.tick_size("EURUSD=X", "forex") == 0.0001
    assert instruments.tick_size("USDJPY=X", "forex") == 0.01
    assert instruments.tick_size("EURJPY=X", "forex") == 0.01
    assert instruments.tick_size("BTC-USD", "crypto") is None
    assert instruments.tick_size("^GSPC", None) is None
    assert set(instruments.roots()) == {"NQ", "ES", "MNQ", "MES", "YM", "MYM", "RTY", "M2K", "CL", "MCL", "NG",
                                        "GC", "MGC", "SI"}


def test_table_is_a_json_file_next_to_the_module():
    data = json.loads(instruments.TABLE_PATH.read_text(encoding="utf-8"))
    assert data["future"]["NQ"] == 0.25


# Tape (T-9, cap part)
def test_tape_cap_small():
    tape = TickTape(capacity=5)
    for n in range(12):
        tape.append(n * 10, 100.0 + n, n if n % 2 else None)
        assert len(tape) <= 5
    assert len(tape) == 5 and tape.total_appended == 12
    assert tape.first_ts_ms == 70 and tape.last_ts_ms == 110
    assert [t[0] for t in tape] == [70, 80, 90, 100, 110]
    assert list(tape)[0] == (70, 107.0, 7.0) and list(tape)[1] == (80, 108.0, 0.0)
    assert [t[0] for t in tape.iter_since(95)] == [100, 110]
    assert [t[0] for t in tape.iter_since(0)] == [70, 80, 90, 100, 110]
    assert list(tape.iter_since(999)) == []


def test_tape_empty_and_bad_capacity():
    tape = TickTape(3)
    assert len(tape) == 0 and tape.first_ts_ms is None and tape.last_ts_ms is None and list(tape) == []
    assert list(tape.iter_since(0)) == []
    with pytest.raises(ValueError):
        TickTape(0)


def test_tape_cap_real_500k():
    assert CAPACITY == 500_000
    tape = TickTape()
    for n in range(CAPACITY + 7_000):
        tape.append(n, 1.0, 1.0)
        if n % 50_000 == 0:
            assert len(tape) <= CAPACITY
    assert len(tape) == CAPACITY and tape.total_appended == CAPACITY + 7_000
    assert tape.first_ts_ms == 7_000 and tape.last_ts_ms == CAPACITY + 6_999
    assert next(tape.iter_since(400_000))[0] == 400_000


def test_tape_feeds_builder():
    tape = TickTape(100)
    for ts, p, s in FIXED:
        tape.append(ts, p, s)
    assert T(build_from_ticks(tape, 2.5, 0.25).bars()) == EXPECTED
