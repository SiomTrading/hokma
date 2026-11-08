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

## Providers

`backend/hokma/market/providers.json` lists the providers, their asset classes, limits, hosts and the `data_state`
each declares. It is editable without code: Finnhub is `live` there: its trade WebSocket is real time on the free plan for US stocks (the `/quote` REST call of the
same account answered seconds old, 6-nov). If `scripts/measure_providers.py` ever shows minutes of lag, set it back to `delayed`.
The history (bars before the first live trade) always comes from Yahoo and carries its own delay: the snapshot says it in `history`. The host of the SiomTrading futures stream is the `url`
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

## Not covered by the tests

Real WebSocket latency of the free Finnhub plan, and the drops and limits of the Coinbase feed need real accounts:
run `scripts/measure_providers.py` by hand. Databento and Massive are tested against fake servers only.
