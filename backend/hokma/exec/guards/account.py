"""The account guard (SPEC-018: DTS-06): daily loss, drawdown from the peak of the book, capital floor and the rules of a funded
account. PASS or BLOCK, by code. It blocks openings only: a closing order is never stopped here (applies_to_closing is False).

Facts come from the snapshot of the book. A fact that is None (unknown) while a limit that needs it is on blocks the opening
with `book_unavailable`: the guard never reads a missing number as zero."""
from __future__ import annotations

from decimal import Decimal

from hokma.exec.guards import GuardContext, GuardResult, base_of, blocked, passed
from hokma.exec.money import ZERO


def _d(value) -> Decimal:
    return Decimal(str(value))


class AccountGuard:
    name = "account"
    applies_to_closing = False

    def check(self, ctx: GuardContext) -> GuardResult:
        book, limits = ctx.book, ctx.limits
        funding = limits.get("funding") or {}
        numbers: dict[str, object] = {}
        base = base_of(ctx)

        loss_usd = funding.get("daily_loss_usd")
        loss_pct = limits.get("max_daily_loss_pct")
        if loss_usd is not None or loss_pct is not None:
            if book.day_pnl is None:
                return blocked(self.name, "book_unavailable", "the day result of the account is unknown, so a daily loss limit cannot be checked")
            numbers["day_pnl"] = book.day_pnl
            if loss_usd is not None and book.day_pnl <= -_d(loss_usd):
                return blocked(self.name, "daily_loss", f"the loss of the day is {-book.day_pnl:,.2f} and the funded account allows {_d(loss_usd):,.2f}",
                               day_pnl=book.day_pnl, daily_loss_usd=loss_usd)
            if loss_pct is not None and base is not None and book.day_pnl <= -(base * _d(loss_pct) / Decimal(100)):
                return blocked(self.name, "daily_loss_pct", f"the loss of the day passed {loss_pct}% of {base:,.2f}", day_pnl=book.day_pnl,
                               max_daily_loss_pct=loss_pct)

        dd_pct, dd_usd = limits.get("max_drawdown_pct"), funding.get("trailing_drawdown_usd")
        if dd_pct is not None or dd_usd is not None:
            if book.equity is None or book.peak_equity is None:
                return blocked(self.name, "book_unavailable", "the equity or its peak is unknown, so the drawdown cannot be checked")
            drop = book.peak_equity - book.equity
            numbers["drawdown_usd"] = drop
            if dd_pct is not None and book.peak_equity > 0 and drop / book.peak_equity * Decimal(100) > _d(dd_pct):
                return blocked(self.name, "max_drawdown_pct", f"the account is {drop / book.peak_equity * 100:.1f}% below its peak; the limit is {dd_pct}%",
                               drawdown_pct=drop / book.peak_equity * 100, max_drawdown_pct=dd_pct)
            if dd_usd is not None and drop >= _d(dd_usd):
                return blocked(self.name, "trailing_drawdown", f"the account is {drop:,.2f} below its peak; the funded account allows {_d(dd_usd):,.2f}",
                               drawdown_usd=drop, trailing_drawdown_usd=dd_usd)

        floor = limits.get("capital_floor_usd")
        if floor is not None:
            if book.equity is None:
                return blocked(self.name, "book_unavailable", "the equity is unknown, so the capital floor cannot be checked")
            if book.equity < _d(floor):
                return blocked(self.name, "capital_floor", f"the equity {book.equity:,.2f} is under the floor of {_d(floor):,.2f}",
                               equity=book.equity, capital_floor_usd=floor)

        max_contracts = funding.get("max_contracts")
        if max_contracts is not None and book.open_contracts is not None and book.open_contracts >= _d(max_contracts):
            return blocked(self.name, "funding_max_contracts", f"the account already holds {book.open_contracts} contracts; the funded account allows {max_contracts}",
                           open_contracts=book.open_contracts, max_contracts=max_contracts)

        consistency = funding.get("consistency_pct")
        if consistency is not None and book.total_profit is not None and book.day_pnl is not None and book.total_profit > ZERO and book.day_pnl > ZERO:
            share = book.day_pnl / book.total_profit * Decimal(100)
            numbers["day_share_pct"] = share
            if share > _d(consistency):
                return blocked(self.name, "consistency", f"today is {share:.1f}% of all the profit; the consistency rule allows {consistency}%",
                               day_share_pct=share, consistency_pct=consistency)
        return passed(self.name, **numbers)
