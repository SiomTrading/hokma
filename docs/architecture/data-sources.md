# Data sources and storage

What the data module reads today, where each source is read, and where results are kept. Everything here describes the code as it is.

## Rules that hold for every source

- Every call takes an `as_of` cut. Nothing dated after it is returned.
- Every call returns one result type: `value`, `source`, `received_at` (when this process received the data, not when it was published), `as_of`, `status` and `reason`.
- `status` is `ok`, `NO_DATA` (the source answered and has nothing), `DATA_UNAVAILABLE` (the source could not be asked or did not answer) or `STALE` (the data is too old to use). A result that is not `ok` carries no value and says why.
- A gap is never filled and a failure is never presented as an empty answer.
- Provider keys come from the connections store. A key never enters a result, a log line or a fixture.
- Each data or quantitative library is imported in the `_lib.py` of its module only (for data, `backend/hokma/data/_lib.py`). The other files call the functions of that file.

## Sources

| Need | Module | Source and how it is read |
|---|---|---|
| Bars | `data/ohlcv.py` | The market module of the application, the same origin as the chart. One call per request, no polling. Only closed bars up to `as_of`. |
| Fundamentals | `data/fundamentals_src.py` | Records selected by filing date, never by the end of the fiscal period. |
| SEC filings and facts | `data/sec/` | The SEC EDGAR service, read with the identity the owner saved in Connections. See the next section. |
| Macro series | `data/macro.py` | The macro provider with the real-time window pinned to `as_of`, so a number is the one published that day. |
| Positioning (weekly) | `data/cot.py` | The paid provider that delivers it, declared as the source. Without its key the answer is `DATA_UNAVAILABLE` with the reason `provider_key_missing`. |
| News | `data/news.py` | Headline feeds cut to the window. A window the feed cannot have observed is `DATA_UNAVAILABLE`. |
| Social and prediction markets | `data/social.py` | Feeds that keep no history: not served for a past `as_of`. |
| Instrument universe | `data/universe.py` | Seven compressed catalog files, one per asset class, from a local folder or a URL. Describes instruments as they are now. |
| Ticks | `data/ticks.py` | Tick data given by the caller: cleaning, bars and trade classification. |

## SEC EDGAR (`backend/hokma/data/sec/`)

Every function takes a `SecClient` and an `as_of` and returns the common result.

| File | Serves |
|---|---|
| `client.py`, `transport.py`, `identity.py` | The client holds the transport, the clock and small caches. The live transport is the only code that makes network calls; every failure becomes a `TransportError` with a kind (identity, rate_limited, not_found, network). Without a saved identity the answer is `DATA_UNAVAILABLE` with the reason `sec_identity_missing`. |
| `index.py` | The filing index of a company and the point in time rule: a filing is visible at `as_of` when the SEC had accepted it by then. When both sides carry a time, the acceptance time decides; otherwise the filing date. |
| `reference.py` | Ticker to CIK (case insensitive, `BRK-B` equals `BRK.B`), the form catalog and form families. |
| `filings.py` | Typed 8-K, periodic reports (10-K, 10-Q, 20-F, 40-F, 6-K), offerings (Form D, S-1, S-3, S-4, 424B, DRS, Form C, EFFECT), the auditor, the subsidiaries (EX-21), the HTML document parser and the fundamentals as they were filed. |
| `financials.py` | Statement fields, ratios, year over year growth and trailing twelve months from the company facts. Every figure uses facts filed by `as_of`. A ratio never mixes periods: operands must share the end date (and the start date when both are durations), otherwise the ratio is empty with a warning. A trailing twelve months window needs four consecutive quarters and must end within 185 days of `as_of`; otherwise the answer is `NO_DATA`. |
| `xbrl.py` | Instance parsing and queries, readers of the instance and the presentation and calculation linkbases, standardization with a registry of every concept it could not map, multi period stitching that reports restatements, validation and deduplication. |
| `ownership.py` | Forms 3, 4 and 5, Form 144 and Schedules 13D and 13G. Owner names stay as written in the filing; parsing never looks anything up on the network. |
| `thirteenf.py` | Holdings reports (13F-HR): positions, comparison between quarters, history, the unit of the values (a warning when the evidence is ambiguous) and the signer role. |
| `funds.py` | N-PORT, N-MFP3, N-CEN, N-CSR, 497K, schedule of investments of business development companies (with non accrual status and industry), loan level asset data, ATS-N notices, N-PX votes and proxy statements. |
| `live.py` | The live feed of new filings, the latest filings of a company and alerts for filings that appeared since a moment. A feed older than 36 hours is `STALE`. |
| `search.py` | Full text search across filings (window capped at `as_of`, later hits dropped) and text search inside one filing. |
| `storage.py` | A filing store with a checksum for every filing and a transport that reads through it. |

## Where results are kept

| What | Where | Rule |
|---|---|---|
| Results of data calls | One SQLite file (`data/cache.py`) with a table of entries and a table of coverage | Only successful results are stored. A hit never returns an entry whose `as_of` is later than the cut asked for. Coverage records which windows a source observed, so a window outside it is reported as a gap. |
| SEC submission texts | Files under `<cik>/<accession>.txt` with `<accession>.meta.json` (`data/sec/storage.py`) | The record holds the sha256 and the size. A copy that does not match is treated as missing and never returned. Writes go through a temporary file, so a reader never sees half a file. Failures are never stored. |
| Other blob stores | The `BlobBackend` protocol (put, get, exists, keys) | A bucket in the cloud plugs in by implementing the protocol; callers do not change. |

There is no time series database in the data module. Bars come from the market module, and results are cached in SQLite.
