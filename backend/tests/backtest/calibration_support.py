"""Support of the tests of the premium threshold calibration (SPEC-020, section 4.9).

THE REPO HAS NO HISTORY OF AN OPTION CHAIN. The only recorded chain in `backend/tests` is ONE day of made up quotes
(`options_fixtures/snapshots_opra_open.json`), which cannot make a history of hundreds of days. The pass path of the calibration therefore runs on
`synthetic_history`, a generator that lives ONLY in this test support and is never used by production code. It prices a chain with
Black-Scholes from a simulated underlying and sets the implied volatility from the volatility that FOLLOWS, which is what makes the premium rich
or fair by construction. A pass on it proves that the pipeline can open the switch; it says nothing about any real market. The default seed is one that passes. Seed 7
passes too; seeds 5 and 11 do not (the gate refuses them), so the generator does not promise a pass. The missing input is a
long recorded history of the real chain of an underlying (task 3304 or a provider that the student connects).

"""
from __future__ import annotations

import functools
import json
import math
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from hokma.backtest.calibration.premium import ChainDay, ChainHistory, OptionQuote

FIXTURES = Path(__file__).resolve().parents[1]


def bars_frame(days, closes, *, spread=0.004):
    """Daily bars on the days given, from closes: open at the previous close, a range of `spread` around them. Hand made, only for the shape."""
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    high = np.maximum(opens, closes) * (1 + spread)
    low = np.minimum(opens, closes) * (1 - spread)
    return pd.DataFrame({"open": opens, "high": high, "low": low, "close": closes}, index=pd.DatetimeIndex([pd.Timestamp(d) for d in days]))


def quote(expiration, kind, strike, bid, ask, iv=None):
    return OptionQuote(expiration, kind, Decimal(str(strike)), Decimal(str(bid)), Decimal(str(ask)), iv)


# ---- one real shaped day: the recorded snapshot of the repo ------------------------------------------------------------------

def alpaca_day() -> ChainDay:
    """The one recorded chain of the repo (AAPL on 2026-11-20, six expirations) as a ChainDay: bid and ask of the latest quote, implied volatility of the snapshot."""
    import re

    snaps = json.loads((FIXTURES / "options_fixtures" / "snapshots_opra_open.json").read_text(encoding="utf-8"))["snapshots"]
    quotes = []
    for symbol, snap in snaps.items():
        found = re.match(r"^[A-Z]+(\d{6})([CP])(\d{8})$", symbol)
        yymmdd, kind, strike = found.groups()
        expiration = date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
        q = snap["latestQuote"]
        quotes.append(OptionQuote(expiration, "call" if kind == "C" else "put", Decimal(int(strike)) / 1000, Decimal(str(q["bp"])), Decimal(str(q["ap"])),
                                  snap["impliedVolatility"]))
    return ChainDay(date(2026, 11, 20), Decimal("228.40"), tuple(quotes))


# ---- the synthetic history (TEST ONLY, see the top of the file) -----------------------------------------------------------------

def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(kind: str, spot: float, strike: float, vol: float, years: float) -> float:
    """Black-Scholes with zero rate and no dividend."""
    if years <= 0 or vol <= 0:
        return max(spot - strike, 0.0) if kind == "call" else max(strike - spot, 0.0)
    d1 = (math.log(spot / strike) + 0.5 * vol * vol * years) / (vol * math.sqrt(years))
    d2 = d1 - vol * math.sqrt(years)
    return spot * _cdf(d1) - strike * _cdf(d2) if kind == "call" else strike * _cdf(-d2) - spot * _cdf(-d1)


def third_fridays(first: date, last: date) -> list[date]:
    out, month = [], date(first.year, first.month, 1)
    while month <= last:
        d = month + timedelta(days=(4 - month.weekday()) % 7 + 14)
        out.append(d)
        month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    return out


def synthetic_history(*, n_days: int = 2400, seed: int = 3, rich: float = 2.5, fair: float = 0.7, block: int = 80, rich_share: float = 0.45,
                      drift: float = 0.0004, vol_of_vol: float = 0.06, start: date = date(2018, 1, 2)) -> ChainHistory:
    """A recorded looking chain history that is SYNTHETIC. The underlying is a simulated index with clustered volatility; the implied volatility of a day
    is the volatility of the NEXT 30 returns times `rich` in a rich block of `block` days and times `fair` in the others. Prices are Black-Scholes with a
    put skew, a spread of half a cent plus one percent of the mid on each side, third Friday expirations from 10 to 70 days, strikes every 5 inside 20 percent
    of the spot never leaves the chain: a worthless wing shows bid 0.00 and ask 0.01."""
    rng = np.random.default_rng(seed)
    days = [d.date() for d in pd.bdate_range(start, periods=n_days)]
    log_sigma, sigma = math.log(0.16), []
    for _ in range(n_days + 31):
        log_sigma = math.log(0.16) + 0.97 * (log_sigma - math.log(0.16)) + vol_of_vol * rng.standard_normal()
        sigma.append(min(max(math.exp(log_sigma), 0.08), 0.50))
    sigma_arr = np.array(sigma)
    rets = drift + sigma_arr / math.sqrt(252) * rng.standard_normal(n_days + 31)
    closes = 400.0 * np.exp(np.cumsum(rets[:n_days + 31]))
    forward = np.array([float(np.std(rets[i + 1:i + 31], ddof=1)) * math.sqrt(252) for i in range(n_days)])
    flags, i = [], 0
    while len(flags) < n_days:
        flags += [bool(rng.random() < rich_share)] * block
    expirations = third_fridays(days[0], days[-1] + timedelta(days=80))
    out = []
    for i, day in enumerate(days):
        spot = float(closes[i])
        atm = min(max(float(forward[i]) * (rich if flags[i] else fair), 0.08), 0.90)
        quotes = []
        base = int(round(spot / 5.0)) * 5
        for expiry in expirations:
            dte = (expiry - day).days
            if not 10 <= dte <= 70:
                continue
            for strike in range(base - 140, base + 141, 5):
                if abs(strike / spot - 1) > 0.20:
                    continue
                for kind in ("put", "call"):
                    skew = 1 + 0.8 * max(0.0, (spot - strike) / spot) if kind == "put" else max(0.6, 1 - 0.3 * max(0.0, (strike - spot) / spot))
                    vol = atm * skew
                    mid = bs_price(kind, spot, float(strike), vol, dte / 365.0)
                    half = 0.005 + 0.01 * mid
                    bid, ask = round(max(mid - half, 0.0), 2), max(round(mid + half, 2), 0.01)
                    quotes.append(OptionQuote(expiry, kind, Decimal(strike), Decimal(str(bid)), Decimal(str(ask)), vol))
        out.append(ChainDay(day, Decimal(str(round(spot, 2))), tuple(quotes)))
    spread = 0.004
    bars = bars_frame(days, [float(d.spot) for d in out], spread=spread)
    return ChainHistory("SPY", tuple(out), bars)


@functools.lru_cache(maxsize=None)
def rich_history() -> ChainHistory:
    """The synthetic history in which selling premium pays in the rich blocks (seed 3): the pass path."""
    return synthetic_history()


@functools.lru_cache(maxsize=None)
def fair_history() -> ChainHistory:
    """The synthetic history with no premium at all (the implied volatility is 0.9 times the realized one everywhere): the gate has to refuse it."""
    return synthetic_history(rich=0.9, fair=0.9)
