"""Errors of the market layer. `code` is stable; the API translates it for the user."""


class MarketError(Exception):
    def __init__(self, code: str, status: int = 502, params: dict | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.params = params or {}


UNKNOWN_SYMBOL = "unknown_symbol"
RATE_LIMITED = "rate_limited"
NETWORK = "network"
UPSTREAM = "upstream_error"
BAD_RESPONSE = "bad_response"
INTERVAL_UNAVAILABLE = "interval_unavailable"
SECONDS_NEED_TICKS = "seconds_need_ticks"
