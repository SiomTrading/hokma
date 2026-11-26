"""B-33, L-49 and N-15: the trailing stops, with prices written by hand. One implementation for the desk and the backtest."""
import ast
from decimal import Decimal as D
from pathlib import Path

import pytest

from hokma.desks.trading import exits
from hokma.desks.trading.exits import ExitError, atr_trailing, trailing_pct

PACKAGE = Path(exits.__file__).resolve().parents[2]


def _walk(step, prices, side="long", **kwargs):
    """Feed the prices one by one, passing the state back in: the list of results."""
    out, state = [], {}
    for price in prices:
        res = step(side, price, **kwargs, **state)
        state = {"extreme": res.extreme, "previous_stop": res.stop}
        out.append(res)
    return out


# ---- B-33: N ATR -----------------------------------------------------------------------------------------------------------

def test_B_33_a_long_stop_rises_with_the_price_and_never_goes_down():
    steps = _walk(lambda s, p, **k: atr_trailing(s, p, atr=2, atr_multiple=3, **k), [100, 105, 103, 108])
    assert [r.stop for r in steps] == [D(94), D(99), D(99), D(102)]       # extreme minus 6; the dip to 103 keeps 99
    assert [r.extreme for r in steps] == [D(100), D(105), D(105), D(108)]
    assert not any(r.stopped for r in steps)


def test_B_33_the_stop_never_goes_down_even_when_the_atr_grows():
    first = atr_trailing("long", 105, atr=2, atr_multiple=3, extreme=105)
    second = atr_trailing("long", 105, atr=4, atr_multiple=3, extreme=first.extreme, previous_stop=first.stop)   # 105 - 12 = 93 is lower
    assert (first.stop, second.stop) == (D(99), D(99))


def test_B_33_the_price_that_touches_the_stop_closes_the_position():
    last = _walk(lambda s, p, **k: atr_trailing(s, p, atr=2, atr_multiple=3, **k), [100, 105, 100, 99])[-1]
    assert (last.stop, last.stopped) == (D(99), True)                  # touching is enough
    assert _walk(lambda s, p, **k: atr_trailing(s, p, atr=2, atr_multiple=3, **k), [100, 105, 99.5])[-1].stopped is False


def test_B_33_a_short_stop_falls_with_the_price_and_never_goes_up():
    steps = _walk(lambda s, p, **k: atr_trailing(s, p, atr=2, atr_multiple=3, **k), [100, 95, 97, 101], side="short")
    assert [r.stop for r in steps] == [D(106), D(101), D(101), D(101)]
    assert [r.extreme for r in steps] == [D(100), D(95), D(95), D(95)]
    assert [r.stopped for r in steps] == [False, False, False, True]


def test_B_33_the_stop_is_rounded_away_from_the_price_so_the_risk_is_never_understated():
    long = atr_trailing("long", 100, atr="2.1", atr_multiple=3, tick="0.25")           # 100 - 6.3 = 93.7
    short = atr_trailing("short", 100, atr="2.1", atr_multiple=3, tick="0.25")         # 100 + 6.3 = 106.3
    assert (long.stop, short.stop) == (D("93.50"), D("106.50"))


def test_B_33_a_variant_only_changes_the_atr_that_is_passed_in():
    short_bars = atr_trailing("long", 21000, atr="12.5", atr_multiple=2, tick="0.25")   # DT: the ATR of short bars
    daily = atr_trailing("long", 21000, atr="250", atr_multiple=2, tick="0.25")         # SWC: the daily ATR
    assert (short_bars.stop, daily.stop, short_bars.basis) == (D("20975.00"), D(20500), "atr")


@pytest.mark.parametrize("kwargs, code", [
    (dict(atr=0), "bad_stop"), (dict(atr=-1), "bad_stop"), (dict(atr=2, atr_multiple=0), "bad_stop"), (dict(atr=float("nan")), "bad_number"),
    (dict(atr=2, side="flat"), "bad_side"), (dict(atr=2, price=0), "bad_price"), (dict(atr=2, previous_stop=0), "bad_stop"),
    (dict(atr=2, extreme=-1), "bad_price"), (dict(atr=2, price=5, atr_multiple=3), "stop_not_positive"), (dict(atr=2, tick=0), "bad_tick"),
])
def test_B_33_inputs_that_cannot_make_a_stop_are_refused_with_a_stable_code(kwargs, code):
    args = dict(side="long", price=100, atr_multiple=3)
    args.update(kwargs)
    side, price = args.pop("side"), args.pop("price")
    with pytest.raises(ExitError) as err:
        atr_trailing(side, price, **args)
    assert err.value.code == code


# ---- L-49: percent from the maximum ----------------------------------------------------------------------------------------

def test_L_49_with_a_maximum_of_100_and_5_percent_the_position_closes_on_touching_95():
    first = trailing_pct("long", 100, pct=5)
    assert (first.stop, first.extreme, first.stopped) == (D(95), D(100), False)
    touched = trailing_pct("long", 95, pct=5, extreme=first.extreme, previous_stop=first.stop)
    assert (touched.stop, touched.stopped) == (D(95), True)
    assert trailing_pct("long", "95.01", pct=5, extreme=100, previous_stop=95).stopped is False


def test_L_49_the_long_stop_never_goes_down():
    steps = _walk(lambda s, p, **k: trailing_pct(s, p, pct=5, **k), [100, 110, 108, 104, 120, 117])
    assert [r.stop for r in steps] == [D(95), D("104.5"), D("104.5"), D("104.5"), D(114), D(114)]
    assert [r.stopped for r in steps] == [False, False, False, True, False, False]
    stops = [r.stop for r in steps]
    assert stops == sorted(stops)


def test_L_49_a_short_follows_the_lowest_price_and_never_goes_up():
    steps = _walk(lambda s, p, **k: trailing_pct(s, p, pct=5, **k), [100, 90, 92, 94.5], side="short")
    assert [r.stop for r in steps] == [D(105), D("94.5"), D("94.5"), D("94.5")]
    assert [r.stopped for r in steps] == [False, False, False, True]


def test_L_49_the_stop_is_rounded_away_from_the_price():
    assert trailing_pct("long", "99.9", pct=5, tick="0.01").stop == D("94.90")          # 99.9 - 4.995 = 94.905, rounded down
    assert trailing_pct("short", "99.9", pct=5, tick="0.01").stop == D("104.90")        # 104.895, rounded up


@pytest.mark.parametrize("pct", [0, -1, 100, 150, float("nan"), None, True])
def test_L_49_a_percent_outside_zero_to_100_is_refused(pct):
    with pytest.raises(ExitError):
        trailing_pct("long", 100, pct=pct)


def test_L_49_a_bad_side_is_refused():
    with pytest.raises(ExitError) as err:
        trailing_pct("up", 100, pct=5)
    assert err.value.code == "bad_side"


# ---- N-15 and the identity of the one implementation -----------------------------------------------------------------------

def _definitions(folders: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for folder in folders:
        for path in (PACKAGE / folder).rglob("*.py"):
            found += [n.name for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return found


def test_N_15_there_is_one_trailing_implementation_and_it_is_the_one_the_other_packages_import():
    defined = _definitions(("backtest", "factory", "desks"))
    assert defined.count("atr_trailing") == 1 and defined.count("trailing_pct") == 1      # only the ones of exits.py
    from hokma.factory import orders

    for name in ("atr_trailing", "trailing_pct"):
        if hasattr(orders, name):                                       # the order factory re-uses, never re-writes
            assert getattr(orders, name) is getattr(exits, name)
    assert atr_trailing.__module__ == trailing_pct.__module__ == "hokma.desks.trading.exits"
