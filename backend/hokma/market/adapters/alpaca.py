"""Alpaca market data stream, IEX feed. Authentication is a message with the Key ID and the Secret; the free plan
allows one connection and 30 symbols."""
import asyncio
import dataclasses

from hokma.market.adapters.alpaca_options import AlpacaOptionsFeed
from hokma.market.adapters.ws_base import WebSocketSource, parse_iso_ms
from hokma.market.providers import ProviderSpec
from hokma.market.stream import SourceStatus, Tick, TerminalError

AUTH_ERRORS = {401, 402, 403, 404}


class AlpacaSource(WebSocketSource):
    def __init__(self, spec: ProviderSpec, credentials: dict, url: str | None = None, **kwargs) -> None:
        super().__init__(spec, url, **kwargs)
        self.__credentials = {"key": credentials["key_id"], "secret": credentials["secret"]}
        self._spec_stream = dict(spec.stream)
        self._credentials_for_options = credentials
        self._options_feed: AlpacaOptionsFeed | None = None
        self.capabilities = dataclasses.replace(self.capabilities, live_options=bool(self._spec_stream.get("options_url")), clock_aligned=True)

    async def subscribe_options(self, keys: list[str]) -> None:
        """Option contracts to follow, by the msgpack stream of Alpaca (the keys come in the padded form of the hub: Alpaca's have no spaces)."""
        if not keys:
            return
        if self._options_feed is None:
            self._options_feed = AlpacaOptionsFeed(self._spec_stream["options_url"], self._credentials_for_options, self.emit, backoff=self._backoff)
        await self._options_feed.want([k.replace(" ", "") for k in keys])

    async def unsubscribe_options(self, keys: list[str]) -> None:
        if self._options_feed is not None and keys:
            await self._options_feed.drop([k.replace(" ", "") for k in keys])

    async def market_hours(self, asset_class: str):
        """The regular, pre and post pieces of today as Alpaca's calendar says them (`/v3/calendar`, see `alpaca_hours`), or None."""
        if asset_class != "equity":
            return None
        from datetime import datetime

        from hokma.market import alpaca_hours
        from hokma.market.history import http_get
        from hokma.market.tz import NEW_YORK

        creds = self._credentials_for_options
        headers = {"APCA-API-KEY-ID": creds["key_id"], "APCA-API-SECRET-KEY": creds["secret"]}
        today = datetime.now(NEW_YORK).date()
        try:
            return await asyncio.to_thread(alpaca_hours.fetch_day, http_get, headers, today, alpaca_hours.trading_hosts(creds["key_id"]))
        except Exception:                                    # noqa: BLE001  (no hours is "unknown", never a guess)
            return None

    async def _after_close(self) -> None:
        if self._options_feed is not None:
            await self._options_feed.stop()
            self._options_feed = None
        await super()._after_close()

    async def _handshake(self, ws) -> None:
        await self._recv(ws)                                           # [{"T":"success","msg":"connected"}]
        await self._send({"action": "auth", **self.__credentials})
        for item in await self._recv(ws):
            if item.get("T") == "error":
                code = int(item.get("code", 0))
                raise TerminalError("connection_limit" if code == 406 else "auth_failed")
        if self._symbols:
            await self._send_subscribe(sorted(self._symbols))
        self._mark_ready()

    async def _send_subscribe(self, symbols: list[str]) -> None:
        await self._send({"action": "subscribe", "trades": symbols})

    async def _send_unsubscribe(self, symbols: list[str]) -> None:
        await self._send({"action": "unsubscribe", "trades": symbols})

    def _on_message(self, message) -> None:
        for item in message if isinstance(message, list) else [message]:
            if not isinstance(item, dict):
                continue
            kind = item.get("T")
            try:
                if kind == "t":
                    self.emit(Tick(item["S"], parse_iso_ms(item["t"]), float(item["p"]), float(item.get("s") or 0)))
                elif kind == "subscription":
                    self.emit(SourceStatus(self.data_state, "subscribed"))
                elif kind == "error":
                    code = int(item.get("code", 0))
                    if code in AUTH_ERRORS:
                        raise TerminalError("auth_failed")
                    if code == 406:
                        raise TerminalError("connection_limit")
            except (KeyError, ValueError, TypeError):
                continue
