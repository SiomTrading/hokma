"""Example alpha models (SPEC-020: L-52, FM-20). Each is a H1 `AlphaModel`: facts in, a Signal out."""
from hokma.factory.alphas.ema_cross import EmaCross
from hokma.factory.alphas.historical_returns import HistoricalReturns
from hokma.factory.alphas.macd import Macd
from hokma.factory.alphas.pairs import PairsCorrelation
from hokma.factory.alphas.rsi import RsiReversion
from hokma.factory.alphas.tsmom import Tsmom

BUILTIN_ALPHAS: dict[str, type] = {
    "ema_cross": EmaCross,
    "rsi_reversion": RsiReversion,
    "macd": Macd,
    "historical_returns": HistoricalReturns,
    "pairs_correlation": PairsCorrelation,
    "tsmom": Tsmom,
}

__all__ = ["BUILTIN_ALPHAS", "EmaCross", "HistoricalReturns", "Macd", "PairsCorrelation", "RsiReversion", "Tsmom"]
