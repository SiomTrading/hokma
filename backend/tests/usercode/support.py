"""Sample rules and helpers of the tests of the isolated code of the strategy coder (SPEC-031)."""
from __future__ import annotations

import textwrap

HEAD = '''"""A rule written for the tests."""
from __future__ import annotations

import math
import numpy as np
from hokma.factory.user_api import ClassVar, IndicatorRequest, Param, ParamSchema, Pct, Price, Ticks, dataclass, fact, view
{extra_import}
{module_extra}
'''

ALPHA_CLASS = '''
@dataclass(frozen=True)
class InsideBarBreakout:
    lookback_bars: int = 3
    SCHEMA: ClassVar[ParamSchema] = ParamSchema((Param("lookback_bars", "int", 3, 2, 50),))
    FACTS: ClassVar[tuple] = ("high", "low", "close")
    LOOKBACK: ClassVar[int] = 50

    def requests(self):
        return ()

    def signal(self, ctx):
{body}
'''

ALPHA_BODY = """\
        high = fact(ctx, "high", 3)
        low = fact(ctx, "low", 3)
        close = fact(ctx, "close", 3)
        if high is None or low is None or close is None:
            return ctx.abstain("not enough bars")
        inside = high[-2] < high[-3] and low[-2] > low[-3]
        if inside and close[-1] > high[-2]:
            return view(ctx, "u_inside_bar", 1.0, (), "the close broke the high of the inside bar")
        return ctx.abstain("no inside bar broken")"""

FILTER_CLASS = '''
@dataclass(frozen=True)
class QuietRange:
    window: int = 20
    SCHEMA: ClassVar[ParamSchema] = ParamSchema((Param("window", "int", 20, 2, 200),))
    FIELDS: ClassVar[tuple] = ("high", "low")
    LOOKBACK: ClassVar[int] = 201

    def requests(self):
        return ()

    def allows(self, *, side, ctx, state):
        high = ctx.series("high")
        low = ctx.series("low")
        if len(high) < self.window + 1:
            return (False, "not enough bars")
        spread = high - low
        if spread[-1] < float(np.mean(spread[-self.window - 1:-1])):
            return (True, None)
        return (False, "range above its average")

    def must_flatten(self, *, ctx):
        return False
'''

EXIT_CLASS = '''
@dataclass(frozen=True)
class MotherBarStop:
    extra_ticks: int = 1
    SCHEMA: ClassVar[ParamSchema] = ParamSchema((Param("extra_ticks", "int", 1, 0, 20),))
    FIELDS: ClassVar[tuple] = ("low",)
    LOOKBACK: ClassVar[int] = 3

    def requests(self):
        return ()

    def levels(self, *, side, ctx):
        low = ctx.series("low")
        if len(low) < 3:
            return (None, None)
        return (Price(float(low[-3])), Ticks(self.extra_ticks + 11))
'''


def source(kind: str = "alpha", *, extra_import: str = "", module_extra: str = "", body: str | None = None, cls: str | None = None) -> str:
    head = HEAD.format(extra_import=extra_import, module_extra=module_extra)
    if cls is None:
        cls = {"alpha": ALPHA_CLASS.format(body=body if body is not None else ALPHA_BODY), "filter": FILTER_CLASS, "exit": EXIT_CLASS}[kind]
    return head + cls


def alpha(body: str, **kw) -> str:
    """The valid alpha with a different body for signal (the lines are indented 8 spaces)."""
    return source("alpha", body=textwrap.indent(textwrap.dedent(body).strip("\n"), " " * 8), **kw)


def alpha_ctx(facts: dict, as_of: str = "2026-01-02T15:00:00+00:00") -> dict:
    return {"as_of": as_of, "symbol": "NQ", "source": "u_test", "facts": facts}


INSIDE_UP = {"high": [10.0, 12.0, 11.5, 12.4], "low": [9.0, 9.5, 10.0, 11.0], "close": [9.8, 11.0, 11.2, 12.3]}
