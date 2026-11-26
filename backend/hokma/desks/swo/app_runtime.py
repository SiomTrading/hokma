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
from hokma.desks.core.safety import scrub_text
from hokma.desks.agents.trader import BrokerCosts, Quote, TradeContext
from hokma.desks.core.graph import ChatModel
from hokma.desks.plantel import SEATS
from hokma.desks.swo.create import desk_agent_slug
from hokma.desks.swo.graph import AnalystInput, ClientChat, CycleData, CycleError, CycleRuntime
from hokma.desks.trading.budget import BudgetInputs
from hokma.i18n import t as _t

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


# What the owner reads when a source is missing: what failed, in words, and where to set it up (Connections).
# The texts live in the message catalogs, read when they are needed so they follow the language of the owner.
def _fix_market() -> str:
    return _t("desks.cycle.fix_market")


def _fix_fundamentals() -> str:
    return _t("desks.cycle.fix_fundamentals")


# One literal key per reason, so the catalog check can see that every key is used.
REASONS = {
    "provider_key_missing": lambda: _t("desks.cycle.reason.provider_key_missing"),
    "provider_unreachable": lambda: _t("desks.cycle.reason.provider_unreachable"),
    "rate_limited": lambda: _t("desks.cycle.reason.rate_limited"),
    "empty_result": lambda: _t("desks.cycle.reason.empty_result"),
    "no_closed_bars_in_window": lambda: _t("desks.cycle.reason.no_closed_bars_in_window"),
    "stale_data": lambda: _t("desks.cycle.reason.stale_data"),
    "window_not_observed": lambda: _t("desks.cycle.reason.window_not_observed"),
    "not_published_by_as_of": lambda: _t("desks.cycle.reason.not_published_by_as_of"),
    "sec_identity_missing": lambda: _t("desks.cycle.reason.sec_identity_missing"),
    "not_connected": lambda: _t("desks.cycle.reason.not_connected"),
}


def _why(result: Any) -> str:
    reason = str(result.reason or "")
    text = REASONS.get(reason) or REASONS.get(str(result.status))
    return text() if text else reason or str(result.status)


NEEDS_SETUP = ("provider_key_missing", "not_connected")      # only these causes are solved by connecting something; the others are not the owner's setup


def _need(result: Any, what: str, *, source: str = "data", fix: str = "") -> Any:
    """The value of a `DataResult`; anything that is not ok stops the step with its reason, in words. The setup step is added only when the
    cause is a missing key or connection."""
    if not result.ok:
        fix = fix if str(result.reason) in NEEDS_SETUP else ""
        raise CycleError("no_data", _t("desks.cycle.no_data", what=what, why=_why(result), source=result.source, status=result.status, reason=result.reason, fix=fix).strip(), source=source, hint=fix or None)
    return result.value


def _fetch(call: Any, what: str, *, source: str, fix: str) -> Any:
    """Run one source. A source that raises instead of answering is the same as one that does not answer: the cause is chained, not lost."""
    try:
        result = call()
    except CycleError:
        raise
    except Exception as exc:                                                   # noqa: BLE001
        raise CycleError("no_data", _t("desks.cycle.source_failed", what=what, error=f"{type(exc).__name__}: {scrub_text(str(exc))[:200]}"), source=source) from exc
    return _need(result, what, source=source, fix=fix)


def _statements(records: Any) -> pd.DataFrame:
    """The point in time frame the investors read: one row per period end, a `filed` column, numeric accounts."""
    frame = pd.DataFrame(list(records))
    if frame.empty or "report_period" not in frame or "filing_date" not in frame:
        raise CycleError("no_data", _t("desks.cycle.no_statements"), source="fundamentals")
    frame = frame.dropna(subset=["report_period", "filing_date"]).copy()
    frame["filed"] = frame.pop("filing_date").astype(str).str[:10]
    frame = frame.set_index(frame.pop("report_period").astype(str).str[:10]).sort_index()
    numeric = [c for c in frame.columns if c != "filed" and pd.api.types.is_numeric_dtype(frame[c])]
    if not numeric:
        raise CycleError("no_data", _t("desks.cycle.no_numeric_accounts"), source="fundamentals")
    return frame[[*numeric, "filed"]]


def load(desk_id: int, symbol: str, as_of: datetime) -> CycleData:
    """The data of one symbol at one cut-off: bars of the symbol and of the benchmark, point in time fundamentals and the verified snapshot.

    Market data is the quorum: without bars of the symbol and of the benchmark there is no cycle, and the error says so. A missing source of
    fundamentals only leaves the fundamentals analyst absent (`problems`) and the company filter without statements (its profiles abstain);
    the analysts that have their data go on."""
    from hokma import data
    from hokma.data import fundamentals_src
    from hokma.data import snapshot as snapshots

    start = as_of - timedelta(days=BARS_DAYS)
    bars = _fetch(lambda: data.bars(symbol, "1D", start=start, end=as_of, as_of=as_of), _t("desks.cycle.what.market", symbol=symbol), source="market", fix=_fix_market())
    benchmark = _fetch(lambda: data.bars(BENCHMARK, "1D", start=start, end=as_of, as_of=as_of), _t("desks.cycle.what.market", symbol=BENCHMARK), source="market", fix=_fix_market())
    problems: dict[str, str] = {}
    statements = pd.DataFrame({"filed": pd.Series(dtype=str)})
    try:
        records = _fetch(lambda: fundamentals_src.fundamentals(symbol, as_of=as_of, statement="metrics", period="quarterly"),
                         _t("desks.cycle.what.fundamentals", symbol=symbol), source="fundamentals", fix=_fix_fundamentals())
        statements = _statements(records)
    except CycleError as exc:
        problems["fundamentals"] = str(exc)
    sections: dict[str, Any] = {}
    try:
        snap = snapshots.snapshot(symbol, as_of=as_of, categories=["market", "fundamentals"]).to_dict()
        sections = {key: section for key, section in snap["sections"].items() if section.get("status") == "ok"}
    except Exception as exc:                                                   # noqa: BLE001  (the snapshot is evidence for two analysts, not the quorum)
        problems.setdefault("market", _t("desks.cycle.snapshot_failed", symbol=symbol, error=f"{type(exc).__name__}: {scrub_text(str(exc))[:200]}"))
    if "market" not in sections:
        problems.setdefault("market", _t("desks.cycle.snapshot_market_missing", symbol=symbol))
    if "fundamentals" not in sections:
        problems.setdefault("fundamentals", _t("desks.cycle.snapshot_fundamentals_missing", symbol=symbol))
    given = {"symbol": symbol, "as_of": as_of.isoformat(), "sections": sections}
    analysts = {key: AnalystInput(snapshot=given) for key in ("market", "fundamentals") if key not in problems}
    company = CompanyData(statements=statements, candles=bars, regime=None)
    return CycleData(analysts=analysts, company=company, bars=benchmark, snapshot={"market": sections.get("market"), "fundamentals": sections.get("fundamentals")},
                     problems=problems)


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
        pair = service.broker_of_resolved(u, desk_id)           # the account of the principal strategy; never "the first account of the desk"
    limits = service.get_limits(desk_id)
    book: dict[str, Decimal] = {}
    if pair is None:
        # No account for this strategy (the desk has none, or the owner has not said which one): the cycle still runs and its proposal is
        # kept as analysis only or waits for the owner (`exec.submit`). It is sized on an empty book at the capital of the desk: nothing is
        # read from an account that is not there, and the real checks run again with the real account when the order goes out.
        broker, state = None, None
        capital = Decimal(desk.capital or 0)
        equity, cash, peak, day_pnl, open_contracts = capital, capital, capital, Decimal(0), Decimal(0)
    else:
        broker, account = pair
        state = account_snapshot.snapshot(account.id)
        if state.equity is None or state.cash is None:
            raise CycleError("no_data", f"the account of desk {desk_id} has no equity or no cash in the back office: {sorted(state.missing)}")
        for position in state.positions:
            cid, qty = position.get("contract_id") or position.get("symbol"), position.get("qty")
            if cid and qty is not None:
                book[str(cid)] = Decimal(str(qty))
        equity, cash, peak, day_pnl, open_contracts = state.equity, state.cash, state.peak_equity, state.day_pnl, state.open_contracts

    def money(key: str) -> Decimal | None:
        value = limits.get(key)
        return None if value is None else Decimal(str(value))

    budget = BudgetInputs(equity=equity, cash=cash, capital=Decimal(desk.capital or 0), peak_equity=peak, day_pnl=day_pnl,
                          open_contracts=open_contracts, risk_pct=Decimal(str(mandate.risk.max_risk_per_trade_pct)),
                          max_premium_usd=money("max_premium_usd"), max_position_pct=money("max_position_pct"), max_drawdown_pct=money("max_drawdown_pct"),
                          capital_floor_usd=money("capital_floor_usd"))
    return TradeContext(desk_id=desk_id, underlying=symbol, as_of=as_of, sheets=found, quotes=quotes, current_book=book, budget=budget,
                        costs=BrokerCosts.from_broker(broker))      # no broker: the default costs of the simulated one, as the class says


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
