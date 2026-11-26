"""Shared pieces of the tests of the strategy written as code (SPEC-031 A2): a strategy of the bank waiting for its code, a valid requirement and the files of the
inside bar breakout, the one strategy that these tests write, review and run."""
from __future__ import annotations

import copy
from pathlib import Path

from bank_support import IDEA, db, env, pending_sheet, pg_admin_url, pg_template, pg_url_migrated  # noqa: F401  (fixtures)
from e2e_h4.support import call, caller, failed, ok

from hokma.core.config import hokma_home

CODER = "strategy-coder"
REVIEWER = "strategy-reviewer"
RESEARCHER = "strategy-researcher"

BRIEF = {
    "summary": "Buys when the close breaks above the high of an inside bar and sells short when it closes below the low of that bar.",
    "quote": "00:03-00:40", "market": "NQ", "timeframe": "5m", "inputs": ["high", "low", "close"],
    "params": [{"name": "min_range", "type": "float", "value": 0.0, "min": 0.0, "max": 100.0, "origin": "proposed"}],
    "behavior": [{"given": "an inside bar inside the bar before it", "when": "the close is above the high of the inside bar", "then": "the strategy is long"},
                 {"given": "an inside bar inside the bar before it", "when": "the close is below the low of the inside bar", "then": "the strategy is short"}],
    "cases": [{"name": "breaks up", "closes": [10.0, 10.0, 10.0, 11.5], "expect": "long"},
              {"name": "breaks down", "closes": [10.0, 10.0, 10.0, 8.5], "expect": "short"},
              {"name": "stays inside", "closes": [10.0, 10.0, 10.0, 10.0], "expect": "hold"}],
    "out_of_scope": ["options", "order book data"]}

STRATEGY = """from user_api import ClassVar, Param, ParamSchema, dataclass, hold, long, short


@dataclass(frozen=True)
class InsideBarBreakout:
    '''Go long when the close breaks the high of an inside bar, go short when it breaks the low, otherwise do nothing.'''

    SCHEMA: ClassVar[ParamSchema] = ParamSchema((Param("min_range", "float", 0.0, 0.0, 100.0),))
    min_range: float = 0.0

    def init(self, ctx):
        ctx.set_warmup(4)

    def on_bar(self, ctx):
        high, low, close = ctx.high, ctx.low, ctx.close
        if len(close) < 4:
            return hold()
        mother_high, mother_low = high[-3], low[-3]
        inside = high[-2] < mother_high and low[-2] > mother_low and (mother_high - mother_low) >= self.min_range
        if inside and close[-1] > high[-2]:
            return long(reason="the close is above the inside bar")
        if inside and close[-1] < low[-2]:
            return short(reason="the close is below the inside bar")
        return hold()
"""

TEST = """import strategy
from user_api import replay


def make(last_close):
    return {
        "open": [10.0, 10.0, 10.0, 10.0],
        "high": [10.5, 12.0, 11.0, 12.0],
        "low": [9.5, 8.0, 9.0, 8.0],
        "close": [10.0, 10.0, 10.0, last_close],
        "volume": [100.0, 100.0, 100.0, 100.0],
    }


def test_a_close_above_the_inside_bar_goes_long():
    decisions = replay(strategy.InsideBarBreakout, make(11.5))
    assert decisions[-1].action == "long"


def test_a_close_below_the_inside_bar_goes_short():
    decisions = replay(strategy.InsideBarBreakout, make(8.5))
    assert decisions[-1].action == "short"


def test_a_close_inside_the_inside_bar_does_nothing():
    decisions = replay(strategy.InsideBarBreakout, make(10.0))
    assert decisions[-1].action == "hold"


def test_nothing_happens_before_the_warm_up():
    decisions = replay(strategy.InsideBarBreakout, make(11.5))
    assert [d.action for d in decisions[:3]] == ["hold", "hold", "hold"]
"""


def make_bank(db, title: str = "Inside bar breakout") -> int:
    """A strategy of the bank whose entry rule no piece of the factory does: it waits for code."""
    source = ok("source_ingest", {"action": "ingest", "kind": "text", "text": IDEA}, caller("hokma"))
    pid = source["passages"][0]["passage_id"]
    made = ok("bank_create", {"title": title, "source_id": source["source_id"], "extraction": pending_sheet(pid)}, caller(RESEARCHER))
    return made["bank_id"]


def brief(**over):
    data = copy.deepcopy(BRIEF)
    data.update(over)
    return data


def put_files(slug: str, strategy: str = STRATEGY, test: str = TEST) -> Path:
    """The two files where the coder writes them: the working folder of the owner."""
    folder = hokma_home() / "strategies" / slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "strategy.py").write_text(strategy, encoding="utf-8", newline="\n")
    (folder / "test_strategy.py").write_text(test, encoding="utf-8", newline="\n")
    return folder


def rows(db, sql: str, **params):
    with db.read() as u:
        return u.session.query(sql, params)
