"""Deterministic identity of an instrument and the contract sheet (SPEC-018: TA-33, AH-41).

Canonical ids: `EQ:AAPL`, `FUT:NQ:2026-12`, `OPT:SPY:2026-12-18:C:600`, `FOP:ES:2026-12:2026-12-18:C:6000`, `CRY:BTC-USD`,
`CFD:EURUSD`, `BND:912796XY1`. The same instrument written in three ways by three providers resolves to the same id; the
sheet (multiplier, tick, expiration, strike, underlying) comes from a provider, resolved once per run and cached.
"""
from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

AssetType = Literal["equity", "etf", "future", "option", "future_option", "cfd", "crypto", "bond", "bill"]
ASSET_TYPES = ("equity", "etf", "future", "option", "future_option", "cfd", "crypto", "bond", "bill")
MONTH_CODES = "FGHJKMNQUVXZ"
SPECS_PATH = Path(__file__).with_name("futures_specs.json")
_OCC = re.compile(r"^([A-Z]{1,6})\s*(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")
_FUT_CODE = re.compile(r"^/?([A-Z0-9]{1,4}?)([FGHJKMNQUVXZ])(\d{1,2})$")
_CANON = re.compile(r"^(EQ|ETF|FUT|OPT|FOP|CRY|CFD|BND|BIL):")


class NoContract(LookupError):
    """There is no contract sheet for the instrument: nothing is ordered or valued on a guess."""

    def __init__(self, symbol: str, detail: str = "") -> None:
        self.symbol, self.detail = symbol, detail
        super().__init__(f"no contract sheet for {symbol!r}" + (f": {detail}" if detail else ""))


@dataclass(frozen=True)
class ContractSheet:
    contract_id: str
    asset_type: AssetType
    root: str | None
    underlying: str | None
    multiplier: Decimal
    tick_size: Decimal | None
    tick_value: Decimal | None
    expiration: date | None
    strike: Decimal | None
    put_call: Literal["C", "P"] | None
    currency: Literal["USD"]
    source: str
    as_of: datetime

    def to_row(self) -> dict[str, Any]:
        return {"contract_id": self.contract_id, "asset_type": self.asset_type, "root": self.root, "underlying": self.underlying,
                "multiplier": str(self.multiplier), "tick_size": None if self.tick_size is None else str(self.tick_size),
                "tick_value": None if self.tick_value is None else str(self.tick_value),
                "expiration": None if self.expiration is None else self.expiration.isoformat(),
                "strike": None if self.strike is None else str(self.strike), "put_call": self.put_call,
                "currency": self.currency, "source": self.source, "as_of": self.as_of.strftime("%Y-%m-%d %H:%M:%S")}

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ContractSheet":
        def dec(value: Any) -> Decimal | None:
            return None if value in (None, "") else Decimal(str(value))

        raw = row.get("as_of")
        as_of = raw if isinstance(raw, datetime) else datetime.strptime(str(raw)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
        return cls(row["contract_id"], row["asset_type"], row.get("root"), row.get("underlying"), Decimal(str(row["multiplier"])),
                   dec(row.get("tick_size")), dec(row.get("tick_value")),
                   date.fromisoformat(str(row["expiration"])[:10]) if row.get("expiration") else None, dec(row.get("strike")),
                   row.get("put_call"), row.get("currency") or "USD", row["source"], as_of.replace(tzinfo=timezone.utc))


@dataclass(frozen=True)
class Parsed:
    """What an id or a provider symbol says about an instrument, before any sheet."""
    asset_type: AssetType
    root: str | None = None                 # the future root, or the symbol of an equity, crypto pair or cfd
    underlying: str | None = None
    expiration: date | None = None          # the expiration of the contract (the option for an option)
    future_expiration: date | None = None   # the month of the future that a future option sits on
    strike: Decimal | None = None
    put_call: str | None = None

    @property
    def contract_id(self) -> str:
        return canonical_id(self)


def _fmt_strike(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text


def canonical_id(p: Parsed) -> str:
    if p.asset_type in ("equity", "etf"):
        return f"{'ETF' if p.asset_type == 'etf' else 'EQ'}:{p.root}"
    if p.asset_type == "crypto":
        return f"CRY:{p.root}"
    if p.asset_type == "cfd":
        return f"CFD:{p.root}"
    if p.asset_type == "bond":
        return f"BND:{p.root}"
    if p.asset_type == "bill":
        return f"BIL:{p.root}"
    if p.asset_type == "future":
        if p.expiration is None:
            raise NoContract(p.root or "?", "a future needs its expiration month")
        return f"FUT:{p.root}:{p.expiration.strftime('%Y-%m')}"
    if p.asset_type == "option":
        if p.expiration is None or p.strike is None or p.put_call is None:
            raise NoContract(p.underlying or "?", "an option needs expiration, strike and type")
        return f"OPT:{p.underlying}:{p.expiration.isoformat()}:{p.put_call}:{_fmt_strike(p.strike)}"
    if p.future_expiration is None or p.expiration is None or p.strike is None or p.put_call is None:
        raise NoContract(p.root or "?", "a future option needs the future month, expiration, strike and type")
    return f"FOP:{p.root}:{p.future_expiration.strftime('%Y-%m')}:{p.expiration.isoformat()}:{p.put_call}:{_fmt_strike(p.strike)}"


MAX_STRIKE = Decimal(100000)


def _check_parsed(p: Parsed, shown: str) -> Parsed:
    """Finite strike, at most 3 decimals and under 100000; expirations inside the two digit year window of the OCC symbol (2000 to 2099)."""
    if p.strike is not None:
        if not p.strike.is_finite() or not 0 < p.strike < MAX_STRIKE or p.strike.as_tuple().exponent < -3 and p.strike != p.strike.quantize(Decimal("0.001")):
            raise NoContract(shown[:80], "strike must be finite, above 0, under 100000 and have at most 3 decimals")
    for name in (p.root, p.underlying):
        if name is not None and not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,11}", name):
            raise NoContract(shown[:80], "a symbol is upper case letters and digits")
    for day in (p.expiration, p.future_expiration):
        if day is not None and not 2000 <= day.year <= 2099:
            raise NoContract(shown[:80], "expiration year must be inside 2000 to 2099")
    return p


def parse_canonical(contract_id: str) -> Parsed:
    """The inverse of `canonical_id`. Raises NoContract for text that is not a canonical id, and for any id that does not come back
    exactly the same from its parsed form (a malformed id could otherwise name another instrument at the broker)."""
    parsed = _parse_canonical(contract_id)
    _check_parsed(parsed, contract_id)
    if canonical_id(parsed) != contract_id:
        raise NoContract(str(contract_id)[:80], "not the exact canonical form of its instrument")
    return parsed


def _parse_canonical(contract_id: str) -> Parsed:
    if not isinstance(contract_id, str) or not _CANON.match(contract_id):
        raise NoContract(str(contract_id)[:80], "not a canonical contract id")
    parts = contract_id.split(":")
    kind = parts[0]
    try:
        if kind in ("EQ", "ETF") and len(parts) == 2:
            return Parsed("etf" if kind == "ETF" else "equity", root=parts[1])
        if kind == "CRY" and len(parts) == 2:
            return Parsed("crypto", root=parts[1])
        if kind == "CFD" and len(parts) == 2:
            return Parsed("cfd", root=parts[1])
        if kind in ("BND", "BIL") and len(parts) == 2:
            return Parsed("bond" if kind == "BND" else "bill", root=parts[1])
        if kind == "FUT" and len(parts) == 3:
            return Parsed("future", root=parts[1], expiration=_month(parts[2]))
        if kind == "OPT" and len(parts) == 5:
            return Parsed("option", root=parts[1], underlying=parts[1], expiration=date.fromisoformat(parts[2]), put_call=parts[3],
                          strike=Decimal(parts[4]))
        if kind == "FOP" and len(parts) == 6:
            return Parsed("future_option", root=parts[1], underlying=parts[1], future_expiration=_month(parts[2]),
                          expiration=date.fromisoformat(parts[3]), put_call=parts[4], strike=Decimal(parts[5]))
    except (ValueError, InvalidOperation) as exc:
        raise NoContract(contract_id[:80], "malformed canonical id") from exc
    raise NoContract(contract_id[:80], "malformed canonical id")


def _month(text: str) -> date:
    return date(int(text[:4]), int(text[5:7]), 1)


def _year(two: str) -> int:
    value = int(two)
    return 2000 + value if len(two) == 2 else 2020 + value


def parse_symbol(symbol: str) -> Parsed:
    """Any spelling that a supported provider uses for the same instrument, or a canonical id. A continuous future
    (`NQ=F`, `/NQ`) parses without expiration: the sheet comes from the provider that knows the front month."""
    if not isinstance(symbol, str) or not 1 <= len(symbol.strip()) <= 80:
        raise NoContract(str(symbol)[:80], "empty or too long")
    text = symbol.strip().upper()
    if _CANON.match(text):
        return parse_canonical(text)
    match = _OCC.match(text.replace(" ", ""))
    if match:
        root, yy, mm, dd, kind, strike = match.groups()
        try:
            expiry = date(_year(yy), int(mm), int(dd))
        except ValueError as exc:
            raise NoContract(text[:80], "not a real date") from exc
        return Parsed("option", root=root, underlying=root, expiration=expiry, put_call=kind, strike=Decimal(int(strike)) / Decimal(1000))
    if text.endswith("=F") or (text.startswith("/") and len(text) <= 6 and not _FUT_CODE.match(text)):
        return Parsed("future", root=text.removesuffix("=F").lstrip("/"))
    code = _FUT_CODE.match(text) if text.startswith("/") or _is_future_code(text) else None
    if code:
        root, month, year = code.groups()
        return Parsed("future", root=root, expiration=date(_future_year(year), MONTH_CODES.index(month) + 1, 1))
    for pair in (re.fullmatch(r"([A-Z0-9]{2,10})[-/]([A-Z]{3,4})", text),):
        if pair:
            return Parsed("crypto", root=f"{pair.group(1)}-{pair.group(2)}")
    if text.startswith(("BINANCE:", "COINBASE:", "KRAKEN:")):
        pair = text.split(":", 1)[1]
        for quote in ("USDT", "USDC", "USD", "EUR", "BTC"):
            if pair.endswith(quote) and len(pair) > len(quote):
                return Parsed("crypto", root=f"{pair[:-len(quote)]}-{'USD' if quote == 'USDT' else quote}")
    if re.fullmatch(r"[A-Z]{6}=X", text):
        return Parsed("cfd", root=text[:6])
    if re.fullmatch(r"[A-Z]{1,5}(?:[.\-/][A-Z])?", text):
        return Parsed("equity", root=text.replace("/", ".").replace("-", "."))
    raise NoContract(text[:80], "unknown symbol format")


def _is_future_code(text: str) -> bool:
    m = _FUT_CODE.match(text)
    return bool(m and m.group(1) in _specs()["roots"])


def _future_year(digits: str) -> int:
    return _year(digits) if len(digits) == 2 else 2020 + int(digits)


@lru_cache(maxsize=1)
def _specs() -> dict[str, Any]:
    return json.loads(SPECS_PATH.read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------------- providers of sheets

SheetProvider = Callable[[Parsed], "ContractSheet | None"]
_providers: dict[str, SheetProvider] = {}
_front_months: dict[str, Callable[[str], date | None]] = {}
_cache: dict[str, ContractSheet] = {}
_lock = threading.Lock()


def register_provider(name: str, provider: SheetProvider, front_month: Callable[[str], date | None] | None = None) -> None:
    """A provider that gives sheets (Schwab instruments, the options chain, the crypto exchange). Registered once at start;
    a provider that returns None does not know the instrument and the next one is asked."""
    with _lock:
        _providers[name] = provider
        if front_month is not None:
            _front_months[name] = front_month
        _cache.clear()


def clear_providers() -> None:
    with _lock:
        _providers.clear()
        _front_months.clear()
        _cache.clear()


def forget_run() -> None:
    """The cache lasts one run: a new run asks the provider again."""
    with _lock:
        _cache.clear()


def _default_sheet(p: Parsed, now: datetime) -> ContractSheet | None:
    """The sheets that need no network: shares, ETFs and crypto pairs (multiplier 1), standard options (the OCC
    deliverable of 100), and the exchange specification of a future with its expiration month."""
    one, hundred = Decimal(1), Decimal(100)
    if p.asset_type in ("equity", "etf"):
        return ContractSheet(canonical_id(p), p.asset_type, None, p.root, one, Decimal("0.01"), Decimal("0.01"), None, None, None,
                             "USD", "hokma.market.instruments", now)
    if p.asset_type == "crypto":
        return ContractSheet(canonical_id(p), "crypto", None, p.root, one, None, None, None, None, None, "USD",
                             "hokma.market.symbols", now)
    if p.asset_type == "option":
        return ContractSheet(canonical_id(p), "option", p.underlying, p.underlying, hundred, Decimal("0.01"), Decimal("1"),
                             p.expiration, p.strike, p.put_call, "USD", "occ.standard_deliverable", now)
    if p.asset_type == "future" and p.expiration is not None:
        spec = _specs()["roots"].get(p.root or "")
        if spec is None:
            return None
        mult, tick = Decimal(spec["multiplier"]), Decimal(spec["tick_size"])
        return ContractSheet(canonical_id(p), "future", p.root, p.root, mult, tick, mult * tick, p.expiration, None, None, "USD",
                             "exchange_spec_table", now)
    return None


def resolve(symbol: str, *, provider: str | None = None) -> ContractSheet:
    """The sheet of an instrument (TA-33). Raises NoContract when no provider knows it. Resolved once per run."""
    from hokma.db.timeutil import utcnow

    parsed = parse_symbol(symbol)
    if parsed.asset_type == "future" and parsed.expiration is None:
        month = None
        for name, front in list(_front_months.items()):
            if provider in (None, name):
                month = front(parsed.root or "")
                if month is not None:
                    break
        if month is None:
            raise NoContract(symbol, "the provider gave no front month for this root")
        parsed = replace(parsed, expiration=month)
    cid = canonical_id(parsed)
    with _lock:
        hit = _cache.get(cid)
    if hit is not None:
        return hit
    now = datetime.strptime(utcnow()[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    sheet: ContractSheet | None = None
    for name, fn in list(_providers.items()):
        if provider in (None, name):
            sheet = fn(parsed)
            if sheet is not None:
                break
    if sheet is None:
        sheet = _default_sheet(parsed, now)
    if sheet is None:
        raise NoContract(symbol, "no provider has a sheet for it")
    with _lock:
        _cache[cid] = sheet
    return sheet
