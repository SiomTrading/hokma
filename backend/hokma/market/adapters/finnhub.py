"""Finnhub trade stream. The provider takes the key as a query parameter of the connection URL; the URL is built in
memory and never logged. `data_state` comes from `providers.json` (`delayed` until a measurement confirms `live`)."""
from urllib.parse import quote

from hokma.market.adapters.ws_base import WebSocketSource
from hokma.market.providers import ProviderSpec
from hokma.market.stream import Tick


class FinnhubSource(WebSocketSource):
    def __init__(self, spec: ProviderSpec, key: str, url: str | None = None, **kwargs) -> None:
        super().__init__(spec, url, **kwargs)
        self.__key = key

    def _url(self) -> str:
        return f"{self._url_base}?token={quote(self.__key, safe='')}"

    async def _handshake(self, ws) -> None:
        for symbol in sorted(self._symbols):
            await self._send({"type": "subscribe", "symbol": symbol})
        self._mark_ready()

    async def _send_subscribe(self, symbols: list[str]) -> None:
        for symbol in symbols:
            await self._send({"type": "subscribe", "symbol": symbol})

    async def _send_unsubscribe(self, symbols: list[str]) -> None:
        for symbol in symbols:
            await self._send({"type": "unsubscribe", "symbol": symbol})

    def _on_message(self, message) -> None:
        if not isinstance(message, dict) or message.get("type") != "trade":
            return
        for item in message.get("data") or []:
            try:
                self.emit(Tick(item["s"], int(item["t"]), float(item["p"]), float(item.get("v") or 0)))
            except (KeyError, ValueError, TypeError):
                continue
