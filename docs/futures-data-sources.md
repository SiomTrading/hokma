# Where the data of futures and indices comes from

Written for the owner of the fund: what each part of a desk reads, in which order, and what name the screen shows.
Verified in the code of this branch (`backend/hokma/market`, `backend/hokma/kit/market_tools.py`, `backend/hokma/data/ohlcv.py`), not by a live Schwab call.

| What you see | Symbol | Code path | Source when Schwab is connected and signed in | Source without Schwab | Shown on screen as |
|---|---|---|---|---|---|
| Live price in the list of assets, live price of the chart | `NQ=F`, `ES=F`, `MNQ=F`... | `MarketHub` (`/ws/market`), asset class `future`, order `schwab`, `siomtrading_futures`; `/NQ` through `symbols.to_provider` | Schwab streamer (LEVELONE_FUTURES, CHART_FUTURES). SiomTrading Futures is switched off while a broker serves futures (SPEC-016 3.10) | SiomTrading Futures (needs its key), else Yahoo polled every 20 s, 10 minutes delayed | Source name in each message; the list shows it under the price, the chart footer shows "price from ..." |
| Candles of the chart, previous close, change | `NQ=F`... | `ConnectedSource.history` (`market/history.py`) | Schwab `pricehistory` with `/NQ` (new in this branch). If Schwab has no bars: Yahoo | Yahoo (`NQ=F`), 10 minutes delayed | Chart footer: "candles from X, price from Y" |
| Quote and candles for the agents (`market_quote`, `market_candles`, `market_desk_prices`) | same | same `build_chart` as the chart, restricted to the providers the desk uses | same as the candles row | Yahoo | `source` field of every answer |
| `data_bars`, `data_snapshot` | same | `data/ohlcv.market_fetcher` calls `market.api.get_source().history`: the same chain, NOT filtered by desk | same as the candles row | Yahoo | `meta.market_source` |
| Index (`^GSPC`, `^DJI`, `^IXIC`) live price | `^...` | `symbols.classify` gives no class: no provider streams it, the hub falls back to Yahoo polling, 15 minutes delayed | Yahoo | Yahoo | "Yahoo Finance", delayed |
| Index candles | `^...` | no class, so `providers_for` is empty | Yahoo | Yahoo | "Yahoo Finance" |
| `/ES` typed by hand | `/ES` | `valid_symbol` refuses `/`: 400 `bad_symbol`. The symbol of a future in Hokma is `ES=F`; Schwab's `/ES` is made inside `to_provider` | n/a | n/a | n/a |

## Schwab and futures: what is verified and what is not

* Verified in the code: the streamer has `LEVELONE_FUTURES` and `CHART_FUTURES`; the root `/NQ` and an exact contract `/NQZ26` are built by `symbols.py`; `/quotes` accepts futures (`/ESH23`).
* Not verified: `GET /marketdata/v1/pricehistory`. The Schwab reference documents its `symbol` as a stock. This branch asks it for `/NQ` anyway. If Schwab answers 400, 403, 404, empty or an error, Yahoo serves the candles and the screen says "Yahoo Finance". A futures refusal never closes Schwab for stocks.
* To confirm with a real session (a human step, one call): `schwab_price_history` with `symbol=/NQ`, `frequencyType=minute`, `frequency=5`. If it answers bars, nothing else is needed. If not, the fix is a futures candle store fed by the CHART_FUTURES stream (see the recommendation in the task 3347 report).
* Indices: not part of this change. Mapping `^GSPC` to `$SPX` needs a new asset class `index` in the hub, the catalog and the connections order; it is a separate task.
