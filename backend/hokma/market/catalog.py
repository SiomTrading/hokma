"""Symbols offered before the owner types anything. A fixed list of well-known tickers: no prices, nothing live."""
from hokma.market.models import SymbolHit

SUGGESTIONS: tuple[SymbolHit, ...] = (
    SymbolHit("NQ=F", "E-mini Nasdaq 100", "future", "CME"),
    SymbolHit("ES=F", "E-mini S&P 500", "future", "CME"),
    SymbolHit("YM=F", "E-mini Dow", "future", "CBOT"),
    SymbolHit("GC=F", "Gold", "future", "COMEX"),
    SymbolHit("CL=F", "Crude Oil", "future", "NYMEX"),
    SymbolHit("SPY", "SPDR S&P 500 ETF", "etf", "NYSE Arca"),
    SymbolHit("QQQ", "Invesco QQQ Trust", "etf", "NASDAQ"),
    SymbolHit("^GSPC", "S&P 500", "index", "Index"),
    SymbolHit("AAPL", "Apple", "equity", "NASDAQ"),
    SymbolHit("BTC-USD", "Bitcoin USD", "crypto", "Crypto"),
)
