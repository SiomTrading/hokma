"""Shared pieces of the trader tests (SPEC-019, A11): contract sheets, a chain with quotes, a context and a scripted model."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from hokma.contracts.identity import ContractSheet
from hokma.desks.agents.trader import BrokerCosts, ExitRules, Quote, TradeContext
from hokma.desks.trading.budget import BudgetInputs

D = Decimal
AS_OF = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
EXP = date(2026, 12, 18)
C200 = "OPT:AAPL:2026-12-18:C:200"
C210 = "OPT:AAPL:2026-12-18:C:210"
P190 = "OPT:AAPL:2026-12-18:P:190"
P180 = "OPT:AAPL:2026-12-18:P:180"
C220 = "OPT:AAPL:2026-12-18:C:220"
P170 = "OPT:AAPL:2026-12-18:P:170"
STOCK = "EQ:AAPL"
SPECS = {  # strike -> (bid, ask)
    C200: ("5.10", "5.20"), C210: ("1.80", "1.90"), C220: ("0.60", "0.70"), P190: ("2.40", "2.50"), P180: ("1.10", "1.20"),
    P170: ("0.40", "0.50"), STOCK: ("199.90", "200.00"),
}


def sheet(cid: str) -> ContractSheet:
    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    if cid == STOCK:
        return ContractSheet(cid, "equity", "AAPL", None, D(1), D("0.01"), D("0.01"), None, None, None, "USD", "test", now)
    parts = cid.split(":")
    return ContractSheet(cid, "option", "AAPL", "AAPL", D(100), D("0.01"), D("1"), date.fromisoformat(parts[2]), D(parts[4]), parts[3], "USD", "test", now)


def quotes() -> dict[str, Quote]:
    return {cid: Quote(D(bid), D(ask)) for cid, (bid, ask) in SPECS.items()}


def sheets() -> dict[str, ContractSheet]:
    return {cid: sheet(cid) for cid in SPECS}


def inputs(**kw) -> BudgetInputs:
    base = dict(equity=D(100_000), cash=D(100_000), capital=D(100_000), peak_equity=D(100_000), day_pnl=D(0), open_contracts=D(0), risk_pct=D(1))
    base.update(kw)
    return BudgetInputs(**base)


def context(**kw) -> TradeContext:
    base = dict(desk_id=7, underlying="AAPL", as_of=AS_OF, sheets=sheets(), quotes=quotes(), current_book={}, budget=inputs(),
                costs=BrokerCosts("per_share", D("0.65"), D(0), D(2)), rules=ExitRules(),
                verdict="Buy rating. Bullish view into the 2026-12-18 expiration, strikes 200 and 210 are the liquid ones.",
                reports={"options": "Implied volatility is rich at 31 percent; the 200 and 210 calls trade tight."})
    base.update(kw)
    return TradeContext(**base)


def answer_json(**kw) -> str:
    body = {"action": "propose", "structure": "debit_call_spread", "underlying": "AAPL", "expiration": "2026-12-18",
            "legs": [{"contract_id": C200, "side": "buy"}, {"contract_id": C210, "side": "sell"}], "stop_basis": "premium",
            "thesis": "The verdict is a buy with the 200 and 210 calls liquid; a debit spread limits the cost of rich volatility."}
    body.update(kw)
    return json.dumps({k: v for k, v in body.items() if v is not None})


class Reply:
    def __init__(self, text: str) -> None:
        self.text = text


class ScriptedModel:
    """A fake model: answers in order (the last repeats); records the messages it was asked."""

    def __init__(self, *answers: str) -> None:
        self.answers, self.calls = list(answers), []

    def complete(self, messages, *, schema=None, max_tokens=4096):
        self.calls.append(("complete", messages))
        return Reply(self.answers[min(len(self.calls) - 1, len(self.answers) - 1)])


class StreamingModel(ScriptedModel):
    def __init__(self, *answers: str, size: int = 7) -> None:
        super().__init__(*answers)
        self.size = size

    def stream(self, messages, *, schema=None, max_tokens=4096):
        self.calls.append(("stream", messages))
        text = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        for i in range(0, len(text), self.size):
            yield text[i:i + self.size]


def book(**kw):
    base = dict(equity=D(100_000), cash=D(100_000), peak_equity=D(100_000), day_pnl=D(0), open_contracts=D(0))
    base.update(kw)
    return SimpleNamespace(**base)
