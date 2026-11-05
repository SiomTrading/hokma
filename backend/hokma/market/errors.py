"""Errors of the market layer. `code` is stable; the API translates it for the user."""


class MarketError(Exception):
    def __init__(self, code: str, status: int = 502) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


UNKNOWN_SYMBOL = "unknown_symbol"
RATE_LIMITED = "rate_limited"
NETWORK = "network"
UPSTREAM = "upstream_error"
BAD_RESPONSE = "bad_response"
