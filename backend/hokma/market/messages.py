"""Catalog codes the live market layer and Connections send to the panel, and their text in the active language.
The panel translates by `code`; `message` is a convenience for clients that print it as is."""
from collections.abc import Callable

from hokma.i18n import t

MARKET_CODES: dict[str, Callable[[dict], str]] = {
    "market.unknown_symbol": lambda params: t("market.error.unknown_symbol"),
    "market.limit": lambda params: t("market.error.limit"),
    "market.down": lambda params: t("market.error.down"),
    "market.bad_request": lambda params: t("market.error.bad_request"),
    "market.not_entitled": lambda params: t("market.error.not_entitled"),
    "market.interval_unavailable": lambda params: t("market.error.interval_unavailable", **params),
    "market.seconds_need_ticks": lambda params: t("market.error.seconds_need_ticks"),
}

CONNECTION_CODES: dict[str, Callable[[], str]] = {
    "connections.auth_failed": lambda: t("connections.error.auth_failed"),
    "connections.no_key": lambda: t("connections.error.no_key"),
    "connections.timeout": lambda: t("connections.error.timeout"),
    "connections.unreachable": lambda: t("connections.error.unreachable"),
    "connections.rate_limited": lambda: t("connections.error.rate_limited"),
    "connections.connection_limit": lambda: t("connections.error.connection_limit"),
    "connections.store_unavailable": lambda: t("connections.error.store_unavailable"),
    "connections.unavailable": lambda: t("connections.error.unavailable"),
    "connections.unknown_provider": lambda: t("connections.error.unknown_provider"),
    "connections.bad_key": lambda: t("connections.error.bad_key"),
    "connections.bad_priority": lambda: t("connections.error.bad_priority"),
    "connections.token_invalid": lambda: t("connections.error.token_invalid"),
    "connections.access_revoked": lambda: t("connections.error.access_revoked"),
    "connections.other_device": lambda: t("connections.error.other_device"),
    "connections.bad_request": lambda: t("connections.error.bad_request"),
}

# What an adapter reports in `SourceStatus.code` when it stops for good, as a catalog code.
DOWN_CODES = {
    "auth_failed": "connections.auth_failed",
    "connection_limit": "connections.connection_limit",
    "rate_limited": "connections.rate_limited",
    "token_invalid": "connections.token_invalid",
    "access_revoked": "connections.access_revoked",
    "other_device": "connections.other_device",
}


def market_message(code: str, params: dict | None = None) -> str:
    """Text of a market code. `params` fills the placeholders of codes that carry them (`market.interval_unavailable`)."""
    return MARKET_CODES.get(code, MARKET_CODES["market.down"])(params or {})


def interval_message(code: str) -> str:
    """Why `intervals.parse` refused a text: `months` or `invalid`. Sent as the message of a `market.bad_request`."""
    return t("market.error.interval_months") if code == "months" else t("market.error.interval_invalid")


def connection_message(code: str | None) -> str:
    if not code:
        return ""
    return CONNECTION_CODES.get(code, CONNECTION_CODES["connections.unreachable"])()
