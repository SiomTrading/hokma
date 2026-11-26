"""Strategies of the panel of the same name (SPEC-028, delivery 5, piece 3338-A): the views of the sketch in the words of the code, the
strikes of the real chain that fit a budget, and the probability of profit.

Everything here is arithmetic over the chain rows that the caller hands in (the rows of one expiration, as hokma.market.options gives them:
{"strike", "call": {bid, ask, iv, delta, open_interest, volume} or None, "put": ...}). Nothing here reads a price source, a database or a model:
the live chain, the premium switch, the shares of the account and the verdict of the options seat are the business of hokma.desks.strategies.

The probability of profit is the one the chain shows for being in the money, Black-Scholes N(d2) with the implied volatility of the contract and the
rate of the chain panel (hokma.market.options.prob_itm and contract_prob_itm), applied to the breakevens of the structure: the structure profits where its
payoff at expiration is above zero, and that region is a union of intervals between breakevens, so its probability is the sum of the probabilities of
its intervals. The thresholds below (spread, open interest, width of a spread in strikes) are defaults, not calibrated in a backtest.
"""
from __future__ import annotations

import math
import unicodedata
from typing import Any

import numpy as np

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.options import _common as c
from hokma.quant.options import payoff as pay
from hokma.quant.options.liquidity import liquidity
from hokma.quant.options.structures import CATALOG, EV_WARNING, MATRIX, build_legs, structure_profile
from hokma.quant.types import QuantResult

# The six views of the sketch -> (direction of the code, intensity). "directional" is both families at once.
VIEWS: dict[str, tuple[tuple[str, int], ...]] = {
    "very_bearish": (("bearish", 2),), "bearish": (("bearish", 1),), "neutral": (("neutral", 1),),
    "directional": (("bullish", 1), ("bearish", 1)), "bullish": (("bullish", 1),), "very_bullish": (("bullish", 2),),
}
VIEW_LABELS = {"es": {"very_bearish": "Muy bajista", "bearish": "Bajista", "neutral": "Neutral", "directional": "Direccional", "bullish": "Alcista",
                      "very_bullish": "Muy alcista"},
               "en": {"very_bearish": "Very bearish", "bearish": "Bearish", "neutral": "Neutral", "directional": "Directional", "bullish": "Bullish",
                      "very_bullish": "Very bullish"}}
PRIORITIES = ("balanced", "max_return", "max_probability")
OUTRIGHT = ("long_call", "long_put")
CLASSIFICATION_TO_VOL = {"rich": "falling", "cheap": "rising", "fair": "flat"}
MAX_WINDOW = 10                 # strikes on each side of the money that a structure may centre on
MAX_WIDTH_STEPS = 6             # widest spread or wing, in strikes
MAX_CONDOR_WING_STEPS = 4
MAX_RELATIVE_SPREAD = 0.40      # quoted spread over mid above which a contract is not offered
MIN_OPEN_INTEREST = 1
PAYOFF_POINTS = 41
DEFAULT_RATE = 0.04             # the rate of hokma.market.options.PROB_ITM_RATE; the caller passes the live one
SHARES_PER_CONTRACT_KEY = "multiplier"


def _norm(text: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")
    return "_".join(raw.lower().replace("-", " ").split())


def parse_view(view: Any) -> str | None:
    """The id of a view from its id or its label in Spanish or English ("Muy bajista", "very bearish"); None when it is none of the six."""
    key = _norm(view)
    if key in VIEWS:
        return key
    for labels in VIEW_LABELS.values():
        for vid, label in labels.items():
            if _norm(label) == key:
                return vid
    return None


def vol_view_from_classification(classification: Any) -> tuple[str, str]:
    """The volatility view of the matrix and where it came from. Rich implied volatility is expected to fall, cheap to rise. Without a calibrated
    classification (uncalibrated, unavailable or nothing) the view is flat and says so: this module never calls volatility rich or cheap on its own."""
    mapped = CLASSIFICATION_TO_VOL.get(str(classification))
    return (mapped, "vol_analysis") if mapped else ("flat", "default_flat_uncalibrated")


def _extended_matrix() -> dict[tuple[str, str], tuple[str, ...]]:
    out: dict[tuple[str, str], tuple[str, ...]] = {}
    for key, names in MATRIX.items():
        items = list(names)
        if key[0] == "neutral" and key[1] in ("flat", "falling"):
            items.insert(1, "iron_butterfly")                       # next to the condor
        if key[0] == "bearish" or key == ("neutral", "rising"):
            items.append("protective_put")                          # the hedge of the shares, offered where the view is not bullish
        out[key] = tuple(items)
    return out


EXTENDED_MATRIX = _extended_matrix()


def family_structures(direction: str, vol_view: str, intensity: int = 1) -> tuple[str, ...]:
    """The structures of one family in order of preference. A strong view puts the outright option first, a mild one the spreads."""
    names = list(EXTENDED_MATRIX.get((direction, vol_view), ()))
    names.sort(key=(lambda n: n not in OUTRIGHT) if intensity >= 2 else (lambda n: n in OUTRIGHT))   # sort is stable
    return tuple(names)


def prob_above(spot: float, level: float, iv: float, years: float, rate: float = DEFAULT_RATE) -> float | None:
    """Probability that the price ends above level: N(d2) with d2 = (ln(S/K) + (r - iv^2 / 2) T) / (iv sqrt(T)), dividend yield 0. At or past expiration it is 1 or 0."""
    if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (spot, level, iv, years, rate)) or spot <= 0:
        return None
    if level <= 0:
        return 1.0
    if years <= 0:
        return 1.0 if spot > level else 0.0
    if iv <= 0:
        return None
    d2 = (math.log(spot / level) + (rate - iv * iv / 2.0) * years) / (iv * math.sqrt(years))
    return c.norm_cdf(d2)


def profit_intervals(legs: list[pay.Leg], breakevens: list[float]) -> list[tuple[float, float | None]]:
    """The price ranges at expiration where the payoff is above zero; the last one may be open (None). Between breakevens the payoff is linear and
    does not change sign, so one point of each range decides it."""
    bounds = [0.0] + [b for b in breakevens if b > 0]
    top = max([x for x in bounds] + [leg.strike for leg in legs if leg.strike is not None])
    out: list[tuple[float, float | None]] = []
    for i, lo in enumerate(bounds):
        hi = bounds[i + 1] if i + 1 < len(bounds) else None
        sample = (lo + hi) / 2.0 if hi is not None else top * 1.25 + 1.0
        if float(pay.pnl_at(legs, np.array([sample]))[0]) > 1e-9:
            out.append((lo, hi))
    return out


def probability_of_profit(legs: list[pay.Leg], breakevens: list[float], spot: float, iv: float, years: float, rate: float = DEFAULT_RATE) -> dict[str, Any] | None:
    """The probability of ending in the profit region and its pieces. None when the volatility, the time or the price cannot be used."""
    parts = []
    total = 0.0
    for lo, hi in profit_intervals(legs, breakevens):
        above_lo = prob_above(spot, lo, iv, years, rate)
        above_hi = 0.0 if hi is None else prob_above(spot, hi, iv, years, rate)
        if above_lo is None or above_hi is None:
            return None
        p = max(above_lo - above_hi, 0.0)
        parts.append({"from": lo, "to": hi, "probability": p})
        total += p
    return {"p_profit": min(total, 1.0), "intervals": parts}


@registry.register("options.profit_probability", module="options",
                   summary="Probability of profit of a named structure at expiration, the way an option chain shows the probability of finishing in the money: Black-Scholes N(d2) with the implied volatility given and the rate given (default 0.04), no dividends, summed over the price ranges where the payoff is above zero (between breakevens). strikes go low to high; premiums are per share, one per option leg in the order of the structure; quantity is in contracts and the contract multiplier is required. Returns the probability, its intervals and the breakevens.",
                   inputs={"structure": {"type": "str", "required": True}, "strikes": {"type": "list", "required": True}, "premiums": {"type": "list", "required": True},
                           "quantity": {"type": "float", "required": True}, "multiplier": {"type": "float", "required": True}, "spot": {"type": "float", "required": True},
                           "volatility": {"type": "float", "required": True}, "time_to_expiration": {"type": "float", "required": True},
                           "rate": {"type": "float", "required": False, "default": DEFAULT_RATE}},
                   piece_ids=("ECT-098",))
def profit_probability(structure: str, strikes: list, premiums: list, quantity: float, multiplier: float, spot: float, volatility: float,
                       time_to_expiration: float, rate: float = DEFAULT_RATE) -> QuantResult:
    name, unit, method, pieces = "options.profit_probability", "probability", "bs_d2_over_profit_region", ("ECT-098",)
    if not isinstance(structure, str) or not isinstance(strikes, (list, tuple)) or not isinstance(premiums, (list, tuple)):
        raise BadInput("structure must be a string; strikes and premiums lists of numbers")
    ks, ps = [c.num("strikes[]", x) for x in strikes], [c.num("premiums[]", x) for x in premiums]
    qty, mult, s = c.num("quantity", quantity), c.num("multiplier", multiplier), c.num("spot", spot)
    v, t, r = c.num("volatility", volatility), c.num("time_to_expiration", time_to_expiration), c.num("rate", rate)
    params = {"structure": structure, "strikes": ks, "premiums": ps, "quantity": qty, "multiplier": mult, "spot": s, "volatility": v,
              "time_to_expiration": t, "rate": r}
    if structure not in CATALOG:
        return c.empty(name, unit, method, f"structure must be one of {sorted(CATALOG)}", params=params, piece_ids=pieces)
    if not all(math.isfinite(x) for x in ks + ps + [qty, mult, s, v, t, r]) or qty <= 0 or mult <= 0 or s <= 0 or v <= 0 or t < 0 or any(k <= 0 for k in ks):
        return c.empty(name, unit, method, "strikes, quantity, multiplier, spot and volatility must be positive finite numbers; time not negative", params=params, piece_ids=pieces)

    def compute() -> dict:
        legs = build_legs(structure, ks, ps, qty, mult, s)
        info = pay.profile(legs)
        res = probability_of_profit(legs, info["breakevens"], s, v, t, r)
        if res is None:
            raise ValueError("the probability needs a positive volatility, a price and a time")
        return {**res, "breakevens": info["breakevens"], "model": "black_scholes_d2", "volatility": v, "rate": r, "time_to_expiration": t}

    return c.guarded(name, unit, method, pieces, params, compute)


# ------------------------------------------------------------------------------------------------------------------------- the chain

def _finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def quote_of(cell: Any) -> dict[str, Any] | None:
    """A contract of the chain as a quote the strategies can price with (mark = mid of bid and ask), or None when it has no valid two-sided quote.
    "liquid" is False when the relative spread or the open interest fails the defaults of this module."""
    if not isinstance(cell, dict):
        return None
    bid, ask = cell.get("bid"), cell.get("ask")
    if not (_finite(bid) and _finite(ask)) or bid < 0 or ask <= 0 or ask < bid:
        return None
    oi = cell.get("open_interest")
    oi = float(oi) if _finite(oi) and oi >= 0 else None
    res = liquidity(bid=float(bid), ask=float(ask), open_interest=oi)
    rel = None if res.value is None else res.value["relative_spread"]
    if rel is None:
        return None
    iv = cell.get("iv")
    return {"bid": float(bid), "ask": float(ask), "mark": (float(bid) + float(ask)) / 2.0, "iv": float(iv) if _finite(iv) and iv > 0 else None,
            "delta": float(cell["delta"]) if _finite(cell.get("delta")) else None, "open_interest": oi,
            "volume": float(cell["volume"]) if _finite(cell.get("volume")) else None, "relative_spread": rel,
            "liquid": rel <= MAX_RELATIVE_SPREAD and (oi is None or oi >= MIN_OPEN_INTEREST)}


def _read_chain(rows: list) -> tuple[list[float], dict[float, dict], dict[float, dict], dict[str, int]]:
    calls: dict[float, dict] = {}
    puts: dict[float, dict] = {}
    quoted = {"calls": 0, "puts": 0, "illiquid": 0}
    for row in rows:
        if not isinstance(row, dict) or not _finite(row.get("strike")) or row["strike"] <= 0:
            continue
        k = float(row["strike"])
        for side, store in (("call", calls), ("put", puts)):
            q = quote_of(row.get(side))
            if q is None:
                continue
            if not q["liquid"]:
                quoted["illiquid"] += 1
                continue
            store[k] = q
            quoted[side + "s"] += 1
    return sorted(set(calls) | set(puts)), calls, puts, quoted


def _candidates(structure: str, ks: list[float], spot: float) -> list[tuple[float, ...]]:
    """The strike combinations of one structure around the money, low to high. A leg without a liquid quote drops the combination later."""
    spec = CATALOG[structure]
    n = len(ks)
    if n == 0:
        return []
    atm = min(range(n), key=lambda i: abs(ks[i] - spot))
    centre = range(max(0, atm - MAX_WINDOW), min(n - 1, atm + MAX_WINDOW) + 1)
    out: list[tuple[float, ...]] = []
    if structure == "iron_condor":
        for i in centre:
            for j in centre:
                if i < j and ks[i] <= spot <= ks[j]:
                    for w in range(1, MAX_CONDOR_WING_STEPS + 1):
                        if i - w >= 0 and j + w < n:
                            out.append((ks[i - w], ks[i], ks[j], ks[j + w]))
    elif structure == "iron_butterfly":
        for b in centre:
            for w in range(1, MAX_WIDTH_STEPS + 1):
                if b - w >= 0 and b + w < n:
                    out.append((ks[b - w], ks[b], ks[b + w]))
    elif spec["strikes"] == 1:
        out = [(ks[i],) for i in centre]
    elif spec["strikes"] == 2:
        for i in centre:
            for j in range(i + 1, min(n, i + MAX_WIDTH_STEPS + 1)):
                if structure == "short_strangle" and not ks[i] <= spot <= ks[j]:
                    continue
                out.append((ks[i], ks[j]))
    return out


def _leg_quotes(structure: str, strikes: tuple[float, ...], calls: dict, puts: dict) -> list[dict] | None:
    quotes = []
    for position, kind, idx in CATALOG[structure]["legs"]:
        if position == "stock":
            continue
        store = calls if kind == "call" else puts
        q = store.get(strikes[idx])
        if q is None:
            return None
        quotes.append({"position": position, "kind": kind, "strike": strikes[idx], **q})
    return quotes


def _capital(structure: str, net: float, info: dict, cls: str) -> float | None:
    """What the structure ties up for one unit: the debit paid, or the most it can lose when it sells premium. A structure with shares needs only the
    debit (the shares are held). None when the loss has no bound."""
    if any(x[0] == "stock" for x in CATALOG[structure]["legs"]):
        return max(net, 0.0)
    if info["max_loss_usd"] is None:
        return None
    return max(net, 0.0) if cls == "debit" else info["max_loss_usd"]


def _target_for(direction: str, spot: float, target: float | None, move: float) -> float:
    if direction == "bullish":
        return target if target is not None and target > spot else spot + move
    if direction == "bearish":
        return target if target is not None and target < spot else spot - move
    return target if target is not None else spot


def _score(priority: str, p: float | None, ret: float | None) -> tuple:
    pp, rr = (-1.0 if p is None else p), (-math.inf if ret is None else ret)
    if priority == "max_probability":
        return (pp, rr)
    if priority == "max_return":
        return (rr, pp)
    both = p is not None and ret is not None
    return (math.sqrt(max(p, 0.0) * max(ret, 0.0)) if both else -1.0, pp, rr)


def _mean_iv(quotes: list[dict]) -> float | None:
    ivs = [q["iv"] for q in quotes if q["iv"]]
    return sum(ivs) / len(ivs) if ivs else None


def _evaluate(structure: str, strikes: tuple[float, ...], quotes: list[dict], spot: float, multiplier: float, years: float, rate: float,
              target: float) -> dict[str, Any] | None:
    spec = CATALOG[structure]
    premiums = [q["mark"] for q in quotes]
    legs = build_legs(structure, list(strikes), premiums, 1.0, multiplier, spot)
    info = pay.profile(legs)
    net = pay.net_premium_usd(legs)
    if (spec["class"] == "debit" and net <= 1e-9) or (spec["class"] == "credit" and net >= -1e-9):
        return None                                               # the quotes make it the other class: not the structure the name says
    if (info["max_profit_usd"] is not None and info["max_profit_usd"] <= 1e-9) or (info["max_loss_usd"] is not None and info["max_loss_usd"] <= 1e-9):
        return None                                               # a sure loss or a sure gain is a crossed quote, not a trade
    iv = _mean_iv(quotes)
    prob = None if iv is None else probability_of_profit(legs, info["breakevens"], spot, iv, years, rate)
    capital = _capital(structure, net, info, spec["class"])
    at_target = float(pay.pnl_at(legs, np.array([target]))[0])
    ret = at_target / capital if capital else None
    return {"structure": structure, "strikes": strikes, "quotes": quotes, "premiums": premiums, "info": info, "net": net, "capital": capital,
            "iv": iv, "p_profit": None if prob is None else prob["p_profit"], "pnl_at_target": at_target, "return_at_target": ret}


def _pick(cands: list[dict], priority: str, budget: float | None) -> tuple[dict | None, bool]:
    """The best candidate that fits the budget; when none fits, the one that ties up the least, and False."""
    fits = [x for x in cands if budget is None or (x["capital"] is not None and x["capital"] <= budget + 1e-9)]
    if fits:
        return max(fits, key=lambda x: _score(priority, x["p_profit"], x["return_at_target"])), True
    bounded = [x for x in cands if x["capital"] is not None]
    if bounded:
        return min(bounded, key=lambda x: x["capital"]), False
    return (cands[0] if cands else None), False


def _curve(legs: list[pay.Leg], spot: float) -> dict[str, list[float]]:
    strikes = [leg.strike for leg in legs if leg.strike is not None]
    lo, hi = min(strikes + [spot]) * 0.85, max(strikes + [spot]) * 1.15
    grid = np.unique(np.concatenate([np.linspace(lo, hi, PAYOFF_POINTS), np.array(strikes + [spot])]))
    return {"spots": grid.tolist(), "pnl_usd": pay.pnl_at(legs, grid).tolist()}


def _card(best: dict, fits: bool, family: str, spot: float, multiplier: float, years: float, rate: float, budget: float | None, target: float,
          shares_held: float | None, selling_open: bool, expiration: str) -> dict[str, Any]:
    structure, spec = best["structure"], CATALOG[best["structure"]]
    prof = structure_profile(structure, list(best["strikes"]), best["premiums"], 1.0, multiplier, spot, rate=rate, volatility=best["iv"], time_to_expiration=years)
    value = prof.value or {}
    legs_out = [{"position": q["position"], "kind": q["kind"], "strike": q["strike"], "premium": q["mark"], "bid": q["bid"], "ask": q["ask"], "iv": q["iv"],
                 "delta": q["delta"], "open_interest": q["open_interest"], "quantity": 1} for q in best["quotes"]]
    if any(x[0] == "stock" for x in spec["legs"]):
        legs_out.insert(0, {"position": "long", "kind": "stock", "strike": None, "premium": spot, "quantity": multiplier})
    needs = multiplier if any(x[0] == "stock" for x in spec["legs"]) else None
    locked = "premium_selling" if spec["class"] == "credit" and not selling_open else None
    short_of_shares = needs is not None and (shares_held is None or shares_held < needs)
    status = "locked" if locked else ("needs_shares" if short_of_shares else ("ok" if fits else "over_budget"))
    net = best["net"]
    return {"id": structure, "structure": structure, "family": family, "premium_class": spec["class"], "expiration": expiration, "quantity": 1,
            "multiplier": multiplier, "legs": legs_out, "net_premium_usd": net, "cost_usd": max(net, 0.0), "credit_usd": max(-net, 0.0),
            "capital_required_usd": best["capital"], "max_profit_usd": value.get("max_profit_usd"), "max_loss_usd": value.get("max_loss_usd"),
            "unbounded_profit": bool(value.get("unbounded_profit")), "unbounded_loss": bool(value.get("unbounded_loss")),
            "breakevens": value.get("breakevens", []), "p_profit": best["p_profit"],
            "p_profit_model": {"model": "black_scholes_d2", "volatility": best["iv"], "rate": rate, "time_to_expiration": years},
            "expected_value_usd": value.get("expected_value_usd"), "target_used": target, "pnl_at_target_usd": best["pnl_at_target"],
            "return_at_target": best["return_at_target"], "greeks": value.get("greeks"), "payoff": _curve(build_legs(structure, list(best["strikes"]), best["premiums"], 1.0, multiplier, spot), spot),
            "budget": {"budget_usd": budget, "capital_required_usd": best["capital"], "fits": fits}, "status": status, "locked": locked,
            "requires_shares": needs, "shares_held": shares_held if needs is not None else None, "can_open_detail": locked is None,
            "warnings": [w for w in prof.warnings if not w.startswith(EV_WARNING)]}


@registry.register("options.strategy_cards", module="options",
                   summary="Strategies of a view on one expiration of a real option chain: the six views of the sketch (very_bearish, bearish, neutral, directional, bullish, very_bullish) become the families of the structure matrix, plus iron_butterfly and protective_put; for each structure it picks the strikes of the chain (liquid contracts only, priced at the mid of bid and ask) that fit the budget for one contract and best serve the priority (balanced, max_return, max_probability), and returns one card with legs, cost or credit, maximum profit and loss, breakevens, probability of profit, greeks and the payoff curve. A card that does not fit says so with its cost (status over_budget); one that sells premium while the switch is closed is returned locked, not hidden; one that needs the shares says needs_shares. rows are the rows of one expiration of the chain tool; years is the time to expiration in years; multiplier comes from the contract sheet.",
                   inputs={"rows": {"type": "list", "required": True}, "spot": {"type": "float", "required": True}, "view": {"type": "str", "required": True},
                           "expiration": {"type": "str", "required": True}, "years": {"type": "float", "required": True},
                           "multiplier": {"type": "float", "required": True}, "target": {"type": "float", "required": False, "default": None},
                           "budget": {"type": "float", "required": False, "default": None}, "priority": {"type": "str", "required": False, "default": "balanced"},
                           "shares_held": {"type": "float", "required": False, "default": None}, "premium_selling_open": {"type": "bool", "required": False, "default": False},
                           "rate": {"type": "float", "required": False, "default": DEFAULT_RATE}, "vol_classification": {"type": "str", "required": False, "default": None}},
                   piece_ids=("ECT-098", "ECT-033"))
def strategy_cards(rows: list, spot: float, view: str, expiration: str, years: float, multiplier: float, target: float | None = None,
                   budget: float | None = None, priority: str = "balanced", shares_held: float | None = None, premium_selling_open: bool = False,
                   rate: float = DEFAULT_RATE, vol_classification: str | None = None) -> QuantResult:
    name, unit, method, pieces = "options.strategy_cards", "usd", "view_to_cards_on_real_chain", ("ECT-098", "ECT-033")
    if not isinstance(rows, (list, tuple)) or not isinstance(view, str) or not isinstance(priority, str):
        raise BadInput("rows must be a list of chain rows; view and priority strings")
    if not isinstance(premium_selling_open, bool):
        raise BadInput("premium_selling_open must be true or false")
    s, t, m = c.num("spot", spot), c.num("years", years), c.num("multiplier", multiplier)
    tgt, bud = c.num("target", target, allow_none=True), c.num("budget", budget, allow_none=True)
    held, r = c.num("shares_held", shares_held, allow_none=True), c.num("rate", rate)
    params = {"n_rows": len(rows), "spot": s, "view": view, "expiration": str(expiration), "years": t, "multiplier": m, "target": tgt, "budget": bud,
              "priority": priority, "shares_held": held, "premium_selling_open": premium_selling_open, "rate": r, "vol_classification": vol_classification}
    vid = parse_view(view)
    if vid is None:
        return c.empty(name, unit, method, f"view must be one of {sorted(VIEWS)}", params=params, piece_ids=pieces)
    if priority not in PRIORITIES:
        return c.empty(name, unit, method, f"priority must be one of {PRIORITIES}", params=params, piece_ids=pieces)
    if not all(math.isfinite(x) for x in (s, t, m, r)) or s <= 0 or m <= 0 or t <= 0:
        return c.empty(name, unit, method, "spot and multiplier must be positive, years above zero, all finite (the option may have expired)", params=params, piece_ids=pieces)
    if any(x is not None and not math.isfinite(x) for x in (tgt, bud, held)) or (bud is not None and bud < 0) or (tgt is not None and tgt <= 0):
        return c.empty(name, unit, method, "target must be positive and budget not negative, finite", params=params, piece_ids=pieces)

    def compute() -> dict:
        ks, calls, puts, counts = _read_chain(list(rows))
        vol_view, vol_source = vol_view_from_classification(vol_classification)
        atm = min(ks, key=lambda k: abs(k - s)) if ks else None
        atm_ivs = [q["iv"] for q in (calls.get(atm), puts.get(atm)) if q and q["iv"]] if atm is not None else []
        move = s * (sum(atm_ivs) / len(atm_ivs)) * math.sqrt(t) * math.sqrt(2.0 / math.pi) if atm_ivs else s * 0.05
        cards: list[dict] = []
        skipped: list[dict] = []
        seen: set[str] = set()
        for direction, intensity in VIEWS[vid]:
            tgt_f = _target_for(direction, s, tgt, move)
            for structure in family_structures(direction, vol_view, intensity):
                if structure in seen:
                    continue
                seen.add(structure)
                cands = []
                for strikes in _candidates(structure, ks, s):
                    quotes = _leg_quotes(structure, strikes, calls, puts)
                    ev = None if quotes is None else _evaluate(structure, strikes, quotes, s, m, t, r, tgt_f)
                    if ev is not None:
                        cands.append(ev)
                best, fits = _pick(cands, priority, bud)
                if best is None:
                    skipped.append({"structure": structure, "family": direction, "reason": "no_liquid_quotes" if not (calls or puts) else "no_valid_strikes"})
                    continue
                cards.append(_card(best, fits, direction, s, m, t, r, bud, tgt_f, held, premium_selling_open, str(expiration)[:10]))
        return {"view": vid, "families": [d for d, _ in VIEWS[vid]], "vol_view": vol_view, "vol_view_source": vol_source, "priority": priority,
                "spot": s, "expiration": str(expiration)[:10], "years": t, "multiplier": m, "budget_usd": bud, "target": tgt, "expected_move": move,
                "chain": {"strikes": len(ks), "liquid_calls": counts["calls"], "liquid_puts": counts["puts"], "illiquid_dropped": counts["illiquid"],
                          "max_relative_spread": MAX_RELATIVE_SPREAD, "min_open_interest": MIN_OPEN_INTEREST},
                "cards": cards, "skipped": skipped}

    return c.guarded(name, unit, method, pieces, params, compute)
