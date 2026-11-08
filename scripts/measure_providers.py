"""Measure the real data providers with real, free accounts (task 5.7 of the 0.0.6 scope).

    uv run python scripts/measure_providers.py --provider alpaca --minutes 10
    uv run python scripts/measure_providers.py --provider finnhub --minutes 10 --symbol AAPL
    uv run python scripts/measure_providers.py --provider coinbase --minutes 60

It is NOT part of the test suite and does nothing by itself: run it by hand, on a developer machine, during market
hours for the stock providers. The keys are read from the system credential store (service `hokma-market`, the
entries Connections saves), so save them first in the Connections screen. Nothing is printed that could contain a key.

What it reports for each provider, as one JSON object:

- `first_message_ms`    time from the subscription to the first trade
- `messages`, `per_minute`   how many trades arrived in the window and at what rate
- `lag_ms` p50 / p95 / max   the provider's own trade time against this machine's clock (clock skew counts: run it
                        on a machine with synced time). A free plan that is delayed shows minutes here; a live
                        one shows well under a second to a few seconds
- `looks_live`          true when the p95 lag is under 5 seconds. This is what decides `data_state` for Finnhub in
                        `backend/hokma/market/providers.json`
- `reconnects`, `longest_gap_s`, `status_log`   drops of the stream and the longest silence (the Coinbase question:
                        how often the public feed drops from your location, and what limits it applies)

Missing so far (no accounts were available when the adapters were written): the numbers for Alpaca IEX, Finnhub and
the Coinbase feed from Colombia. Paste the output into the pull request of the Connections work and set
`data_state` for Finnhub accordingly.
"""
import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from hokma.connections.store import KeyStore  # noqa: E402
from hokma.market import symbols as symbol_map  # noqa: E402
from hokma.market.adapters import ADAPTERS  # noqa: E402
from hokma.market.providers import get_catalog  # noqa: E402
from hokma.market.stream import BarUpdate, SourceStatus, Tick  # noqa: E402

DEFAULT_SYMBOL = {"coinbase": "BTC-USD", "alpaca": "SPY", "finnhub": "AAPL", "databento": "NQ=F", "massive": "NQ=F"}


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))] if ordered else float("nan")


async def measure(provider_id: str, symbol: str, minutes: float) -> dict:
    spec = get_catalog().providers[provider_id]
    secret = KeyStore().get(provider_id) if spec.needs_key else None
    if spec.needs_key and not secret:
        raise SystemExit(f"No key saved for {provider_id}. Save it in the Connections screen first.")
    asset = symbol_map.classify(symbol)
    upstream = symbol_map.to_provider(provider_id, symbol, asset)
    if upstream is None:
        raise SystemExit(f"{provider_id} has no translation for {symbol}")
    source = ADAPTERS[provider_id](spec, secret, None)
    lags: list[float] = []
    arrivals: list[float] = []
    log: list[str] = []
    reconnects = 0
    started = time.monotonic()
    first_ms = None
    await source.connect()
    await source.subscribe([upstream])
    deadline = started + minutes * 60
    events = source.events()
    try:
        while time.monotonic() < deadline:
            try:
                event = await asyncio.wait_for(events.__anext__(), timeout=max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            now = time.monotonic()
            if isinstance(event, SourceStatus):
                log.append(f"{now - started:.1f}s {event.state} {event.code}")
                reconnects += event.state == "reconnecting"
            elif isinstance(event, (Tick, BarUpdate)):
                if first_ms is None:
                    first_ms = int((now - started) * 1000)
                arrivals.append(now)
                stamp = event.ts_ms if isinstance(event, Tick) else event.start_ms
                lags.append(time.time() * 1000 - stamp)
    finally:
        await source.close()
    gaps = [b - a for a, b in zip(arrivals, arrivals[1:])]
    p95 = percentile(lags, 0.95)
    return {
        "provider": provider_id, "symbol": symbol, "minutes": minutes, "first_message_ms": first_ms,
        "messages": len(arrivals), "per_minute": round(len(arrivals) / minutes, 1),
        "lag_ms": {"p50": round(statistics.median(lags), 1) if lags else None, "p95": round(p95, 1) if lags else None,
                   "max": round(max(lags), 1) if lags else None},
        "looks_live": bool(lags) and p95 < 5000, "declared_data_state": spec.data_state, "reconnects": reconnects,
        "longest_gap_s": round(max(gaps), 1) if gaps else None, "status_log": log[-30:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--provider", required=True, choices=sorted(DEFAULT_SYMBOL))
    parser.add_argument("--symbol", help="chart symbol, for example AAPL, BTC-USD or NQ=F")
    parser.add_argument("--minutes", type=float, default=10.0)
    args = parser.parse_args()
    result = asyncio.run(measure(args.provider, args.symbol or DEFAULT_SYMBOL[args.provider], args.minutes))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
