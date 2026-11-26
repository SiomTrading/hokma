"""The result of a backtest run (SPEC-020, section 4.2). Statistics come from `hokma.quant.metrics` and `hokma.quant.validation`
and never from the engine (decision 1254). Amounts are Decimal; a trade carries its tag and context (B-14, B-25).

Money convention: `Trade.pnl_usd` is the result after `costs_usd` (commission, slippage, swap, impact), so every rate and ratio
computed from it is net. `mae_usd` is at most 0 and `mfe_usd` at least 0, both in USD with the contract multiplier.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from hokma.db import Database
from hokma.db.timeutil import from_db_time, to_db_time, utcnow
from hokma.quant import limits, metrics, risk, validation
from hokma.quant.metrics import drawdown as drawdown_metrics
from hokma.quant.metrics import trades as trade_metrics
from hokma.quant.types import QuantResult

ZERO = Decimal(0)
MAX_TAG = 80
MAX_CONTEXT = 64
MAX_ERROR = 2000
OPEN_STATUSES = ("queued", "running", "degraded")


class ResultError(ValueError):
    """The result cannot be built, saved or read. `code` is stable: bad_tag, bad_context, bad_fill, no_bars, bad_run, bad_curve, bad_stats."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _clean_tag(tag: str | None) -> str | None:
    """A tag is text up to 80 characters or None: an empty or blank tag is stored as None, never as an empty text."""
    if tag is None:
        return None
    if not isinstance(tag, str) or len(tag) > MAX_TAG:
        raise ResultError("bad_tag", f"a tag is text of at most {MAX_TAG} characters")
    return tag.strip() or None


def _clean_context(context: Mapping[str, Any]) -> dict[str, Any]:
    """Indicator values as plain JSON numbers, text or None, keys sorted; NaN and infinity become None."""
    if not isinstance(context, Mapping) or len(context) > MAX_CONTEXT:
        raise ResultError("bad_context", f"context is a mapping of at most {MAX_CONTEXT} values")
    out: dict[str, Any] = {}
    for key in sorted(context, key=str):
        value = context[key]
        if isinstance(value, bool) or value is None or isinstance(value, str):
            out[str(key)] = value
        elif isinstance(value, (int, float, Decimal)) or hasattr(value, "item"):
            number = float(value)
            out[str(key)] = number if math.isfinite(number) else None
        else:
            raise ResultError("bad_context", f"context value of {key!r} is not a number or text")
    return out


@dataclass(frozen=True)
class Trade:
    contract_id: str
    side: Literal["long", "short"]
    qty: Decimal
    entry_at: datetime
    entry_price: Decimal
    exit_at: datetime | None
    exit_price: Decimal | None
    pnl_usd: Decimal | None
    costs_usd: Decimal
    mae_usd: Decimal | None = None          # worst open loss while the trade lived, in USD (L-34)
    mfe_usd: Decimal | None = None          # best open gain while the trade lived, in USD (L-34)
    entry_tag: str | None = None            # B-14
    exit_tag: str | None = None
    context: Mapping[str, Any] = field(default_factory=dict)   # indicator values at entry (B-25)

    def __post_init__(self) -> None:
        object.__setattr__(self, "entry_tag", _clean_tag(self.entry_tag))
        object.__setattr__(self, "exit_tag", _clean_tag(self.exit_tag))
        object.__setattr__(self, "context", _clean_context(self.context))

    @property
    def tag(self) -> str | None:
        """The rule that originated the trade: the tag of its entry order (None when the order had no tag)."""
        return self.entry_tag


@dataclass(frozen=True)
class EquityCurve:
    """Account equity marked to market at every bar close."""
    timestamps: tuple[datetime, ...]
    equity_usd: tuple[Decimal, ...]


@dataclass(frozen=True)
class BacktestResult:
    run_id: int
    status: Literal["done", "failed", "account_blown"]
    spec_hash: str
    data_hash: str
    engine_version: str
    seed: int
    trades: tuple[Trade, ...]
    equity: EquityCurve
    realized_pnl_usd: Decimal
    mtm_pnl_usd: Decimal                    # both, always
    costs_usd: Mapping[str, Decimal]        # commission, slippage, swap, impact
    ambiguous_bars: int                     # bars where stop and target were both touched (B-07); 0 when none, never absent
    rejected: Mapping[str, int]             # margin, guard codes, reject_prob, intraday account rules
    stats: Mapping[str, QuantResult]        # from quant.metrics.summary; never from the engine (B-18)
    warnings: tuple[str, ...] = ()          # historical_margin_missing, below_realistic_expectation, costs_kill_edge, ...
    ambiguous_bar_dates: tuple[datetime, ...] = ()   # the bars counted in ambiguous_bars, for the judge to see


# --------------------------------------------------------------------------------------------------------------
# B-07: bars where stop and target were touched at once
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class BarRange:
    at: datetime
    high: Decimal
    low: Decimal


def ambiguous_bars(bars: Iterable[BarRange], *, side: Literal["long", "short"], stop: Decimal, target: Decimal) -> tuple[datetime, ...]:
    """Dates of the bars whose range reaches both the stop and the target: the order inside the bar is unknown, so each one is reported.

    A long has its stop below and its target above; a short the other way round. A stop on the wrong side of the target is an error.
    """
    if side not in ("long", "short"):
        raise ResultError("bad_fill", "side must be long or short")
    if (side == "long" and not stop < target) or (side == "short" and not stop > target):
        raise ResultError("bad_fill", "the stop must be on the losing side of the target")
    found = []
    for bar in bars:
        hit_stop = bar.low <= stop if side == "long" else bar.high >= stop
        hit_target = bar.high >= target if side == "long" else bar.low <= target
        if hit_stop and hit_target:
            found.append(bar.at)
    return tuple(found)


# --------------------------------------------------------------------------------------------------------------
# V-16: fills grouped into entry trades, exit trades and positions
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Fill:
    contract_id: str
    side: Literal["buy", "sell"]
    qty: Decimal
    price: Decimal
    at: datetime
    costs_usd: Decimal = ZERO
    tag: str | None = None
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Grouped:
    entry_trades: tuple[Trade, ...]     # one per entry fill, closed by the exits that were matched to it (first in, first out)
    exit_trades: tuple[Trade, ...]      # one per exit fill, with the average price of the entries it closed
    positions: tuple[Trade, ...]        # one per life of a position, from flat to flat


@dataclass
class _Lot:
    fill: Fill
    side: Literal["long", "short"]
    left: Decimal
    matches: list = field(default_factory=list)       # (closed qty, exit fill, share of the exit costs)


@dataclass
class _Position:
    lots: list = field(default_factory=list)
    closes: list = field(default_factory=list)        # (closed qty, exit fill, entry price of the lots it closed)


def _avg(parts: Sequence[tuple[Decimal, Decimal]]) -> Decimal:
    total = sum((q for q, _ in parts), ZERO)
    return sum((q * p for q, p in parts), ZERO) / total


def _window(bars: Sequence[BarRange], start: datetime, end: datetime | None) -> list[BarRange]:
    inside = [b for b in bars if b.at >= start and (end is None or b.at <= end)]
    if not inside:
        raise ResultError("no_bars", "a trade needs the bars it lived through: with none, MAE and MFE would be zero")
    return inside


def _build_trade(fills_side: str, contract: str, qty: Decimal, entry_price: Decimal, entry_at: datetime, exit_price: Decimal | None,
                 exit_at: datetime | None, costs: Decimal, bars: Sequence[BarRange], mult: Decimal, entry_tag: str | None,
                 exit_tag: str | None, context: Mapping[str, Any]) -> Trade:
    inside = _window(bars, entry_at, exit_at)
    mae, mfe = trade_metrics.mae_mfe(fills_side, entry_price, qty, mult, [b.high for b in inside], [b.low for b in inside])
    pnl = None
    if exit_price is not None:
        direction = Decimal(1) if fills_side == "long" else Decimal(-1)
        pnl = (exit_price - entry_price) * qty * mult * direction - costs
    return Trade(contract, fills_side, qty, entry_at, entry_price, exit_at, exit_price, pnl, costs, mae, mfe, entry_tag, exit_tag, context)


def group_trades(fills: Iterable[Fill], bars: Mapping[str, Sequence[BarRange]], multipliers: Mapping[str, Decimal]) -> Grouped:
    """Group the fills of a run in time order. Needs the bars and the multiplier of every contract: MAE and MFE are computed over the life of each
    group and are never left at zero (the grouping trap of L-36). A lot that is still open at the end becomes a trade with no exit."""
    by_contract: dict[str, list[Fill]] = {}
    for f in sorted(fills, key=lambda x: x.at):
        if f.qty <= 0 or f.price <= 0 or f.side not in ("buy", "sell"):
            raise ResultError("bad_fill", "a fill has a positive qty and price and a side buy or sell")
        by_contract.setdefault(f.contract_id, []).append(f)
    entries: list[Trade] = []
    exits: list[Trade] = []
    positions: list[Trade] = []
    for contract, items in by_contract.items():
        if contract not in bars or contract not in multipliers:
            raise ResultError("no_bars", f"no bars or multiplier for {contract}")
        mult, series = multipliers[contract], bars[contract]
        lots: deque[_Lot] = deque()
        all_lots: list[_Lot] = []
        position = _Position()
        closed_positions: list[_Position] = []
        for f in items:
            direction = "long" if f.side == "buy" else "short"
            remaining = f.qty
            while remaining > 0 and lots and lots[0].side != direction:
                lot = lots[0]
                take = min(lot.left, remaining)
                share = f.costs_usd * take / f.qty
                lot.matches.append((take, f, share))
                position.closes.append((take, f, lot.fill.price))
                lot.left -= take
                remaining -= take
                if lot.left == 0:
                    lots.popleft()
                if not lots:
                    closed_positions.append(position)
                    position = _Position()
            if remaining > 0:
                new = _Lot(f, direction, remaining)
                if remaining != f.qty:                        # the part that crossed through zero opens the other side
                    new.fill = replace(f, qty=remaining, costs_usd=f.costs_usd * remaining / f.qty)
                lots.append(new)
                all_lots.append(new)
                position.lots.append(new)
        if position.lots:
            closed_positions.append(position)
        for lot in all_lots:
            entry_qty = lot.fill.qty
            closed = sum((q for q, _, _ in lot.matches), ZERO)
            entry_costs = lot.fill.costs_usd
            if lot.matches:
                exit_price = _avg([(q, f.price) for q, f, _ in lot.matches]) if closed == entry_qty else None
                last_exit = lot.matches[-1][1]
                costs = entry_costs + sum((s for _, _, s in lot.matches), ZERO)
                exit_at, tag_out = (last_exit.at, last_exit.tag) if closed == entry_qty else (None, None)
            else:
                exit_price, exit_at, tag_out, costs = None, None, None, entry_costs
            entries.append(_build_trade(lot.side, contract, entry_qty, lot.fill.price, lot.fill.at, exit_price, exit_at, costs, series, mult,
                                        lot.fill.tag, tag_out, lot.fill.context))
        for pos in closed_positions:
            first_lot = pos.lots[0]
            entry_qty = sum((lot.fill.qty for lot in pos.lots), ZERO)
            entry_price = _avg([(lot.fill.qty, lot.fill.price) for lot in pos.lots])
            entry_costs = sum((lot.fill.costs_usd for lot in pos.lots), ZERO)
            closed = sum((q for q, _, _ in pos.closes), ZERO)
            done = closed == entry_qty
            exit_costs = sum((f.costs_usd * q / f.qty for q, f, _ in pos.closes), ZERO)
            exit_price = _avg([(q, f.price) for q, f, _ in pos.closes]) if pos.closes and done else None
            last = pos.closes[-1][1] if pos.closes and done else None
            positions.append(_build_trade(first_lot.side, contract, entry_qty, entry_price, first_lot.fill.at, exit_price,
                                          None if last is None else last.at, entry_costs + exit_costs, series, mult, first_lot.fill.tag,
                                          None if last is None else last.tag, first_lot.fill.context))
            seen: dict[int, tuple[Decimal, Fill, list]] = {}
            for q, f, price in pos.closes:
                key = id(f)
                if key in seen:
                    seen[key][2].append((q, price))
                    seen[key] = (seen[key][0] + q, f, seen[key][2])
                else:
                    seen[key] = (q, f, [(q, price)])
            for q, f, parts in seen.values():
                entry_avg = _avg(parts)
                matched = [lot for lot in pos.lots if any(m[1] is f for m in lot.matches)]
                share_entry = sum((lot.fill.costs_usd * sum((mq for mq, mf, _ in lot.matches if mf is f), ZERO) / lot.fill.qty for lot in matched), ZERO)
                first = min(matched, key=lambda lot: lot.fill.at)
                exits.append(_build_trade(first.side, contract, q, entry_avg, first.fill.at, f.price, f.at,
                                          share_entry + f.costs_usd * q / f.qty, series, mult, first.fill.tag, f.tag, first.fill.context))
    return Grouped(tuple(entries), tuple(exits), tuple(positions))


# --------------------------------------------------------------------------------------------------------------
# V-34, N-21: gross and net exposure, cash and value of the account
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Holding:
    contract_id: str
    qty: Decimal                # signed: long positive, short negative
    price: Decimal
    multiplier: Decimal


@dataclass(frozen=True)
class Exposure:
    at: datetime | None
    gross_usd: Decimal          # sum of the absolute notionals (qty * price * multiplier)
    net_usd: Decimal            # long notionals minus short notionals
    cash_usd: Decimal           # the cash of the engine book, not recomputed here
    value_usd: Decimal          # account equity at the bar close
    by_contract: Mapping[str, Decimal]

    @property
    def leverage(self) -> Decimal | None:
        return None if self.value_usd <= 0 else self.gross_usd / self.value_usd


def exposure(holdings: Iterable[Holding], *, cash_usd: Decimal, equity_usd: Decimal, at: datetime | None = None) -> Exposure:
    """Exposure of the positions at one bar. Notional is qty * price * multiplier, so 1 NQ at 20000 is 400000. Cash and value are taken from the
    engine book as they are."""
    notionals: dict[str, Decimal] = {}
    for h in holdings:
        notionals[h.contract_id] = notionals.get(h.contract_id, ZERO) + h.qty * h.price * h.multiplier
    return Exposure(at, sum((abs(v) for v in notionals.values()), ZERO), sum(notionals.values(), ZERO), cash_usd, equity_usd, notionals)


def cash_mismatches(series: Sequence[Exposure], engine_cash: Mapping[datetime, Decimal]) -> tuple[datetime, ...]:
    """Bars where the cash of the exposure differs from the cash of the engine book (or the engine has no cash for the bar)."""
    return tuple(e.at for e in series if e.at is None or engine_cash.get(e.at) != e.cash_usd)


# --------------------------------------------------------------------------------------------------------------
# B-18, V-19, V-21, B-20, L-33: the table of metrics of a run
# --------------------------------------------------------------------------------------------------------------
def daily_equity(curve: EquityCurve) -> pd.Series:
    """Equity at the last bar of every calendar day, as floats on a daily index."""
    if len(curve.timestamps) != len(curve.equity_usd) or not curve.timestamps:
        raise ResultError("bad_curve", "the equity curve needs the same number of dates and values, at least one")
    index = pd.DatetimeIndex([pd.Timestamp(t).tz_localize(None) if pd.Timestamp(t).tzinfo is None else pd.Timestamp(t).tz_convert("UTC").tz_localize(None)
                              for t in curve.timestamps])
    series = pd.Series([float(v) for v in curve.equity_usd], index=index).sort_index()
    return series.groupby(series.index.normalize()).last()


def equity_returns(curve: EquityCurve) -> pd.Series:
    """Daily returns of the equity curve. The first day is 0, so a drawdown that starts on day one is measured from the opening equity."""
    equity = daily_equity(curve)
    returns = equity.pct_change()
    returns.iloc[0] = 0.0
    return returns


def trade_rows(trades: Iterable[Trade], multipliers: Mapping[str, Decimal] | None = None) -> list[dict[str, Any]]:
    """Closed trades as plain rows for `metrics.trade_stats` (Decimal to float: statistics are not money)."""
    rows = []
    for t in trades:
        if t.pnl_usd is None:
            continue
        rows.append({"pnl_usd": float(t.pnl_usd), "entry_at": t.entry_at, "exit_at": t.exit_at, "entry_price": float(t.entry_price),
                     "qty": float(t.qty), "multiplier": float((multipliers or {}).get(t.contract_id, Decimal(1))), "costs_usd": float(t.costs_usd),
                     "mae_usd": None if t.mae_usd is None else float(t.mae_usd), "mfe_usd": None if t.mfe_usd is None else float(t.mfe_usd)})
    return rows


def summary(result: BacktestResult, *, rf: float = 0.0, periods_per_year: int = 252, multipliers: Mapping[str, Decimal] | None = None,
            n_trials: int | None = None, sr_std: float | None = None, var_level: float = 0.95) -> dict[str, QuantResult]:
    """The table of metrics of a run, every figure computed by `hokma.quant` and none by the engine.

    Blocks of `quant.metrics.summary` keyed by name, then `sharpe`, `sortino`, `calmar` and `cagr_by_date` from `quant.metrics.compute`, `var` from
    `quant.risk.var`, `drawdown_duration`, the trade level blocks (`trade_stats`, `sqn`, `streaks`, `kelly`) and, when `n_trials` (the
    count of the trial ledger) and `sr_std` are given, `dsr` from `quant.validation.dsr`. Annualize with
    `quant.metrics.returns.bars_per_year`, never a fixed 252 for intraday series.
    """
    returns = equity_returns(result.equity)
    out: dict[str, QuantResult] = dict(metrics.summary(returns, rf=rf, periods_per_year=periods_per_year))
    out.update(metrics.compute(returns, ["sharpe", "sortino", "calmar", "cagr_by_date"], rf=rf, periods_per_year=periods_per_year))
    out["var"] = risk.var(returns, level=var_level)
    out["drawdown_duration"] = drawdown_metrics.drawdown_duration(returns)
    rows = trade_rows(result.trades, multipliers)
    if rows:
        pnl = pd.Series([r["pnl_usd"] for r in rows])
        out["trade_stats"] = trade_metrics.trade_stats(rows)
        out["sqn"] = trade_metrics.sqn(pnl)
        out["streaks"] = trade_metrics.streaks(pnl)
        out["kelly"] = trade_metrics.kelly(pnl)
    if n_trials is not None:
        limits.check_param("n_trials", n_trials)
        if sr_std is None:
            raise ResultError("bad_stats", "the deflated Sharpe ratio needs sr_std together with n_trials")
        out["dsr"] = validation.dsr(returns, n_trials=n_trials, sr_std=sr_std)
    return out


def report_html(result: BacktestResult, *, title: str, benchmark: Any = None) -> str:
    """The full report of the run: the HTML of `quant.metrics.report_html` over the daily equity returns, with no external resource."""
    return metrics.report_html(equity_returns(result.equity), benchmark=benchmark, title=str(title)[:120])


def with_stats(result: BacktestResult, **kwargs: Any) -> BacktestResult:
    """The same result with its `stats` filled by `summary`."""
    return replace(result, stats=summary(result, **kwargs))


# --------------------------------------------------------------------------------------------------------------
# N-26: JSON of the result, and back
# --------------------------------------------------------------------------------------------------------------
def _dt(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _undt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _dec(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _undec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _trade_dict(t: Trade) -> dict[str, Any]:
    return {"contract_id": t.contract_id, "side": t.side, "qty": _dec(t.qty), "entry_at": _dt(t.entry_at), "entry_price": _dec(t.entry_price),
            "exit_at": _dt(t.exit_at), "exit_price": _dec(t.exit_price), "pnl_usd": _dec(t.pnl_usd), "costs_usd": _dec(t.costs_usd),
            "mae_usd": _dec(t.mae_usd), "mfe_usd": _dec(t.mfe_usd), "entry_tag": t.entry_tag, "exit_tag": t.exit_tag, "context": dict(t.context)}


def _trade_from(d: Mapping[str, Any]) -> Trade:
    return Trade(d["contract_id"], d["side"], Decimal(d["qty"]), _undt(d["entry_at"]), Decimal(d["entry_price"]), _undt(d["exit_at"]),
                 _undec(d["exit_price"]), _undec(d["pnl_usd"]), Decimal(d["costs_usd"]), _undec(d["mae_usd"]), _undec(d["mfe_usd"]),
                 d["entry_tag"], d["exit_tag"], d["context"])


def _quant_from(d: Mapping[str, Any]) -> QuantResult:
    return QuantResult(name=d["name"], value=d["value"], unit=d["unit"], method=d["method"], params=dict(d["params"]), inputs_hash=d["inputs_hash"],
                       as_of=_undt(d["as_of"]), warnings=tuple(d["warnings"]), piece_ids=tuple(d["piece_ids"]))


def to_dict(result: BacktestResult) -> dict[str, Any]:
    """Plain JSON values: Decimal and dates as text. `ambiguous_bars`, the hashes, the engine version and the seed are always present."""
    return {
        "run_id": result.run_id, "status": result.status, "spec_hash": result.spec_hash, "data_hash": result.data_hash,
        "engine_version": result.engine_version, "seed": result.seed, "trades": [_trade_dict(t) for t in result.trades],
        "equity": {"timestamps": [_dt(t) for t in result.equity.timestamps], "equity_usd": [_dec(v) for v in result.equity.equity_usd]},
        "realized_pnl_usd": _dec(result.realized_pnl_usd), "mtm_pnl_usd": _dec(result.mtm_pnl_usd),
        "costs_usd": {k: _dec(v) for k, v in result.costs_usd.items()}, "ambiguous_bars": result.ambiguous_bars,
        "ambiguous_bar_dates": [_dt(t) for t in result.ambiguous_bar_dates], "rejected": dict(result.rejected),
        "stats": {k: v.to_dict() for k, v in result.stats.items()}, "warnings": list(result.warnings)}


def from_dict(d: Mapping[str, Any]) -> BacktestResult:
    return BacktestResult(
        d["run_id"], d["status"], d["spec_hash"], d["data_hash"], d["engine_version"], d["seed"], tuple(_trade_from(t) for t in d["trades"]),
        EquityCurve(tuple(_undt(t) for t in d["equity"]["timestamps"]), tuple(Decimal(v) for v in d["equity"]["equity_usd"])),
        Decimal(d["realized_pnl_usd"]), Decimal(d["mtm_pnl_usd"]), {k: Decimal(v) for k, v in d["costs_usd"].items()}, d["ambiguous_bars"],
        dict(d["rejected"]), {k: _quant_from(v) for k, v in d["stats"].items()}, tuple(d["warnings"]),
        tuple(_undt(t) for t in d["ambiguous_bar_dates"]))


def to_json(result: BacktestResult) -> str:
    return json.dumps(to_dict(result), sort_keys=True, separators=(",", ":"), allow_nan=False)


def from_json(text: str) -> BacktestResult:
    return from_dict(json.loads(text))


# --------------------------------------------------------------------------------------------------------------
# L-40, V-15, P-11: what a run stores and when
#   done or account_blown: one row in backtest_runs updated with its summary, one row per trade in backtest_trades and the equity curve in a
#   file whose sha-256 is stored with its name. failed: the error and no trade at all. Everything is checked and written in one transaction.
# --------------------------------------------------------------------------------------------------------------
def _curve_bytes(result: BacktestResult) -> bytes:
    body = {"run_id": result.run_id, "spec_hash": result.spec_hash, "data_hash": result.data_hash,
            "timestamps": [_dt(t) for t in result.equity.timestamps], "equity_usd": [_dec(v) for v in result.equity.equity_usd]}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _curve_name(run_id: int) -> str:
    return f"curve-{int(run_id)}.json"


def _write_file(folder: Path, name: str, data: bytes) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".{name}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, folder / name)


def _open_run(session: Any, run_id: int) -> dict[str, Any]:
    row = session.one("SELECT * FROM backtest_runs WHERE id = :i", {"i": int(run_id)})
    if row is None:
        raise ResultError("bad_run", f"run {run_id} does not exist")
    if row["status"] not in OPEN_STATUSES:
        raise ResultError("bad_run", f"run {run_id} is already {row['status']}: a finished run is not written twice")
    return row


def _summary_json(result: BacktestResult) -> str:
    body = to_dict(result)
    for key in ("trades", "equity"):
        body.pop(key)
    return json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)


def record_result(db: Database, result: BacktestResult, *, curve_dir: Path) -> str:
    """Save a finished run and return the hash of its curve file. `curve_dir` is set by the service, never by an agent; the file name comes from
    the run id. A failed result is refused here: use `record_failure`."""
    if result.status not in ("done", "account_blown"):
        raise ResultError("bad_run", "only a done or account_blown result is saved with its trades; a failure goes through record_failure")
    data = _curve_bytes(result)
    digest = hashlib.sha256(data).hexdigest()
    name = _curve_name(result.run_id)
    wrote = False
    try:
        with db.write() as u:
            s = u.session
            row = _open_run(s, result.run_id)
            if row["spec_hash"] != result.spec_hash or int(row["seed"]) != int(result.seed):
                raise ResultError("bad_run", "the result does not belong to this run: spec hash or seed differ")
            _write_file(Path(curve_dir), name, data)
            wrote = True
            for t in result.trades:
                s.insert("backtest_trades", {
                    "run_id": result.run_id, "contract_id": t.contract_id, "side": t.side, "qty": _dec(t.qty), "entry_at": to_db_time(t.entry_at),
                    "entry_price": _dec(t.entry_price), "exit_at": to_db_time(t.exit_at), "exit_price": _dec(t.exit_price), "pnl_usd": _dec(t.pnl_usd),
                    "costs_usd": _dec(t.costs_usd), "mae_usd": _dec(t.mae_usd), "mfe_usd": _dec(t.mfe_usd), "entry_tag": t.entry_tag,
                    "exit_tag": t.exit_tag, "context_json": json.dumps(dict(t.context), sort_keys=True, allow_nan=False)})
            s.update("backtest_runs", {"id": result.run_id}, {
                "status": result.status, "data_hash": result.data_hash, "engine_version": result.engine_version, "summary_json": _summary_json(result),
                "result_path": name, "result_hash": digest, "error": None, "finished_at": utcnow()})
    except BaseException:
        if wrote:
            (Path(curve_dir) / name).unlink(missing_ok=True)
        raise
    return digest


def record_failure(db: Database, run_id: int, error: str) -> None:
    """Close a run as failed with its error. No trade of a failed run exists: a half written run is never left behind."""
    with db.write() as u:
        s = u.session
        _open_run(s, run_id)
        s.execute("DELETE FROM backtest_trades WHERE run_id = :i", {"i": int(run_id)})
        s.update("backtest_runs", {"id": int(run_id)}, {"status": "failed", "error": str(error)[:MAX_ERROR], "finished_at": utcnow()})


def load_trades(db: Database, run_id: int) -> tuple[Trade, ...]:
    with db.read() as u:
        rows = u.session.query("SELECT * FROM backtest_trades WHERE run_id = :i ORDER BY id", {"i": int(run_id)})
    return tuple(Trade(
        r["contract_id"], r["side"], Decimal(r["qty"]), from_db_time(str(r["entry_at"])), Decimal(r["entry_price"]),
        None if r["exit_at"] is None else from_db_time(str(r["exit_at"])), _undec(r["exit_price"]), _undec(r["pnl_usd"]), Decimal(r["costs_usd"]),
        _undec(r["mae_usd"]), _undec(r["mfe_usd"]), r["entry_tag"], r["exit_tag"], json.loads(r["context_json"] or "{}")) for r in rows)


def load_curve(db: Database, run_id: int, *, curve_dir: Path) -> EquityCurve:
    """The curve of a saved run, read back only if the file still has the hash that was stored with it."""
    with db.read() as u:
        row = u.session.one("SELECT result_path, result_hash FROM backtest_runs WHERE id = :i", {"i": int(run_id)})
    if row is None or not row["result_path"] or not row["result_hash"] or row["result_path"] != _curve_name(run_id):
        raise ResultError("bad_curve", f"run {run_id} has no curve file")
    data = (Path(curve_dir) / row["result_path"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != row["result_hash"]:
        raise ResultError("bad_curve", f"the curve file of run {run_id} does not match its stored hash")
    body = json.loads(data)
    return EquityCurve(tuple(datetime.fromisoformat(t) for t in body["timestamps"]), tuple(Decimal(v) for v in body["equity_usd"]))
