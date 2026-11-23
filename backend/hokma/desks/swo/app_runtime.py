"""The runtime of the SWO cycle in the app (SPEC-019, B13): the real models, the real data layer and the quotes of the chain.

`graph.run_cycle` receives everything it needs in a `CycleRuntime` and reaches nothing on its own. This module builds it for a desk from the
pieces the app already has: the model clients of `hokma.llm` (one per seat, on the tier the desk gave the agent), the data layer of `hokma.data`
(bars, point in time fundamentals, the verified snapshot), the connected chain of `hokma.market.options` and the contract sheets of
`hokma.contracts`. The account, the equity and the positions come from the back office. A source that is not ready stops the cycle with a
classified `CycleError` (`no_data`): nothing is made up, and no price or sheet is typed here.

Nothing in this module sends an order or calls a broker to trade: the quotes are read-only and the only way out of a cycle is `exec.submit`.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping

import pandas as pd

from hokma.backoffice import snapshot as account_snapshot
from hokma.broker.types import Fill
from hokma.db import get_database
from hokma.desks.agents.investors import CompanyData
from hokma.desks.agents.trader import BrokerCosts, Quote, TradeContext
from hokma.desks.core.graph import ChatModel
from hokma.desks.plantel import SEATS
from hokma.desks.swo.create import desk_agent_slug
from hokma.desks.swo.graph import AnalystInput, ClientChat, CycleData, CycleError, CycleRuntime
from hokma.desks.trading.budget import BudgetInputs

BENCHMARK = "SPY"
BARS_DAYS = 400
CHAIN_LIMIT = 60
CHAIN_MIN_DAYS, CHAIN_MAX_DAYS = 14, 75
# The second reviewer of the gate is a seat of another tier than the judge, so the two reviews do not run on one model.
REVIEW_SEATS = {"A10:review1": "A10", "A10:review2": "A13"}


def _slug(desk_id: int, code: str) -> str:
    seat = SEATS[REVIEW_SEATS.get(code, code)]
    return desk_agent_slug(seat, desk_id)


def _client(desk_id: int, code: str) -> Any:
    from hokma.llm.factory import make_llm

    return make_llm(_slug(desk_id, code), desk_id=desk_id)


def _need(result: Any, what: str) -> Any:
    """The value of a `DataResult`; anything that is not ok stops the cycle with its reason."""
    if not result.ok:
        raise CycleError("no_data", f"{what}: {result.status}: {result.reason}")
    return result.value


def _statements(records: Any) -> pd.DataFrame:
    """The point in time frame the investors read: one row per period end, a `filed` column, numeric accounts."""
    frame = pd.DataFrame(list(records))
    if frame.empty or "report_period" not in frame or "filing_date" not in frame:
        raise CycleError("no_data", "fundamentals: the provider gave no dated statements")
    frame = frame.dropna(subset=["report_period", "filing_date"]).copy()
    frame["filed"] = frame.pop("filing_date").astype(str).str[:10]
    frame = frame.set_index(frame.pop("report_period").astype(str).str[:10]).sort_index()
    numeric = [c for c in frame.columns if c != "filed" and pd.api.types.is_numeric_dtype(frame[c])]
    if not numeric:
        raise CycleError("no_data", "fundamentals: the provider gave no numeric accounts")
    return frame[[*numeric, "filed"]]


def load(desk_id: int, symbol: str, as_of: datetime) -> CycleData:
    """The data of one symbol at one cut-off: bars of the symbol and of the benchmark, point in time fundamentals and the verified snapshot."""
    from hokma import data
    from hokma.data import fundamentals_src
    from hokma.data import snapshot as snapshots

    start = as_of - timedelta(days=BARS_DAYS)
    bars = _need(data.bars(symbol, "1D", start=start, end=as_of, as_of=as_of), f"bars of {symbol}")
    benchmark = _need(data.bars(BENCHMARK, "1D", start=start, end=as_of, as_of=as_of), f"bars of {BENCHMARK}")
    records = _need(fundamentals_src.fundamentals(symbol, as_of=as_of, statement="metrics", period="quarterly"), f"fundamentals of {symbol}")
    snap = snapshots.snapshot(symbol, as_of=as_of, categories=["market", "fundamentals"]).to_dict()
    sections = {key: section for key, section in snap["sections"].items() if section.get("status") == "ok"}
    given = {"symbol": symbol, "as_of": as_of.isoformat(), "sections": sections}
    analysts = {"market": AnalystInput(snapshot=given), "fundamentals": AnalystInput(snapshot=given)}
    company = CompanyData(statements=_statements(records), candles=bars, regime=None)
    return CycleData(analysts=analysts, company=company, bars=benchmark, snapshot={"market": sections.get("market"), "fundamentals": sections.get("fundamentals")})


def _contract_id(symbol: str, row: Mapping[str, Any]) -> str:
    strike = Decimal(str(row["strike"])).normalize()
    text = f"{strike:f}"
    return f"OPT:{symbol}:{row['expiration']}:{'C' if row['type'] == 'call' else 'P'}:{text}"


def _chain(symbol: str, desk_id: int) -> tuple[dict[str, Any], dict[str, Quote], Any]:
    """The sheets and the quotes of the connected chain, the underlying included. A contract the provider cannot describe is left out."""
    from hokma.contracts import sheet as sheets
    from hokma.contracts.identity import NoContract
    from hokma.market import options

    try:
        chain = options.options_chain(symbol, None, None, CHAIN_MIN_DAYS, CHAIN_MAX_DAYS, None, None, None, None, CHAIN_LIMIT, desk_id)
    except options.OptionsError as exc:
        raise CycleError("no_data", f"the option chain of {symbol} is not available: {exc}") from exc
    found: dict[str, Any] = {}
    quotes: dict[str, Quote] = {}
    for row in chain["rows"]:
        bid, ask = row.get("bid"), row.get("ask")
        if bid is None or ask is None or float(ask) <= 0 or float(bid) > float(ask):
            continue
        cid = _contract_id(symbol, row)
        try:
            found[cid] = sheets.get(cid)
            quotes[cid] = Quote(Decimal(str(bid)), Decimal(str(ask)))
        except (NoContract, ValueError):
            continue
    spot = chain.get("spot")
    if spot is not None and float(spot) > 0:
        try:
            found[f"EQ:{symbol}"] = sheets.get(f"EQ:{symbol}")
            quotes[f"EQ:{symbol}"] = Quote(Decimal(str(spot)), Decimal(str(spot)))
        except NoContract:
            pass
    if not any(c.startswith("OPT:") for c in quotes):
        raise CycleError("no_data", f"the option chain of {symbol} has no contract with a two sided quote")
    return found, quotes, chain


def trade_context(desk_id: int, symbol: str, as_of: datetime) -> TradeContext:
    """What Isaac reads: the chain, the book and the budget of the desk. The graph adds the verdict, the reports, the limits and the premium switch."""
    from hokma.broker import service
    from hokma.desks.agents.manager import load_desk_mandate

    found, quotes, _chain_info = _chain(symbol, desk_id)
    mandate = load_desk_mandate(desk_id)
    with get_database().read() as u:
        desk = u.desks.get(desk_id)
        pair = service.broker_of_desk(u, desk_id)
    if pair is None:
        raise CycleError("no_data", f"desk {desk_id} has no trading account")
    broker, account = pair
    state = account_snapshot.snapshot(account.id)
    if state.equity is None or state.cash is None:
        raise CycleError("no_data", f"the account of desk {desk_id} has no equity or no cash in the back office: {sorted(state.missing)}")
    book: dict[str, Decimal] = {}
    for position in state.positions:
        cid, qty = position.get("contract_id") or position.get("symbol"), position.get("qty")
        if cid and qty is not None:
            book[str(cid)] = Decimal(str(qty))
    limits = service.get_limits(desk_id)

    def money(key: str) -> Decimal | None:
        value = limits.get(key)
        return None if value is None else Decimal(str(value))

    budget = BudgetInputs(equity=state.equity, cash=state.cash, capital=Decimal(desk.capital), peak_equity=state.peak_equity, day_pnl=state.day_pnl,
                          open_contracts=state.open_contracts, risk_pct=Decimal(str(mandate.risk.max_risk_per_trade_pct)),
                          max_premium_usd=money("max_premium_usd"), max_position_pct=money("max_position_pct"), max_drawdown_pct=money("max_drawdown_pct"),
                          capital_floor_usd=money("capital_floor_usd"))
    return TradeContext(desk_id=desk_id, underlying=symbol, as_of=as_of, sheets=found, quotes=quotes, current_book=book, budget=budget,
                        costs=BrokerCosts.from_broker(broker))


def fills_of(order: Mapping[str, Any]) -> list[Fill]:
    """The fills of an executed order row, as the back office booked them from the broker (source reference `<order>:<execution>`). Each
    fill carries the commission booked for the same execution and the multiplier of the contract sheet."""
    oid, account_id = order.get("broker_order_id"), order.get("account_id")
    if not oid or account_id is None:
        return []
    escaped = str(oid).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with get_database().read() as u:
        rows = u.session.query("SELECT * FROM book_entries WHERE account_id = :a AND kind IN ('fill', 'commission') AND source_ref LIKE :p ESCAPE '\\' ORDER BY id",
                               {"a": account_id, "p": f"{escaped}:%"})
    commission = {r["source_ref"]: abs(Decimal(str(r["amount"]))) for r in rows if r["kind"] == "commission"}
    out: list[Fill] = []
    for r in rows:
        if r["kind"] != "fill" or r["qty"] in (None, "") or r["price"] in (None, "") or not r["contract_id"]:
            continue
        qty = Decimal(str(r["qty"]))
        out.append(Fill(account_id=int(account_id), symbol=r["contract_id"], side="buy" if qty > 0 else "sell", qty=float(abs(qty)), price=float(Decimal(str(r["price"]))),
                        commission=float(commission.get(r["source_ref"], Decimal(0))), at=str(r["occurred_at"]), order_id=str(oid),
                        multiplier=None if r["multiplier"] in (None, "") else Decimal(str(r["multiplier"])), contract_id=r["contract_id"]))
    return out


def build_runtime(desk_id: int) -> CycleRuntime:
    """The runtime of one desk: a model client per seat on the tier of its agent, the trader model, the data and the chain."""
    cache: dict[str, ChatModel] = {}

    def chat(key: str) -> ChatModel:
        if key not in cache:
            cache[key] = ClientChat(_client(desk_id, key))
        return cache[key]

    return CycleRuntime(chat=chat, trader_model=_client(desk_id, "A11"), load=load, trade_context=trade_context, caller=_caller(desk_id),
                        fills_of=fills_of, mark_of=None)


def _caller(desk_id: int) -> Any:
    """The caller the proposal of Isaac goes out as: the agent of his seat in this desk, never the owner."""
    from hokma.kit.hokma_tools import Caller

    return Caller({"HOKMA_AGENT": _slug(desk_id, "A11")})
