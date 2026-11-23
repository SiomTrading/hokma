"""Is the implied volatility rich or cheap? Matched-expiry implied volatility against realized volatility, risk
reversal and butterfly (SPEC-019, B1; AFS-005).

The classification needs a threshold calibrated in a backtest (H4). Without one the answer is "uncalibrated": this module
never says "rich" or "cheap" on its own. An earnings date inside the option's life makes the comparison unreliable and is
reported, not corrected.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any

import numpy as np

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.options import _common as c
from hokma.quant.options.surface import otm_smile
from hokma.quant.types import QuantResult

WINDOWS = (20, 60, 90)
TRADING_DAYS = 252
MAX_CLOSES = 20_000
DELTA_WING = 0.25


def realized_close_to_close(closes: np.ndarray, window: int, periods: int = TRADING_DAYS) -> float | None:
    """Annualized close to close volatility of the last `window` log returns (sample standard deviation). None when there are too few closes."""
    if len(closes) < window + 1 or window < 2:
        return None
    rets = np.diff(np.log(closes[-(window + 1):]))
    return float(np.std(rets, ddof=1) * math.sqrt(periods))


def _call_delta(spot: float, strike: np.ndarray, rate: float, q: float, tte: float, iv: np.ndarray) -> np.ndarray:
    d1 = (np.log(spot / strike) + (rate - q + 0.5 * iv ** 2) * tte) / (iv * math.sqrt(tte))
    cdf = 0.5 * np.vectorize(math.erfc)(-d1 / math.sqrt(2.0))
    return math.exp(-q * tte) * cdf


def _interp_at(x: float, xs: np.ndarray, ys: np.ndarray) -> float | None:
    """Linear interpolation of ys at x for any ordering of xs; None outside the observed range."""
    order = np.argsort(xs)
    xs, ys = xs[order], ys[order]
    if x < xs[0] or x > xs[-1]:
        return None
    return float(np.interp(x, xs, ys))


def smile_metrics(sk: np.ndarray, iv: np.ndarray, spot: float, rate: float, q: float, tte: float) -> dict[str, float | None]:
    """ATM volatility (at the forward), 25 delta call and put volatilities, risk reversal and butterfly of one smile."""
    forward = spot * math.exp((rate - q) * tte)
    atm = _interp_at(forward, sk, iv)
    delta_call = _call_delta(spot, sk, rate, q, tte, iv)                    # call delta of every strike with its own volatility
    carry = math.exp(-q * tte)
    k_call = _interp_at(DELTA_WING, delta_call, sk)                         # strike where the call delta is 0.25
    k_put = _interp_at(carry - DELTA_WING, delta_call, sk)                  # strike where the put delta is -0.25 (put delta = call delta - exp(-qT))
    iv_call = None if k_call is None else _interp_at(k_call, sk, iv)
    iv_put = None if k_put is None else _interp_at(k_put, sk, iv)
    rr = None if iv_call is None or iv_put is None else iv_call - iv_put
    fly = None if iv_call is None or iv_put is None or atm is None else 0.5 * (iv_call + iv_put) - atm
    return {"atm_iv": atm, "iv_25d_call": iv_call, "iv_25d_put": iv_put, "strike_25d_call": k_call, "strike_25d_put": k_put,
            "risk_reversal_25d": rr, "butterfly_25d": fly, "forward": forward}


@registry.register("options.vol_richness", module="options",
                   summary="Compare the implied volatility of one expiry (at the forward) with close to close realized volatility over 20, 60 and 90 days, plus the 25 delta risk reversal (call minus put volatility) and butterfly (average wing minus ATM). Give the chain rows, spot, rate, dividend yield, as_of, the target expiration and either daily closes (oldest first) or realized_vol {20, 60, 90} from the stats module. The classification is 'uncalibrated' unless a calibration {metric_window, rich_ratio, cheap_ratio, backtest_run_ref} from a backtest is given; it is also 'uncalibrated' when an earnings date falls before the expiration, because the implied volatility then includes the earnings jump that realized volatility does not.",
                   inputs={"contracts": {"type": "list", "required": True}, "spot": {"type": "float", "required": True}, "rate": {"type": "float", "required": True},
                           "as_of": {"type": "str", "required": True}, "expiration": {"type": "str", "required": True},
                           "dividend_yield": {"type": "float", "required": False, "default": 0.0},
                           "closes": {"type": "list", "required": False, "default": None}, "realized_vol": {"type": "dict", "required": False, "default": None},
                           "earnings_date": {"type": "str", "required": False, "default": None}, "calibration": {"type": "dict", "required": False, "default": None}},
                   piece_ids=("AFS-005",))
def vol_richness(contracts: list, spot: float, rate: float, as_of: Any, expiration: Any, dividend_yield: float = 0.0, closes: list | None = None,
                 realized_vol: dict | None = None, earnings_date: Any = None, calibration: dict | None = None) -> QuantResult:
    name, unit, method, pieces = "options.vol_richness", "vol", "matched_expiry_iv_vs_realized", ("AFS-005",)
    rows = c.contracts_list(contracts)
    s, r, q = c.num("spot", spot), c.num("rate", rate), c.num("dividend_yield", dividend_yield)
    if closes is not None and (not isinstance(closes, (list, tuple)) or len(closes) > MAX_CLOSES):
        raise BadInput(f"closes must be a list of at most {MAX_CLOSES} prices")
    if realized_vol is not None and not isinstance(realized_vol, dict):
        raise BadInput("realized_vol must be a dict of window to annualized volatility")
    if calibration is not None and not isinstance(calibration, dict):
        raise BadInput("calibration must be a dict")
    px = np.array([c.num("closes[]", x) for x in closes], dtype=float) if closes is not None else None
    params = {"spot": s, "rate": r, "dividend_yield": q, "as_of": str(as_of), "expiration": str(expiration), "n_contracts": len(rows),
              "n_closes": None if px is None else int(len(px)), "realized_vol": realized_vol, "earnings_date": None if earnings_date is None else str(earnings_date),
              "calibration": calibration}
    problem = c.bs_domain(s, s, r, 0.2, 1.0, q)
    if problem:
        return c.empty(name, unit, method, problem, params=params, piece_ids=pieces)
    t = c.years_between(as_of, expiration)
    if t is None or t <= 0:
        return c.empty(name, unit, method, "as_of or expiration cannot be read, or the option has expired", params=params, piece_ids=pieces)
    if px is not None and (not np.isfinite(px).all() or (px <= 0).any()):
        return c.empty(name, unit, method, "closes must be finite positive prices", params=params, piece_ids=pieces)
    if px is None and realized_vol is None:
        return c.empty(name, unit, method, "give closes or realized_vol to compare against", params=params, piece_ids=pieces)
    exp_key = str(expiration)[:10]
    group = [row for row in rows if str(row.get("expiration"))[:10] == exp_key]
    if not group:
        return c.empty(name, unit, method, f"the chain has no rows for the expiration {exp_key}", params=params, piece_ids=pieces)
    sk, _, iv = otm_smile(group, s, r, q, t)
    if len(sk) < 3:
        return c.empty(name, unit, method, f"only {len(sk)} usable out-of-the-money quote(s) at {exp_key}: a smile needs at least 3", params=params, piece_ids=pieces)

    warns: list[str] = []

    def compute() -> dict:
        metrics = smile_metrics(sk, iv, s, r, q, t)
        if metrics["atm_iv"] is None:
            raise ValueError("the forward is outside the quoted strikes: no at-the-money volatility")
        for key in ("risk_reversal_25d", "butterfly_25d"):
            if metrics[key] is None:
                warns.append(f"{key} is unavailable: the quoted strikes do not reach the 25 delta wings")
        realized: dict[str, float | None] = {}
        for w in WINDOWS:
            if realized_vol is not None and (w in realized_vol or str(w) in realized_vol):
                raw = realized_vol.get(w, realized_vol.get(str(w)))
                val = c.num(f"realized_vol[{w}]", raw)
                realized[str(w)] = val if math.isfinite(val) and val > 0 else None
            else:
                realized[str(w)] = realized_close_to_close(px, w) if px is not None else None
            if realized[str(w)] is None:
                warns.append(f"realized volatility over {w} days is unavailable: not enough valid closes")
        ratios = {k: (metrics["atm_iv"] / v if v else None) for k, v in realized.items()}
        spreads = {k: (metrics["atm_iv"] - v if v else None) for k, v in realized.items()}
        earn = None
        if earnings_date is not None:
            try:
                earn = date.fromisoformat(str(earnings_date)[:10])
            except ValueError:
                raise ValueError("earnings_date must be an ISO date") from None
        start = as_of.date() if hasattr(as_of, "date") else date.fromisoformat(str(as_of)[:10])
        in_window = earn is not None and start <= earn <= date.fromisoformat(exp_key)
        label, reason = "uncalibrated", "no threshold calibrated in a backtest was given"
        if calibration:
            window = str(calibration.get("metric_window", 60))
            rich, cheap, ref = calibration.get("rich_ratio"), calibration.get("cheap_ratio"), calibration.get("backtest_run_ref")
            ratio = ratios.get(window)
            valid = (ref not in (None, "") and isinstance(rich, (int, float)) and isinstance(cheap, (int, float)) and not isinstance(rich, bool)
                     and not isinstance(cheap, bool) and 0 < cheap <= rich and ratio is not None)
            if not valid:
                reason = "the calibration is incomplete (needs rich_ratio >= cheap_ratio > 0, backtest_run_ref and a realized window): not used"
            elif in_window:
                reason = "an earnings date falls before the expiration: the implied volatility includes the earnings jump, so it is not compared"
            else:
                label = "rich" if ratio > rich else ("cheap" if ratio < cheap else "fair")
                reason = f"ratio of {window} day realized volatility against calibration {ref}"
        if in_window:
            warns.append("an earnings date falls before the expiration: implied volatility includes the jump, realized volatility does not")
        return {"matched_expiration": exp_key, "time_to_expiration": t, **metrics, "realized_vol": realized, "iv_over_realized": ratios,
                "iv_minus_realized": spreads, "earnings_in_window": bool(in_window), "classification": label, "classification_reason": reason}

    return _finish(name, unit, method, pieces, params, compute, warns)


def _finish(name: str, unit: str, method: str, pieces: tuple[str, ...], params: dict, compute: Any, warns: list[str]) -> QuantResult:
    """Run compute, then attach the warnings it collected while running."""
    result = c.guarded(name, unit, method, pieces, params, compute)
    return QuantResult(name=result.name, value=result.value, unit=result.unit, method=result.method, params=result.params,
                       inputs_hash=result.inputs_hash, as_of=result.as_of, warnings=result.warnings + tuple(warns), piece_ids=result.piece_ids)
