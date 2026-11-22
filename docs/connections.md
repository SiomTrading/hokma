# Connections and the live price channel

All data connections are configured in one place: the Connections screen (`#/connections`) and the endpoints under
`/api/connections`. There is no key or provider setting anywhere else (Settings, first run, chat, CLI).

## Keys

- A key is saved with `keyring` in the system credential store (Windows Credential Manager, macOS Keychain, Secret
  Service on Linux), service `hokma-market`, one entry per provider. Alpaca keeps its Key ID and Secret in one entry
  (a small JSON text).
- With no usable store the API answers `connections.store_unavailable` ("System credential store not available") and
  saves nothing. There is no file or environment fallback.
- `<data>/connections.json` holds only which providers are on and the priority order.
- A key never reaches the panel after it is saved, an agent, the environment of the `claude` process, a log line or a
  SiomTrading server. The `secrets` protection blocks reading the store (the `keyring` library, `secret-tool`,
  Secret Service on D-Bus, `security`, `cmdkey`, the keyring and wallet files).

## Live data: one source per user, always a stream (1.0.10, SPEC-016)

Everything that shows a price receives it by streaming and paints it as it arrives. Nothing polls: a source without streaming is not a live source,
and a test of the adapter registry fails if one says `streaming = False`. Yahoo is the only exception and it is never presented as live: it stays
the delayed history and fallback of a user with no broker ("Con retraso"), exactly as in 1.0.9.

| The user has | Live data of stocks, ETFs and options |
|---|---|
| Schwab connected | Only Schwab, by its Streamer (`schwab/stream.py`, one WebSocket per installation). Alpaca receives no subscription and no call |
| A second broker connected | Only the one chosen in Connections ("data source"); by default the first one that connected |
| No broker | Alpaca, labelled "Alpaca Trading", for tests and demo orders |

Crypto is always Coinbase. Forex and news are Finnhub: Finnhub no longer serves stocks, ETFs or options. Futures: Schwab when it is connected (services
`LEVELONE_FUTURES` and `CHART_FUTURES`, key `/NQ` or an exact contract `/NQZ26`, rollover by field 34); an account without futures data loses only that
symbol and says so; with no Schwab, SiomTrading Futures. Never both, never a silent move. Interactive Brokers has its entry (a `broker`, off, type
`gateway`) and no adapter yet: its adapter is its own spec (I1, the TWS protocol, is not verified).
Alpaca option quotes (no broker) come by its msgpack stream (`wss://stream.data.alpaca.markets/v1beta1/indicative`, bid and ask only, no greeks, 200
quotes on the free plan); the field names of a quote are assumed from the usual Alpaca message. The calendar of Alpaca (`GET /v2/calendar`, one function in
`market/alpaca_hours.py`) is NOT verified against its documentation: if it fails the chart has no shading and open or closed is "unknown".
Coinbase and Finnhub build the candle in progress from trades; when it closes, the candle of the source replaces it (one read per closed candle). The role of each provider (`broker` or `free`) is in `providers.json`; no provider name is in the routing.

There is **no silent fallback**. If the broker stops, the symbols stay where they were, stopped, with the broker's name; no other source answers. The
panel gets a `broker_alert` after 3 seconds, the owner gets one notice (Telegram, or mail if they turned it on) after 30 seconds, never more than one
every 10 minutes per broker, with the market closed too, and a recovery notice. The candles come only from the broker while it is connected.

What the Schwab stream gives (guide of the Streamer, section 7): `LEVELONE_EQUITIES` (price and fields of the day: Schwab sends only what changed, Hokma
keeps the last value of each field), `CHART_EQUITY` (1 minute candles), `LEVELONE_OPTIONS` (the chain moves cell by cell, strikes around the price of
the stream, contracts on screen plus a margin). The day candle in progress is the one of the source (open, high, low, volume; the close is the last
price of the regular session); the closed ones are those of `pricehistory`. Intervals Schwab does not give are folded on the clock. Hours and sessions
(pre, regular, post, night) come from `GET /markets`, never from a table of ours; a source with no hours says "unknown".
Seconds and range charts need trade by trade, which Schwab does not have: they are disabled with it.

The values that only the real Streamer can tell (heartbeat, limit of symbols, whether the minute candle is in progress, night session, token expiry
on an open session) are in `providers.json` under `stream` and are measured with **Connections, Schwab menu, "Check Schwab data"**: it saves a file with
the raw answers and the Streamer messages, without any token or id, and sends no order.

## Providers

`backend/hokma/market/providers.json` lists the providers, their asset classes, limits, hosts and the `data_state`
each declares. It is editable without code: Finnhub is `live` there: its trade WebSocket is real time on the free plan (forex since 1.0.10; the `/quote` REST call of the
same account answered seconds old, 6-nov). If `scripts/measure_providers.py` ever shows minutes of lag, set it back to `delayed`.
The history (bars before the first live trade) comes from the broker alone while one is connected, else from Alpaca (stocks) and Coinbase (crypto), Finnhub candles for forex if the plan has them (`market/history.py`), and from Yahoo as the last fallback of a user with no broker. A provider that fails, has no access (403) or is rate limited is skipped. A desk chart only uses the connections of its desk (`desk` in the subscribe message). The snapshot says the real source and its delay in `history`. SiomTrading Futures has no bars endpoint yet: it serves the live price only. The host of the SiomTrading futures stream is the `url`
of `siomtrading_futures`.

A new provider is an adapter (`backend/hokma/market/adapters/`) registered with `register_adapter(id, factory)` plus an
entry in `providers.json` and a symbol translation in `market/symbols.py`.

## SiomTrading Futures

The adapter is `backend/hokma/market/adapters/siomtrading_futures.py`. The address is `wss://{host}/v1/stream/futures`
and the host is fixed by the package in `providers.json` (`siomtrading_futures.url`); the owner never types it.

**The shipped host, `stream.siomtrading.com`, is a placeholder.** SiomTrading must confirm the real host before the
release is published; changing it is a one-line edit of `providers.json`, with no code change. The adapter refuses any
address that is not `wss://` (a plain `ws://` is accepted only on the loopback interface, for tests).

- The owner pastes an access token on the card (service `hokma-market`, entry `siomtrading_futures`, like every key).
  The token travels only in the first message: `{"type":"hello","token":...,"client":"hokma/0.0.6","protocol":1}`.
  It is never in the URL, a header, a log line, a message to the panel, a file under `<data>` or the environment of
  `claude`.
- The client sends `subscribe` / `unsubscribe` with the root (`NQ`, the server resolves the current contract) or an
  exact contract, and a `ping` every 15 s. The server sends `welcome` (limit, `data_state`, instruments),
  `subscribed`, `tick`, `bar`, `rollover`, `status`, `error` and `pong`. The data state of `welcome` is the one the
  panel shows; the value in `providers.json` only holds until the server speaks.
- Close codes: 1001 and 1011 reconnect after 1, 2, 4 ... 30 s. 1008 and 4403 are final until a new token is pasted
  ("Token not valid" / "Access revoked"). 4409 is final ("Connected from another device"). 4429 waits for the
  `retry_after_ms` of the previous `error`, never less than 30 s.
- On `rollover` the hub restarts the candle building of that symbol and sends the panel a new `snapshot`.
- `symbol.unknown`, `symbol.not_entitled` and `limit.symbols` refuse that one symbol (`market.unknown_symbol`,
  `market.not_entitled`, `market.limit`); the others keep streaming.

Tests: `backend/tests/futures_fake_server.py` is a fake server that speaks the whole contract and injects faults;
`backend/tests/test_futures_adapter.py` runs the adapter against it (tests F1 to F7). The real endpoint on the
SiomTrading side does not exist yet; the day it does, the owner pastes a token and it works with no new version.

## Routing

The hub picks the provider of a symbol by its asset class (`equity`, `crypto`, `forex`, `future`) and the priority of
Connections, among the providers that are on, have their key and are not in error. With none, the symbol is served by
the delayed fallback (Yahoo, polled from the backend) and the snapshot says `fallback: true`. When a provider is at
its symbol limit the next one with room takes the symbol; if every candidate is full the answer is `market.limit`.
A change of priority applies to the next subscription; open ones stay where they are until their provider is switched
off, loses its key or fails for good, and then they move.

## Option chain (Alpaca)

With the Alpaca IEX key already saved, `market/options.py` reads the option chain of a US stock or ETF: contracts and open
interest from `GET /v2/options/contracts` (demo key `PK...` on `paper-api.alpaca.markets`, real key `AK...` on
`api.alpaca.markets`, read only, that one request) and price, greeks and IV from `GET /v1beta1/options/snapshots/{symbol}`
on `data.alpaca.markets` (`opra` first; a 403 falls back to `indicative` for an hour). It serves the `options_chain` tool
of the agents and the three routes `/api/market/options/{underlying,expirations,chain}` of the Options view of the chart.
The free plan gives indicative prices and estimated greeks, and the answers say so.

## Not covered by the tests

Real WebSocket latency of the free Finnhub plan, and the drops and limits of the Coinbase feed need real accounts:
run `scripts/measure_providers.py` by hand.
