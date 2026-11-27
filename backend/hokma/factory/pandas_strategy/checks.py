"""The dry run of a strategy in pandas (1.3.0, task 3433): what has to be true before it is kept, and nothing else.

The same table goes through the same loader that the backtest will use (in this process, as any Python), and the strategy must:
1. have the format (PARAMS and generate_signals) and load;
2. RUN: no error with the default parameters and with every parameter at its minimum and at its maximum (the corners of the space the optimizer will walk), and an answer in the format;
3. DECIDE: at least one entry on the sample;
4. be DETERMINISTIC: the same table twice gives the same signals;
5. not look at the FUTURE: the table is cut at 30 to 50 points chosen at random plus every change of session and of regular hours, and the signals of the bars before each cut must be the same as in the
   whole table. A signal that changes when the later bars are removed was made with them.

The table is a synthetic one made by Hokma from a seed (the same every time, 10 Globex sessions of 5 minute bars with their weekend, so the columns session_id and is_rth have all their cases). Every
failure is a `Problem` with its rule, its line or field and the exact change. A strategy that passes is correct in these four senses; whether it earns money is the business of the backtest.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from hokma.factory.pandas_strategy import contract, loader, sessions
from hokma.factory.pandas_strategy.contract import Problem, Signals
from hokma.factory.pandas_strategy.loader import Strategy, StrategyError

SEED = 20251110
SESSIONS = 10
BAR_SECONDS = 300
MIN_CUTS, MAX_CUTS = 30, 50
FIRST_CUT = 3
MAX_PROBLEMS = 8
START_PRICE = 20_000.0
TICK = 0.25
LEAK_SUSPECTS = (
    (re.compile(r"\.shift\(\s*-"), "shift with a negative number reads the next bar"),
    (re.compile(r"center\s*=\s*True"), "center=True makes a window that looks at the bars after"),
    (re.compile(r"\.bfill\(|method\s*=\s*['\"]bfill|fillna\([^)]*['\"](bfill|backfill)"), "bfill fills from the bars after"),
    (re.compile(r"\.iloc\[\s*-\s*1\s*\]|\.tail\(|\.iloc\[\s*-"), "iloc[-1] or tail is the last bar of the whole table, not of the bar you are on"),
    (re.compile(r"\.(max|min|mean|std|median|sum|quantile|var)\(\s*\)(?!\.)(?<!rolling\(\)\.max\(\))"), "a statistic of the whole column uses every bar, the later ones too: use rolling(n) or expanding()"),
    (re.compile(r"\.transform\(\s*['\"](max|min|mean|std|sum|last|median)['\"]"), "transform('max'/'last'/...) of a group is the value of the whole session, the end of it included: use cummax(), cumsum() or expanding()"),
    (re.compile(r"\.groupby\([^)]*\)\.(max|min|mean|std|sum|last)\("), "a group statistic of the whole session includes the bars after the current one: use the cumulative form"),
    (re.compile(r"\.rank\(|\.sort_values\(|\.nlargest\(|\.nsmallest\(|\.idxmax\(|\.idxmin\(|\.argmax\(|\.argmin\("), "ranking or sorting the whole column compares each bar with the later ones"),
    (re.compile(r"\[\s*::\s*-1\s*\]"), "reversing the series reads the bars from the end"),
    (re.compile(r"np\.(max|min|mean|std|median|nanmax|nanmin|nanmean|nanstd|percentile|quantile|nanpercentile)\("), "a numpy statistic of the whole array uses every bar, the later ones too"),
)


WINDOWED = re.compile(r"\.(rolling|ewm|expanding)\(|\.cum(sum|max|min|prod)\(|\.diff\(|\.pct_change\(|\.shift\(\s*[0-9]")


@dataclass
class DryRun:
    ok: bool
    problems: list[Problem] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    defs: tuple[contract.ParamDef, ...] = ()
    seconds: float = 0.0

    def to_data(self) -> dict[str, Any]:
        shown = self.problems[:MAX_PROBLEMS]
        return {"ok": self.ok, "problems": [p.to_data() for p in shown], "problem_count": len(self.problems), "more": max(0, len(self.problems) - len(shown)), "stats": self.stats,
                "params": [{"name": d.name, "default": d.default, "min": d.min, "max": d.max, "step": d.step, "type": d.kind} for d in self.defs], "seconds": round(self.seconds, 2)}


# ----------------------------------------------------------------------------------------------------------------------------------------- the table
def sample_bars(seed: int = SEED, sessions_n: int = SESSIONS, bar_seconds: int = BAR_SECONDS) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """(open times in ns UTC, open, high, low, close, volume) of a synthetic future: Globex sessions from 18:00 to 17:00 New York, Monday to Friday, with the weekend between two weeks. The numbers
    come from a seed, so the table is the same in every run and on every machine; it has trends and quiet hours, gaps between sessions and more volume in the regular hours."""
    rng = np.random.default_rng(seed)
    zone = "America/New_York"
    days = pd.bdate_range("2025-06-02", periods=sessions_n)
    stamps: list[pd.DatetimeIndex] = []
    for day in days:
        close = pd.Timestamp(day.year, day.month, day.day, 17, 0, tz=zone)
        previous = day - pd.tseries.offsets.BDay(1)
        open_ = pd.Timestamp(previous.year, previous.month, previous.day, 18, 0, tz=zone)
        if day.day_of_week == 0:                                            # the Monday session opens on Sunday evening
            open_ = pd.Timestamp(day.year, day.month, day.day, 18, 0, tz=zone) - pd.Timedelta(days=1)
        stamps.append(pd.date_range(open_, close, freq=f"{bar_seconds}s", inclusive="left"))
    index = stamps[0].append(stamps[1:]) if len(stamps) > 1 else stamps[0]
    n = len(index)
    local = index
    minute = local.hour * 60 + local.minute
    regular = (minute >= 570) & (minute < 960)
    sigma = np.where(regular, 0.0009, 0.0004) * np.sqrt(bar_seconds / 300.0)
    shocks = rng.standard_normal(n) * sigma
    trend = np.zeros(n)
    for i in range(1, n):
        trend[i] = 0.12 * trend[i - 1] + shocks[i]
    drift = 0.0002 * np.sin(np.arange(n) / 90.0)
    closes = START_PRICE * np.exp(np.cumsum(trend + drift))
    closes = np.round(closes / TICK) * TICK
    opens = np.empty(n)
    opens[0] = START_PRICE
    opens[1:] = closes[:-1]
    first = np.r_[True, np.diff(np.asarray(index.as_unit("ns").asi8)) > bar_seconds * 10**9 * 1.5]       # the first bar of a session gaps from the close before
    opens[first] = np.round((opens[first] * (1 + rng.standard_normal(int(first.sum())) * 0.0015)) / TICK) * TICK
    wick = np.abs(rng.standard_normal((2, n))) * sigma * START_PRICE * 0.5
    highs = np.round((np.maximum(opens, closes) + wick[0]) / TICK) * TICK
    lows = np.round((np.minimum(opens, closes) - wick[1]) / TICK) * TICK
    volume = np.round(rng.lognormal(mean=np.where(regular, 6.2, 4.8), sigma=0.5, size=n)).astype(float)
    times = np.asarray(index.tz_convert("UTC").as_unit("ns").asi8, dtype="int64")      # nanoseconds, whatever unit pandas chose
    return times, {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volume}


def _cut_points(times: np.ndarray, bar_seconds: int, rng: np.random.Generator) -> list[int]:
    """The last bar kept in each cut: 30 to 50 chosen at random, plus the bar before and the bar of each change of session and of regular hours."""
    n = len(times)
    session, rth = sessions.session_columns(times, bar_seconds)
    change = np.flatnonzero(np.r_[False, session[1:] != session[:-1]])
    flip = np.flatnonzero(np.r_[False, rth[1:] != rth[:-1]])
    edges = sorted({int(b) + d for b in (*change[:12], *flip[:12]) for d in (-1, 0) if FIRST_CUT <= int(b) + d < n - 1})
    pool = np.arange(FIRST_CUT, n - 1)
    random_count = int(rng.integers(MIN_CUTS, MAX_CUTS + 1))
    chosen = {int(i) for i in rng.choice(pool, size=min(random_count, len(pool)), replace=False)}
    return sorted(chosen | set(edges))


def suspects(source: str) -> list[tuple[int, str]]:
    """The lines of the code that usually look at the future, as a hint for the fix (they are suspects, not the proof: the proof is the cut)."""
    found = []
    for number, line in enumerate(loader.normalize(source).split("\n"), start=1):
        text = line.split("#", 1)[0]
        windowed = WINDOWED.search(text) is not None                  # a rolling, ewm, expanding or cumulative form of a statistic only sees the bars before
        for pattern, why in LEAK_SUSPECTS:
            if pattern.search(text) and not (windowed and "whole column" in why):
                found.append((number, why))
                break
    return found


# ----------------------------------------------------------------------------------------------------------------------------------------- the run
def _signals(box: Strategy, params: dict[str, Any], times: np.ndarray, columns: dict[str, np.ndarray], bar_seconds: int, label: str) -> tuple[Signals | None, list[Problem]]:
    try:
        out = box.run(params, times, columns, bar_seconds)
    except StrategyError as exc:
        return None, [Problem(p.rule, f"{label}: {p.message}" if label else p.message, p.fix, p.line, p.field) for p in exc.to_problems()]
    signals, problems = contract.build_signals(out, columns["close"])
    return (signals if not problems else None), [Problem(p.rule, f"{label}: {p.message}" if label else p.message, p.fix, p.line, p.field) for p in problems]


def dry_run(source: str, *, bar_seconds: int = BAR_SECONDS, seed: int = SEED) -> DryRun:
    """Every check on the text of a strategy, in this process. Stops at the first stage that fails, because the next ones need it: the format, the load, the run, then the rest."""
    started = time.monotonic()
    problems, defs = loader.read(source)
    if problems:
        return DryRun(False, problems, {"stage": "format"}, defs, time.monotonic() - started)
    text = loader.normalize(source)
    times, columns = sample_bars(seed, SESSIONS, bar_seconds)
    n = len(times)
    stats: dict[str, Any] = {"bars": n, "bar_seconds": bar_seconds, "sessions": SESSIONS}
    try:
        box = loader.load(text)
    except StrategyError as exc:
        return DryRun(False, exc.to_problems(), {**stats, "stage": "load"}, defs, time.monotonic() - started)
    base = contract.defaults(defs)
    full, found = _signals(box, base, times, columns, bar_seconds, "")
    if full is None:
        return DryRun(False, found, {**stats, "stage": "run"}, defs, time.monotonic() - started)
    entries_long, entries_short = int(full.entry_long.sum()), int(full.entry_short.sum())
    stats.update({"entries_long": entries_long, "entries_short": entries_short, "exits": int(full.exit_long.sum() + full.exit_short.sum())})
    problems = []
    if entries_long + entries_short == 0:
        problems.append(Problem("no_decision", f"the strategy made no entry on the {n} bars of the sample ({SESSIONS} sessions) with the default parameters.",
                                "Check the entry condition: a comparison that is never true, a window longer than the sample, or a NaN that makes every comparison false. Loosen it or shorten the window."))
    again, repeat = _signals(box, base, times, columns, bar_seconds, "second run")
    if again is not None and not again.same_as(full):
        problems.append(Problem("not_deterministic", "two runs on the same table gave different signals.", "Remove what changes between runs: random numbers, sample(), the order of a set or the current time. The same bars must give the same signals."))
    problems += repeat
    for which in ("min", "max"):
        corner, errors = _signals(box, contract.corner(defs, which), times, columns, bar_seconds, f"with every parameter at its {which}imum")
        problems += errors
        stats[f"corner_{which}"] = corner is not None
    if problems:
        return DryRun(False, problems, {**stats, "stage": "decide"}, defs, time.monotonic() - started)
    rng = np.random.default_rng(seed + 1)
    cuts = _cut_points(times, bar_seconds, rng)
    stats["cuts"] = len(cuts)
    leaks = []
    for k in cuts:
        part, errors = _signals(box, base, times[: k + 1], {name: values[: k + 1] for name, values in columns.items()}, bar_seconds, f"on the table cut after bar {k}")
        if errors:
            problems += [Problem("fails_on_short_table", e.message, "A strategy runs on a table that grows bar by bar: it must not fail when there are few bars (an index that does not exist yet).", e.line, e.field) for e in errors[:1]]
            break
        if part is None or not part.same_as(full, upto=k + 1):
            leaks.append(k)
    if leaks:
        bad = leaks[0]
        part, _ = _signals(box, base, times[: bad + 1], {name: values[: bad + 1] for name, values in columns.items()}, bar_seconds, "")
        where = next((name for name in contract.SIGNAL_COLUMNS if part is not None and not np.array_equal(part.column(name).astype(float), full.column(name)[: bad + 1].astype(float), equal_nan=True)), "a column")
        hints = suspects(text)
        tail = (" Lines that usually do this: " + "; ".join(f"line {n_} ({why})" for n_, why in hints[:4]) + ".") if hints else ""
        problems.append(Problem("looks_at_future", f"the signals of the bars before bar {bad} are not the same when the table is cut after it: {where} changed. It happened in {len(leaks)} of {len(cuts)} cuts, the first one at bar {bad}.{tail}",
                                "Make each bar depend only on itself and the bars before it: shift(1), rolling(n), ewm(...), expanding(), cumsum(); never shift(-n), center=True, bfill, iloc[-1], a statistic of the whole column or of the whole session.",
                                field=where))
    stats["stage"] = "done"
    return DryRun(not problems, problems, stats, defs, time.monotonic() - started)
