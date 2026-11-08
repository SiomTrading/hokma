"""Coinbase Exchange public feed. No key. `ticker` and `matches` channels; the first `subscribe` must arrive within
5 seconds of connecting, so a heartbeat subscription is sent at once and keeps the socket valid with no symbols."""
from hokma.market.adapters.ws_base import WebSocketSource, parse_iso_ms
from hokma.market.stream import SourceStatus, SymbolError, Tick

HEARTBEAT_PRODUCT = "BTC-USD"


class CoinbaseSource(WebSocketSource):
    async def _handshake(self, ws) -> None:
        await self._send({"type": "subscribe", "product_ids": [HEARTBEAT_PRODUCT], "channels": ["heartbeat"]})
        if self._symbols:
            await self._send_subscribe(sorted(self._symbols))
        self._mark_ready()

    async def _send_subscribe(self, symbols: list[str]) -> None:
        await self._send({"type": "subscribe", "product_ids": symbols, "channels": ["ticker", "matches"]})

    async def _send_unsubscribe(self, symbols: list[str]) -> None:
        await self._send({"type": "unsubscribe", "product_ids": symbols, "channels": ["ticker", "matches"]})

    def _on_message(self, message) -> None:
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        try:
            if kind in ("ticker", "match", "last_match"):
                size = message.get("size") if kind == "match" else None
                self.emit(Tick(message["product_id"], parse_iso_ms(message["time"]), float(message["price"]),
                               float(size) if size is not None else None))
            elif kind == "subscriptions":
                self.emit(SourceStatus(self.data_state, "subscribed"))
            elif kind == "error":
                reason = str(message.get("reason") or message.get("message") or "")
                for symbol in self._symbols:
                    if symbol in reason:
                        self.emit(SymbolError(symbol, "market.unknown_symbol", "rejected by the provider"))
        except (KeyError, ValueError, TypeError):
            return
