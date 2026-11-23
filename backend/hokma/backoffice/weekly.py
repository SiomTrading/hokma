"""Weekly digest of closed theses (SPEC-018: CTS-025). Descriptive only: it reports what closed in the week, it recommends nothing.

* The week is chosen by the date of the exit. Headline numbers use closed theses only, net of costs.
* A closed thesis carries its own cumulative result (its partial trims are already inside it). The partial trims of theses that are still open
  are listed apart, with their own total, and are never added to the headline numbers.
* R is the net result over the initial risk of the stop: the distance from entry to stop, times the quantity, times the multiplier of the
  contract (futures and options have one). When that risk is not positive (a gap through the stop, a stop moved to or past the entry) the thesis
  has no R and stays out of the R averages; it stays in every other number.
* MAE is clamped to zero or below and MFE to zero or above, as percentages of the entry.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from hokma.exec.money import ZERO, MoneyError, dec, dec_or_none, text

DIMENSIONS = ("source", "exit_reason", "thesis_type", "sector", "mechanism")
MAX_THESES = 50_000
UNKNOWN = "unknown"


class DigestError(ValueError):
    """A thesis record that cannot be used."""


@dataclass(frozen=True)
class ClosedThesis:
    thesis_id: str
    closed_on: str                      # date of the exit
    side: str                           # long or short
    entry_price: Decimal
    stop_price: Decimal | None
    qty: Decimal
    multiplier: Decimal                 # 1 for shares; the contract multiplier for futures and options
    gross_pnl: Decimal
    costs: Decimal = ZERO               # commissions and fees paid, positive
    mae_pct: Decimal | None = None
    mfe_pct: Decimal | None = None
    holding_days: int | None = None
    source: str | None = None
    exit_reason: str | None = None
    thesis_type: str | None = None
    sector: str | None = None
    mechanism: str | None = None

    @property
    def net_pnl(self) -> Decimal:
        return self.gross_pnl - self.costs

    @property
    def initial_risk(self) -> Decimal | None:
        """Positive money at risk at the entry, or None when the stop gives no positive risk."""
        if self.stop_price is None:
            return None
        distance = (self.entry_price - self.stop_price) if self.side == "long" else (self.stop_price - self.entry_price)
        risk = distance * self.qty * self.multiplier
        return risk if risk > 0 else None

    @property
    def r_multiple(self) -> Decimal | None:
        risk = self.initial_risk
        return None if risk is None else self.net_pnl / risk


@dataclass(frozen=True)
class OpenThesis:
    thesis_id: str
    trims: tuple[tuple[str, Decimal], ...] = ()     # (date, realized result of the partial close)


def thesis_from_row(row: Mapping[str, Any]) -> ClosedThesis:
    """A closed thesis from a dict. The side, the entry, the quantity and the multiplier must be there; nothing is guessed."""
    try:
        side = str(row["side"]).lower()
        if side not in ("long", "short"):
            raise DigestError("side must be long or short")
        qty, mult = dec(row["qty"], "qty"), dec(row.get("multiplier", 1), "multiplier")
        if qty <= 0 or mult <= 0:
            raise DigestError("qty and multiplier must be positive")
        mae, mfe = dec_or_none(row.get("mae_pct")), dec_or_none(row.get("mfe_pct"))
        return ClosedThesis(
            str(row["thesis_id"])[:80], str(row["closed_on"])[:10], side, dec(row["entry_price"], "entry_price"), dec_or_none(row.get("stop_price")), qty, mult,
            dec(row["gross_pnl"], "gross_pnl"), dec(row.get("costs", 0), "costs"), None if mae is None else min(mae, ZERO), None if mfe is None else max(mfe, ZERO),
            row.get("holding_days"), *(row.get(k) for k in DIMENSIONS))
    except KeyError as exc:
        raise DigestError(f"missing field {exc.args[0]}") from exc
    except MoneyError as exc:
        raise DigestError(str(exc)) from exc


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return sum(values, ZERO) / len(values) if values else None


def _stdev(values: Sequence[Decimal]) -> Decimal:
    """Sample standard deviation; zero for fewer than two values."""
    if len(values) < 2:
        return ZERO
    mean = sum(values, ZERO) / len(values)
    return (sum(((v - mean) ** 2 for v in values), ZERO) / (len(values) - 1)).sqrt()


@dataclass(frozen=True)
class Digest:
    start: str
    end: str
    total: int
    winners: int
    losers: int
    breakeven: int
    win_rate: Decimal | None
    expectancy: Decimal | None
    profit_factor: Decimal | None
    net_pnl: Decimal
    costs: Decimal
    avg_winner: Decimal | None
    avg_loser: Decimal | None
    r_average: Decimal | None
    r_stdev: Decimal | None
    r_count: int
    avg_mae_pct: Decimal | None
    avg_mfe_pct: Decimal | None
    patterns: dict[str, dict[str, dict[str, int]]]
    partial_trims: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        t = text
        return {"period": {"from": self.start, "to": self.end}, "total": self.total, "winners": self.winners, "losers": self.losers, "breakeven": self.breakeven,
                "win_rate": t(self.win_rate), "expectancy": t(self.expectancy), "profit_factor": t(self.profit_factor), "net_pnl": t(self.net_pnl),
                "costs": t(self.costs), "avg_winner": t(self.avg_winner), "avg_loser": t(self.avg_loser), "r_average": t(self.r_average), "r_stdev": t(self.r_stdev),
                "r_count": self.r_count, "avg_mae_pct": t(self.avg_mae_pct), "avg_mfe_pct": t(self.avg_mfe_pct), "patterns": self.patterns,
                "partial_trims": self.partial_trims}


def _day(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)[:10]


def build_digest(closed: Iterable[ClosedThesis], open_theses: Iterable[OpenThesis] = (), *, start: date | str, end: date | str) -> Digest:
    s, e = _day(start), _day(end)
    if e < s:
        raise DigestError("the end of the week is before its start")
    week = [t for t in closed if s <= t.closed_on <= e]
    if len(week) > MAX_THESES:
        raise DigestError(f"more than {MAX_THESES} theses in one digest")
    nets = [t.net_pnl for t in week]
    wins, losses = [n for n in nets if n > 0], [n for n in nets if n < 0]
    gross_loss = abs(sum(losses, ZERO))
    r_values = [r for r in (t.r_multiple for t in week) if r is not None]
    patterns: dict[str, dict[str, dict[str, int]]] = {}
    for dim in DIMENSIONS:
        bucket: dict[str, dict[str, int]] = defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0})
        for t in week:
            row = bucket[getattr(t, dim) or UNKNOWN]
            row["total"] += 1
            row["wins"] += t.net_pnl > 0
            row["losses"] += t.net_pnl < 0
        patterns[dim] = {k: dict(v) for k, v in sorted(bucket.items())}
    trims = [(o.thesis_id, d, amount) for o in open_theses for d, amount in o.trims if s <= str(d)[:10] <= e]
    partial = {"count": len(trims), "total_realized": text(sum((a for _, _, a in trims), ZERO)),
               "trims": [{"thesis_id": i, "date": str(d)[:10], "realized": text(a)} for i, d, a in trims],
               "note": "partial trims of open theses are not part of the totals above"}
    return Digest(
        s, e, len(week), len(wins), len(losses), len(week) - len(wins) - len(losses), Decimal(len(wins)) / len(week) if week else None, _mean(nets),
        (sum(wins, ZERO) / gross_loss) if gross_loss else None, sum(nets, ZERO), sum((t.costs for t in week), ZERO), _mean(wins), _mean(losses),
        _mean(r_values), _stdev(r_values) if r_values else None, len(r_values), _mean([t.mae_pct for t in week if t.mae_pct is not None]),
        _mean([t.mfe_pct for t in week if t.mfe_pct is not None]), patterns, partial)


__all__ = ["ClosedThesis", "Digest", "DigestError", "OpenThesis", "build_digest", "thesis_from_row"]
