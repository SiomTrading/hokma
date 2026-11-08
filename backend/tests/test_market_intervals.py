"""T-4 (Python side): the interval grammar against the table shared with the TypeScript validator."""
import json
from pathlib import Path

import pytest

from hokma.market.intervals import (IntervalError, RangeInterval, TimeInterval, parse, timeframe_seconds,
                                    visible_bars)

CASES = json.loads((Path(__file__).parent / "market_fixtures" / "intervals_cases.json").read_text(encoding="utf-8"))["cases"]


def test_the_shared_table_has_enough_cases():
    assert len(CASES) >= 30


@pytest.mark.parametrize("case", CASES, ids=lambda case: repr(case["input"]))
def test_shared_table(case):
    if not case["ok"]:
        with pytest.raises(IntervalError) as error:
            parse(case["input"])
        assert error.value.code == case["error"]
        return
    found = parse(case["input"])
    assert found.canonical == case["canonical"]
    if case["kind"] == "time":
        assert isinstance(found, TimeInterval)
        assert (found.n, found.unit, found.seconds) == (case["n"], case["unit"], case["seconds"])
    else:
        assert isinstance(found, RangeInterval)
        assert (found.size, found.unit) == (case["size"], case["unit"])


def test_protocol_shapes():
    assert parse("7m").to_protocol() == {"kind": "time", "seconds": 420}
    assert parse("r:10t").to_protocol(0.25) == {"kind": "range", "size": 10, "unit": "t", "tick_size": 0.25}
    assert parse("2.5rp").to_protocol() == {"kind": "range", "size": 2.5, "unit": "p", "tick_size": None}


def test_non_text_is_invalid():
    for value in (None, 5, b"7m"):
        with pytest.raises(IntervalError):
            parse(value)


def test_days_and_weeks_are_sessions_not_seconds():
    assert parse("1D").is_session_based and parse("2W").is_session_based
    assert not parse("24h").is_session_based and not parse("168h").is_session_based


def test_helpers_for_canonical_text():
    assert timeframe_seconds("45m") == 2700
    assert visible_bars("5m") == 180 and visible_bars("7m") == 300 and visible_bars("1D") == 180
    with pytest.raises(IntervalError):
        timeframe_seconds("r:10t")
