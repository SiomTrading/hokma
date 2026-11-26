"""Strategy factory (SPEC-020): the strategy contract, orders, signals, timeframes, sessions, labels, alphas and the registry.

Nothing here talks to a broker or to the backtest engine: a strategy only emits `OrderIntent` values.
"""
