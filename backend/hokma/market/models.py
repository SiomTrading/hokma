"""Plain data shared by every market data source and by the API layer."""
from dataclasses import dataclass, field

TIMEFRAMES = ("1m", "5m", "15m", "1h", "1D")
TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1D": 86400}
# How many bars the chart shows for each timeframe (the newest ones).
VISIBLE_BARS = {"1m": 240, "5m": 180, "15m": 160, "1h": 160, "1D": 180}
OPENING_RANGE_MINUTES = 30


@dataclass(frozen=True)
class Candle:
    t: int  # start of the bar, epoch seconds UTC
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass(frozen=True)
class SymbolHit:
    symbol: str
    name: str
    kind: str  # equity | etf | index | future | crypto | currency | other
    exchange: str


@dataclass(frozen=True)
class SessionPeriod:
    start: int
    end: int


@dataclass
class Series:
    """What a source returns for one symbol and timeframe, before anything is derived from it."""
    symbol: str
    name: str
    kind: str
    exchange: str
    currency: str
    timezone: str
    utc_offset_seconds: int
    candles: list[Candle]
    previous_close: float | None
    last_trade_at: int | None  # epoch seconds of the last trade the source knows about
    session: SessionPeriod | None  # the regular session the source reports as current or next
    fetched_at: int
    stale: bool = False  # served from an old cache because the network failed
    source: str = ""


@dataclass(frozen=True)
class OpeningRange:
    high: float
    session_date: str  # YYYY-MM-DD in the exchange time zone
    start: int
    end: int
    complete: bool  # the data already covers the whole window


@dataclass
class ChartData:
    symbol: str
    name: str
    kind: str
    exchange: str
    currency: str
    timezone: str
    timeframe: str
    candles: list[Candle]
    last_price: float | None
    previous_close: float | None
    change: float | None
    change_pct: float | None
    market_open: bool
    last_trade_at: int | None
    data_age_seconds: int | None
    delayed: bool
    opening_range: OpeningRange | None
    source: str
    stale: bool
    fetched_at: int
    notes: list[str] = field(default_factory=list)
