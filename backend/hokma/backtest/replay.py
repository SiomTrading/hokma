"""Replay of a desk tribunal over a grid of cells (SPEC-020: TA-81, TA-82, K28).

A replay runs the tribunal of a swing desk (SWC or SWO) on past dates, one cell per date and symbol, and keeps what it decided. It is a way to
test the tribunal and its context, not a backtest of a bot: a DT desk is refused with `replay_not_for_dt_bots`. The model behind the tribunal is
injected as `engine(cell) -> decision`, so a test can give a fake one and the service has no road to a broker or to an order.

Resume: every finished cell is saved as a checkpoint (`run_checkpoints`) under a `run_records` row of kind `backtest_replay`. A cell that raises is
saved as `failed` with its error and the other cells go on. Running the same replay again redoes only the failed cells: a `done` cell of any
earlier run with the same fingerprint is never run twice, a failed one is not final.

What a replay never does: it writes no lesson. A lesson only comes from an executed trade (decision 1093), and a replay executes nothing.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from hokma.backoffice import checkpoint, runs
from hokma.db import Database, get_database
from hokma.desks.core.hashing import content_hash
from hokma.quant import limits

RECORD_KIND = "backtest_replay"
REPLAY_LINES = ("SWC", "SWO")
DIRECTIONS = ("buy", "sell", "hold")
MAX_ERROR = 500
MAX_SYMBOLS = 500
MAX_HORIZON_DAYS = 260
MAX_ID = 80
RATE_PLACES = Decimal("0.0001")
SWO_SOURCE = "pead"


class ReplayError(ValueError):
    """The replay cannot run or be scored as asked. `code` is stable: replay_not_for_dt_bots, bad_replay."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ReplayCell:
    day: date
    symbol: str

    @property
    def key(self) -> str:
        return f"{self.day.isoformat()}|{self.symbol}"


@dataclass(frozen=True)
class ReplaySpec:
    """What a replay runs: the desk, its line, the dates and the symbols. `engine_id` names the tribunal build (models and prompts) so a changed
    tribunal never reuses the cells of another one. SWO runs only on the report dates of the PEAD source."""

    desk_id: int
    line: str
    symbols: tuple[str, ...]
    dates: tuple[date, ...]
    engine_id: str
    event_source: str | None = None

    def __post_init__(self) -> None:
        if self.line == "DT":
            raise ReplayError("replay_not_for_dt_bots", "a replay tests the tribunal of SWC and SWO desks, not a DT bot: use a backtest")
        if self.line not in REPLAY_LINES:
            raise ReplayError("bad_replay", f"a replay runs on the lines {', '.join(REPLAY_LINES)}")
        if self.line == "SWO" and self.event_source != SWO_SOURCE:
            raise ReplayError("bad_replay", f"an SWO replay runs only on the dates of the {SWO_SOURCE} source")
        if isinstance(self.desk_id, bool) or not isinstance(self.desk_id, int):
            raise ReplayError("bad_replay", "desk_id is a whole number")
        if not isinstance(self.engine_id, str) or not 1 <= len(self.engine_id) <= MAX_ID:
            raise ReplayError("bad_replay", f"engine_id is text of 1 to {MAX_ID} characters")
        symbols, dates = tuple(self.symbols), tuple(self.dates)
        if not symbols or len(symbols) > MAX_SYMBOLS or not all(isinstance(s, str) and 1 <= len(s) <= 40 and "|" not in s for s in symbols):
            raise ReplayError("bad_replay", f"a replay has 1 to {MAX_SYMBOLS} symbols, each text of 1 to 40 characters")
        if len(set(symbols)) != len(symbols):
            raise ReplayError("bad_replay", "a symbol appears once")
        if not dates or not all(type(d) is date for d in dates) or len(set(dates)) != len(dates):
            raise ReplayError("bad_replay", "a replay has at least one date and a date appears once")
        limits.check_param("n_packages", len(symbols) * len(dates))
        object.__setattr__(self, "symbols", symbols)
        object.__setattr__(self, "dates", tuple(sorted(dates)))

    def cells(self) -> tuple[ReplayCell, ...]:
        """The grid, date by date and inside a date symbol by symbol."""
        return tuple(ReplayCell(d, s) for d in self.dates for s in self.symbols)

    def fingerprint(self) -> str:
        return content_hash({"desk_id": self.desk_id, "line": self.line, "symbols": list(self.symbols),
                             "dates": [d.isoformat() for d in self.dates], "engine_id": self.engine_id, "event_source": self.event_source})


@dataclass(frozen=True)
class CellResult:
    day: date
    symbol: str
    status: str                                   # done or failed
    decision: Mapping[str, Any] | None = None     # {"direction", "confidence"?, "expiry"?}
    error: str | None = None


@dataclass(frozen=True)
class ReplayRun:
    run_id: int
    fingerprint: str
    cells: tuple[CellResult, ...]                 # every cell of the grid, in grid order
    ran: int                                      # cells run by this call
    skipped: int                                  # cells that an earlier call had finished

    @property
    def done(self) -> int:
        return sum(1 for c in self.cells if c.status == "done")

    @property
    def failed(self) -> int:
        return sum(1 for c in self.cells if c.status == "failed")


def _decision(raw: Any) -> dict[str, Any]:
    """The plain decision of a cell, or ReplayError when the engine returned something that is not one."""
    if not isinstance(raw, Mapping):
        raise ReplayError("bad_decision", "the engine returns a mapping")
    direction = raw.get("direction")
    if direction not in DIRECTIONS:
        raise ReplayError("bad_decision", f"direction is one of {', '.join(DIRECTIONS)}")
    out: dict[str, Any] = {"direction": direction}
    confidence = raw.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, int) or not 0 <= confidence <= 100:
            raise ReplayError("bad_decision", "confidence is a whole number from 0 to 100")
        out["confidence"] = confidence
    expiry = raw.get("expiry")
    if expiry is not None:
        if type(expiry) is not date and not isinstance(expiry, str):
            raise ReplayError("bad_decision", "expiry is a date")
        out["expiry"] = (expiry if type(expiry) is date else date.fromisoformat(expiry)).isoformat()
    return out


def _saved(db: Database, signature: str) -> dict[str, CellResult]:
    """The last state of every cell that any run of this fingerprint saved. A `done` cell is final; a `failed` one is redone."""
    with db.read() as u:
        rows = u.session.query("SELECT c.state_json FROM run_checkpoints c JOIN run_records r ON r.id = c.run_id WHERE r.kind = :k AND r.signature = :s "
                               "ORDER BY r.id, c.node", {"k": RECORD_KIND, "s": signature})
    found: dict[str, CellResult] = {}
    for row in rows:
        state = runs.decode(json.loads(row["state_json"]))["state"]
        day = date.fromisoformat(state["day"])
        key = f"{state['day']}|{state['symbol']}"
        if key in found and found[key].status == "done":
            continue
        found[key] = CellResult(day, state["symbol"], state["status"], state.get("decision"), state.get("error"))
    return found


def _open_record(db: Database, signature: str, desk_id: int) -> int:
    """The run record of this call: the one that was stopped (running or interrupted) goes on, otherwise a new one begins."""
    with db.read() as u:
        latest = u.session.one("SELECT id, status FROM run_records WHERE kind = :k AND signature = :s ORDER BY id DESC LIMIT 1",
                               {"k": RECORD_KIND, "s": signature})
    if latest is not None and latest["status"] in ("running", "interrupted"):
        if latest["status"] == "running":                         # the process that was running it is gone
            runs.interrupt(latest["id"], db=db)
        runs.resume(latest["id"], db=db)
        return int(latest["id"])
    return runs.begin(desk_id, RECORD_KIND, signature=signature, db=db)


def run_replay(spec: ReplaySpec, engine: Callable[[ReplayCell], Mapping[str, Any]], *, db: Database | None = None) -> ReplayRun:
    """Run `engine` on every cell of `spec` that is not done yet. A cell whose engine raises (or returns no decision) is stored `failed` with its
    error and the others finish; calling again runs only the failed cells. The engine is the only thing that thinks: this function reads no price,
    sends no order and writes no lesson."""
    database = db or get_database()
    signature = spec.fingerprint()
    cells = spec.cells()
    known = _saved(database, signature)
    pending = [c for c in cells if known.get(c.key) is None or known[c.key].status != "done"]
    record_id = _open_record(database, signature, spec.desk_id)
    step = checkpoint.count(record_id, db=database)
    try:
        for cell in pending:
            try:
                result = CellResult(cell.day, cell.symbol, "done", _decision(engine(cell)))
            except Exception as exc:                              # noqa: BLE001 - a cell that raises is a failed cell, the others go on
                result = CellResult(cell.day, cell.symbol, "failed", None, f"{type(exc).__name__}: {exc}"[:MAX_ERROR])
            state = {"day": cell.day.isoformat(), "symbol": cell.symbol, "status": result.status, "decision": result.decision, "error": result.error}
            checkpoint.save(record_id, signature, step, f"cell{step}", state, (), db=database)
            step += 1
            known[cell.key] = result
    except BaseException:
        try:
            runs.interrupt(record_id, db=database)
        except runs.RunClosed:
            pass
        raise
    final = tuple(known[c.key] for c in cells)
    runs.finish(record_id, "done", outputs={"total": len(cells), "done": sum(1 for r in final if r.status == "done"),
                                            "failed": sum(1 for r in final if r.status == "failed"), "ran": len(pending)}, db=database)
    return ReplayRun(record_id, signature, final, len(pending), len(cells) - len(pending))


# ------------------------------------------------------------------------------------------------------------------------------ scoring
@dataclass(frozen=True)
class ReplayDecision:
    symbol: str
    day: date
    direction: str
    expiry: date | None = None                    # SWO: the expiry of the structure, which is the horizon of the decision


def decisions_of(run: ReplayRun) -> tuple[ReplayDecision, ...]:
    """The decisions of the finished cells of a run, ready to score."""
    return tuple(ReplayDecision(c.symbol, c.day, c.decision["direction"], date.fromisoformat(c.decision["expiry"]) if "expiry" in c.decision else None)
                 for c in run.cells if c.status == "done" and c.decision is not None)


@dataclass(frozen=True)
class DirectionScore:
    """Hit rate by direction. `rate` is hits over scored decisions; `hold` has no rate. The figures are always indicative: a replay is a small
    sample of one tribunal on past dates, never proof of an edge."""

    line: str
    horizon: str                                  # "N days" (SWC) or "expiry" (SWO)
    by_direction: Mapping[str, Mapping[str, Any]]
    sample_size: int                              # the buy and sell decisions that were scored
    unscored: tuple[tuple[str, date, str], ...]   # (symbol, day, reason): a decision with no real bar at its horizon is reported, not filled
    indicative: bool = field(default=True, init=False)

    def to_dict(self) -> dict[str, Any]:
        return {"line": self.line, "horizon": self.horizon, "indicative": self.indicative, "sample_size": self.sample_size,
                "by_direction": {k: dict(v) for k, v in self.by_direction.items()},
                "unscored": [{"symbol": s, "day": d.isoformat(), "reason": r} for s, d, r in self.unscored]}


def _end_price(line: str, bars: Sequence[tuple[date, Decimal]], decision: ReplayDecision, horizon_days: int | None) -> tuple[Decimal | None, Decimal | None, str]:
    """(entry, exit, reason): entry is the close of the decision day; exit is the close `horizon_days` bars later (SWC) or on the expiry (SWO)."""
    index = next((i for i, (d, _) in enumerate(bars) if d == decision.day), None)
    if index is None:
        return None, None, "no_bar_on_decision_day"
    entry = bars[index][1]
    if line == "SWC":
        if index + horizon_days >= len(bars):
            return entry, None, "no_bar_at_horizon"
        return entry, bars[index + horizon_days][1], ""
    if decision.expiry is None:
        return entry, None, "no_expiry"
    if decision.expiry <= decision.day:
        return entry, None, "expiry_not_after_decision"
    end = next((p for d, p in bars if d == decision.expiry), None)
    return (entry, end, "") if end is not None else (entry, None, "no_bar_at_expiry")


def score_directions(decisions: Iterable[ReplayDecision], prices: Mapping[str, Mapping[date, Decimal]], *, line: str,
                     horizon_days: int | None = None) -> DirectionScore:
    """Score the decisions of a replay by direction, each with the horizon of its line: SWC looks `horizon_days` bars ahead, SWO looks at the expiry
    of the structure. `prices` are real closes by symbol and date; a decision whose bar is missing is listed in `unscored`, never filled.
    A buy hits when the price is higher at the horizon, a sell when it is lower; an unchanged price is a miss. `hold` is counted and has no rate."""
    if line == "DT":
        raise ReplayError("replay_not_for_dt_bots", "a replay tests the tribunal of SWC and SWO desks, not a DT bot")
    if line not in REPLAY_LINES:
        raise ReplayError("bad_replay", f"a replay is scored on the lines {', '.join(REPLAY_LINES)}")
    if line == "SWC" and (isinstance(horizon_days, bool) or not isinstance(horizon_days, int) or not 1 <= horizon_days <= MAX_HORIZON_DAYS):
        raise ReplayError("bad_replay", f"an SWC score needs horizon_days from 1 to {MAX_HORIZON_DAYS}")
    series = {sym: sorted((d, Decimal(p)) for d, p in by_day.items()) for sym, by_day in prices.items()}
    counts = {d: {"decisions": 0, "scored": 0, "hits": 0} for d in DIRECTIONS}
    unscored: list[tuple[str, date, str]] = []
    for decision in decisions:
        if decision.direction not in DIRECTIONS:
            raise ReplayError("bad_replay", f"direction is one of {', '.join(DIRECTIONS)}")
        counts[decision.direction]["decisions"] += 1
        if decision.direction == "hold":
            continue
        entry, end, reason = _end_price(line, series.get(decision.symbol, ()), decision, horizon_days)
        if end is None or entry is None:
            unscored.append((decision.symbol, decision.day, reason))
            continue
        counts[decision.direction]["scored"] += 1
        change = end - entry
        counts[decision.direction]["hits"] += 1 if (change > 0 if decision.direction == "buy" else change < 0) else 0
    by: dict[str, dict[str, Any]] = {}
    for name in ("buy", "sell"):
        c = counts[name]
        by[name] = {**c, "rate": (Decimal(c["hits"]) / Decimal(c["scored"])).quantize(RATE_PLACES) if c["scored"] else None}
    by["hold"] = {"decisions": counts["hold"]["decisions"], "rate": None}                  # a hold makes no claim about the price: no rate
    return DirectionScore(line, f"{horizon_days} days" if line == "SWC" else "expiry", by, by["buy"]["scored"] + by["sell"]["scored"], tuple(unscored))


__all__ = ["CellResult", "DirectionScore", "ReplayCell", "ReplayDecision", "ReplayError", "ReplayRun", "ReplaySpec", "decisions_of", "run_replay",
           "score_directions"]
