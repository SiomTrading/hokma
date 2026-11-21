"""Builds the JSON files of this folder. Run: python make_fixtures.py (standard library only).

The shapes are the ones of Alpaca's OpenAPI documents (docs.alpaca.markets/openapi: trading-api.json and
market-data-api.json, 20-nov-2026). The numbers are made up with Black-Scholes so the chain behaves like a real one
(AAPL near 228.40). No key and no account data are in any file. Replace them with recordings from a demo account
when one is available: the tests only depend on the shape and on the symbols listed in test_options_chain.py.
"""
import json
import math
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
SPOT = 228.40
TODAY = date(2026, 11, 20)
EXPIRATIONS = ["2026-11-20", "2026-12-18", "2027-01-15", "2027-01-29", "2027-02-19", "2027-03-19"]
STRIKES = [190 + 5 * i for i in range(17)]            # 190 .. 270
ROOT = "AAPL"


def cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def pdf(x):
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def occ(exp, kind, strike):
    d = date.fromisoformat(exp)
    return f"{ROOT}{d:%y%m%d}{'C' if kind == 'call' else 'P'}{int(strike * 1000):08d}"


def iv_of(exp, strike):
    days = (date.fromisoformat(exp) - TODAY).days
    return round(0.24 + 0.00015 * min(days, 120) + 0.0009 * abs(strike - SPOT) / 5 ** 0.5, 4)


def bs(kind, strike, days, iv):
    t = max(days, 0.4) / 365
    d1 = (math.log(SPOT / strike) + iv * iv * t / 2) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    call = SPOT * cdf(d1) - strike * cdf(d2)
    price = call if kind == "call" else call - SPOT + strike
    delta = cdf(d1) if kind == "call" else cdf(d1) - 1
    gamma = pdf(d1) / (SPOT * iv * math.sqrt(t))
    vega = SPOT * pdf(d1) * math.sqrt(t) / 100
    theta = -(SPOT * pdf(d1) * iv) / (2 * math.sqrt(t)) / 365
    return max(price, 0.0), delta, gamma, theta, vega


def contracts():
    out = []
    for exp in EXPIRATIONS:
        for kind in ("call", "put"):
            for strike in STRIKES:
                days = (date.fromisoformat(exp) - TODAY).days
                near = max(0.0, 1 - abs(strike - SPOT) / 40)
                interest = int(round(9000 * near ** 2 + 40)) + (days % 7) * 11
                if days <= 7:
                    interest = interest // 4
                out.append({
                    "id": f"00000000-0000-4000-8000-{len(out):012d}", "symbol": occ(exp, kind, strike),
                    "name": f"AAPL {date.fromisoformat(exp):%b %d %Y} {strike} {kind.title()}", "status": "active",
                    "tradable": True, "expiration_date": exp, "root_symbol": ROOT, "underlying_symbol": ROOT,
                    "underlying_asset_id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415", "type": kind, "style": "american",
                    "strike_price": f"{strike}", "multiplier": "100", "size": "100", "open_interest": str(interest),
                    "open_interest_date": "2026-11-19", "close_price": "1.25", "close_price_date": "2026-11-19",
                    "ppind": True})
    return out


def snapshots(quote_time, trade_time, volume_scale, with_exchange):
    out = {}
    for c in contracts():
        exp, kind, strike = c["expiration_date"], c["type"], float(c["strike_price"])
        days = (date.fromisoformat(exp) - TODAY).days
        iv = iv_of(exp, strike)
        price, delta, gamma, theta, vega = bs(kind, strike, days, iv)
        half = max(0.01, price * (0.012 + 0.0025 * abs(strike - SPOT) / 5))
        bid, ask = round(max(price - half, 0), 2), round(price + half, 2)
        if price < 0.08 and days <= 28:
            bid = 0.0
        quote = {"ap": ask, "as": 20 + int(strike) % 37, "bp": bid, "bs": 12 + int(strike) % 29, "c": "A",
                 "t": quote_time}
        trade = {"c": "@", "p": round(price, 2), "s": 3, "t": trade_time}
        if with_exchange:
            quote.update({"ax": "X", "bx": "C"})
            trade["x"] = "N"
        snap = {"dailyBar": {"c": round(price, 2), "h": round(price * 1.04, 2), "l": round(price * 0.96, 2), "n": 40,
                             "o": round(price, 2), "t": "2026-11-20T05:00:00Z",
                             "v": int(volume_scale * (1 + abs(strike - SPOT) % 17)), "vw": round(price, 2)},
                "greeks": {"delta": round(delta, 4), "gamma": round(gamma, 4), "rho": 0.01, "theta": round(theta, 4),
                           "vega": round(vega, 4)},
                "impliedVolatility": iv, "latestQuote": quote, "latestTrade": trade}
        out[c["symbol"]] = snap
    return out


def write(name, data):
    (HERE / name).write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")


rows = contracts()
write("contracts_page1.json", {"option_contracts": rows[:100], "next_page_token": "tok-page-2"})
write("contracts_page2.json", {"option_contracts": rows[100:], "next_page_token": None})
write("snapshots_indicative_open.json", {"snapshots": snapshots("2026-11-20T16:00:01Z", "2026-11-20T15:59:30Z", 12, False),
                                         "next_page_token": None})
write("snapshots_indicative_closed.json", {"snapshots": snapshots("2026-11-20T20:59:58Z", "2026-11-20T20:58:12Z", 9, False),
                                           "next_page_token": None})
write("snapshots_opra_open.json", {"snapshots": snapshots("2026-11-20T16:00:01Z", "2026-11-20T16:00:00Z", 12, True),
                                   "next_page_token": None})
write("opra_403.json", {"code": 40310000, "message": "subscription does not permit querying recent SIP data"})
write("stock_snapshot.json", {"symbol": ROOT, "latestTrade": {"t": "2026-11-20T20:59:59Z", "x": "V", "p": SPOT, "s": 100, "c": ["@"], "i": 1, "z": "C"},
                               "latestQuote": {"t": "2026-11-20T20:59:58Z", "ax": "Q", "ap": 228.46, "as": 3, "bx": "P", "bp": 228.33, "bs": 4, "c": ["R"], "z": "C"},
                               "minuteBar": {"t": "2026-11-20T20:59:00Z", "o": 228.4, "h": 228.41, "l": 228.39, "c": 228.4, "v": 1200, "n": 20, "vw": 228.4},
                               "dailyBar": {"t": "2026-11-20T05:00:00Z", "o": 229.05, "h": 229.9, "l": 227.8, "c": 228.4, "v": 731132, "n": 5000, "vw": 228.7},
                               "prevDailyBar": {"t": "2026-11-19T05:00:00Z", "o": 228.0, "h": 229.0, "l": 227.0, "c": 229.05, "v": 900000, "n": 6000, "vw": 228.3}})
print(len(rows), "contracts")
